"""評測測試:標準答案比對、門檻掃描(用決策實際比的分數)、評測工具的報告位置與樣本資料夾。

不呼叫真實模型、不連網:辨識一律用 MockAnalyzer 或假物件,樣本是 tmp_path 裡的合成白圖。
"""
import sys
from datetime import date
from pathlib import Path

from PIL import Image

from src.config import BASE_DIR, AppConfig
from src.evaluation import (
    RULE_ONLY_MAX_SCORE,
    GroundTruth,
    compare,
    evaluate_file,
    load_labels,
    recommend_threshold,
    sweep_thresholds,
    threshold_warning,
)
from src.models import ExtractionResult
from src.providers import MockAnalyzer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import evaluate as evaluate_tool  # noqa: E402


def _truth() -> GroundTruth:
    return GroundTruth(
        filename="a.png",
        doc_type="發票",
        date="2026-07-04",
        vendor="全聯實業",
        amount=350.0,
        invoice_number="AB12345678",
    )


def test_all_key_fields_match_is_correct():
    result = ExtractionResult(
        doc_type="發票", date="2026-07-04", vendor="全聯實業股份有限公司",
        amount=350.0, invoice_number="AB-12345678", confidence=0.9,
    )
    row = compare(result, _truth())
    assert row["correct"] is True
    assert row["vendor_ok"] is True          # 全名 vs 簡稱採雙向包含
    assert row["fields"]["invoice_number"]   # 忽略連字號差異


def test_wrong_amount_makes_it_incorrect():
    result = ExtractionResult(
        doc_type="發票", date="2026-07-04", vendor="全聯實業", amount=35.0, confidence=0.95
    )
    row = compare(result, _truth())
    assert row["correct"] is False
    assert row["fields"]["amount"] is False


def test_vendor_mismatch_does_not_affect_correct():
    result = ExtractionResult(
        doc_type="發票", date="2026-07-04", vendor="家樂福", amount=350.0,
        invoice_number="AB12345678", confidence=0.9,
    )
    row = compare(result, _truth())
    assert row["correct"] is True   # vendor 僅供參考，不納入整體正確
    assert row["vendor_ok"] is False


def test_blank_truth_fields_are_not_scored():
    truth = GroundTruth(filename="a.png", doc_type="收據", amount=None, date=None)
    result = ExtractionResult(doc_type="收據", date=None, amount=None, confidence=0.6)
    row = compare(result, truth)
    assert row["correct"] is True
    assert "amount" not in row["fields"]


def test_sweep_and_recommend_picks_lowest_qualifying_threshold():
    records = [
        {"score": 0.95, "correct": True},
        {"score": 0.90, "correct": True},
        {"score": 0.85, "correct": True},
        {"score": 0.80, "correct": True},
        {"score": 0.75, "correct": True},
        {"score": 0.70, "correct": True},
        {"score": 0.70, "correct": True},
        {"score": 0.70, "correct": True},
        {"score": 0.70, "correct": True},
        {"score": 0.65, "correct": False},  # 低分且錯誤：門檻 0.70 剛好排除
    ]
    rows = sweep_thresholds(records, thresholds=[0.60, 0.70, 0.80])
    by_t = {r["threshold"]: r for r in rows}
    assert by_t[0.60]["auto_precision"] == 0.9
    assert by_t[0.70]["auto_precision"] == 1.0

    best = recommend_threshold(rows, target_precision=0.90)
    # 0.60 與 0.70 都達標，選自動化比例較高的 0.60
    assert best["threshold"] == 0.60
    assert best["automation_rate"] == 1.0


def test_recommend_returns_none_when_target_unreachable():
    records = [{"score": 0.99, "correct": False}, {"score": 0.5, "correct": True}]
    rows = sweep_thresholds(records, thresholds=[0.5, 0.9])
    assert recommend_threshold(rows, target_precision=0.95) is None


def test_load_labels_treats_blank_as_unscored(tmp_path):
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "檔名,doc_type,date,vendor,amount,invoice_number\n"
        "b.jpg,收據,2026-03-16,7-ELEVEN,89,\n",
        encoding="utf-8",
    )
    labels = load_labels(csv_path)
    assert labels["b.jpg"].amount == 89.0
    assert labels["b.jpg"].invoice_number is None


# ---- 評測的分數要和決策一致 ----------------------------------------------------

def test_compare_records_decision_score_not_just_self_confidence():
    result = ExtractionResult(doc_type="發票", date="2026-07-04", amount=350.0, confidence=0.99,
                              verified_confidence=0.10)   # 例如與 QR 不符
    row = compare(result, _truth())
    assert row["confidence"] == 0.99          # 模型自評只留作參考
    assert row["score"] == 0.10               # 掃門檻用決策實際比的分數


def test_sweep_uses_decision_score():
    # 模型自評很高但驗證信心很低的錯誤結果,不能被算成「自動歸檔」
    records = [
        {"confidence": 0.99, "score": 0.10, "correct": False},
        {"confidence": 0.60, "score": 0.95, "correct": True},
    ]
    (row,) = sweep_thresholds(records, thresholds=[0.80])
    assert row["n_auto"] == 1 and row["auto_precision"] == 1.0


def _sample(tmp_path, name="e01_合成發票.png"):
    path = tmp_path / name
    Image.new("RGB", (200, 120), "white").save(path)
    return path


def test_evaluate_file_runs_verification_before_scoring(tmp_path):
    # MockAnalyzer:發票、日期為今天、自評 0.92;合成白圖沒有 QR → 只有規則核對,驗證信心封頂 0.85
    path = _sample(tmp_path)
    truth = GroundTruth(filename=path.name, doc_type="發票", date=date.today().isoformat())
    row = evaluate_file(MockAnalyzer(AppConfig()), path, truth)
    assert row["confidence"] == 0.92
    assert row["辨識結果"]["verified_confidence"] == RULE_ONLY_MAX_SCORE
    assert row["score"] == RULE_ONLY_MAX_SCORE
    assert row["correct"] is True and row["錯誤"] is None


def test_evaluate_file_records_recognition_failure(tmp_path):
    class Broken:
        def analyze(self, file_path, doc_type_hint=None):
            raise ValueError("模型逾時")

    path = _sample(tmp_path)
    row = evaluate_file(Broken(), path, GroundTruth(filename=path.name, doc_type="發票"))
    assert row["correct"] is False and row["score"] == 0.0
    assert "模型逾時" in row["錯誤"]


def test_evaluate_file_broken_verifier_scores_zero(tmp_path, monkeypatch):
    """評測與 Pipeline 一致:核對程式出錯時分數是 0(轉人工),不退回模型自評 0.92。"""
    import src.verify as verify_mod

    def boom(result, file_path):
        raise RuntimeError("QR 解碼器壞了")

    monkeypatch.setattr(verify_mod, "verify_result", boom)
    path = _sample(tmp_path)
    row = evaluate_file(MockAnalyzer(AppConfig()), path, GroundTruth(filename=path.name, doc_type="發票"))
    assert row["confidence"] == 0.92 and row["score"] == 0.0


def test_threshold_warning_above_rule_only_ceiling():
    assert RULE_ONLY_MAX_SCORE == 0.85
    assert threshold_warning(0.85) is None
    warning = threshold_warning(0.90)
    assert "0.85" in warning and "轉人工" in warning


# ---- tools/evaluate.py:報告位置、樣本資料夾 -------------------------------------

def test_tool_writes_its_own_report_not_the_comparison_report():
    # 模型比較報告是人寫的分析;評測工具只寫自動產生的「模型實測結果」
    assert evaluate_tool.REPORT_PATH == BASE_DIR / "docs" / "模型實測結果.md"


def test_tool_defaults_to_synthetic_samples():
    assert evaluate_tool.DEFAULT_SAMPLES_DIR == BASE_DIR / "data" / "samples" / "einvoice"
    assert evaluate_tool.samples_warning(evaluate_tool.DEFAULT_SAMPLES_DIR) is None


def test_tool_warns_when_pointed_at_samples_root():
    # data/samples/ 根目錄的 labels.csv 可能是真實資料(個資)
    warning = evaluate_tool.samples_warning(BASE_DIR / "data" / "samples")
    assert warning and "真實資料" in warning and "data/samples/einvoice/" in warning


def test_tool_mock_run_scores_after_verification(tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    _sample(samples)
    (samples / "labels.csv").write_text(
        "檔名,doc_type,date,vendor,amount,invoice_number\ne01_合成發票.png,發票,,,,\n", encoding="utf-8")

    result = evaluate_tool.evaluate_model(AppConfig(), None, mock=True, samples_dir=samples)
    (row,) = result["records"]
    assert row["confidence"] == 0.92 and row["score"] == RULE_ONLY_MAX_SCORE

    out = evaluate_tool._write_report([result], 0.90, samples, out=tmp_path / "模型實測結果.md")
    text = out.read_text(encoding="utf-8")
    assert "effective_score" in text and "| 0.92 | 0.85 |" in text


def test_tool_report_warns_when_recommended_threshold_exceeds_rule_ceiling():
    # 只有 0.95 的那筆對:建議門檻 0.90 → 只有規則核對的類型會全部轉人工
    records = [{"score": 0.95, "correct": True}, {"score": 0.85, "correct": False}]
    text = evaluate_tool._calibration_section(records, 0.90)
    assert "auto_threshold: 0.90" in text and "轉人工" in text
