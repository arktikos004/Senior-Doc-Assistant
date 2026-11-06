"""三種做法對照(A 只依自評 / B 只解 QR / C 模型加分層核對)與各項指標。

不呼叫模型、不連網:讀值來自依檔名回傳固定結果的假 analyzer;做法 B 要解的 QR 是測試當場用
build_left_qr + qrcode 產生、寫進 tmp_path 的影像,其餘樣本是白圖。

測試集(10 筆,期望值都是手算的):
| # | 檔名               | 類型 | 狀況  | 讀值                         | A        | B        | C                    |
| 1 | inv_ok             | 發票 | clean | 全對、自評 0.90              | 接受 ✓   | 接受 ✓   | 接受 ✓(QR 相符)     |
| 2 | inv_misread        | 發票 | clean | 金額讀錯、自評 0.95          | 接受 ✗   | 接受 ✓   | 轉人工(與 QR 不符)  |
| 3 | inv_blur           | 發票 | blur  | 金額讀錯、QR 解不開          | 接受 ✗   | 轉人工   | 接受 ✗(只有規則)    |
| 4 | bill_glare         | 帳單 | glare | 全對、自評 0.50              | 轉人工   | 轉人工   | 轉人工(只有規則)    |
| 5 | bill_ok            | 帳單 | clean | 全對                         | 接受 ✓   | 轉人工   | 接受 ✓(只有規則)    |
| 6 | bill_wrong_due     | 帳單 | clean | 繳費期限讀錯                 | 接受 ✗   | 轉人工   | 接受 ✗(只有規則)    |
| 7 | letter_ok          | 公文 | clean | 全對                         | 接受 ✓   | 轉人工   | 接受 ✓(只有規則)    |
| 8 | letter_wrong_date  | 公文 | clean | 發文日讀錯 → 期限跟著錯      | 接受 ✗   | 轉人工   | 接受 ✗(只有規則)    |
| 9 | med_bag            | 藥袋 | clean | 漏讀 1、多讀 1、時段不符 1   | 接受 ✗   | 轉人工   | 接受 ✗(只有規則)    |
| 10| broken             | 收據 | clean | 辨識失敗                     | failed   | 轉人工   | failed(辨識失敗)    |
"""
import copy
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import qrcode
from PIL import Image

import src.verify as verify_mod
from src.config import BASE_DIR, AppConfig
from src.evaluation import (
    EVIDENCE_FAILED,
    EVIDENCE_QR,
    EVIDENCE_QR_CONFLICT,
    EVIDENCE_RULES,
    EVIDENCE_SELF,
    EVIDENCE_VERIFY_ERROR,
    GroundTruth,
    LABELS_JSONL,
    breakdown,
    deadline_metrics,
    deadline_summary,
    degraded_summary,
    evaluate_file,
    evidence_class,
    group_records,
    load_dataset,
    medication_summary,
    method_summary,
    qr_only,
    summary_conflict_summary,
    summary_conflicts,
)
from src.models import ExtractionResult
from src.verify import einvoice

SELLER = "04595257"   # 財政部統編檢查碼說明的範例號碼(通過檢查碼)
QR_OK = einvoice.build_left_qr("ZX10293847", date(2026, 7, 4), "5847", 333, 350, "00000000", SELLER,
                               "SyntheticTestOnly0000A==")
QR_MISREAD = einvoice.build_left_qr("QA55667788", date(2026, 8, 15), "1234", 1219, 1280, "00000000", SELLER,
                                    "SyntheticTestOnly0000B==")
MED_TRUTH = [
    {"name": "範例錠A 500mg", "timing": ["早", "晚"], "prn": False},
    {"name": "範例止痛錠", "timing": [], "prn": True},
    {"name": "範例膠囊C", "timing": ["睡前"], "prn": False},
]
LETTER_FIELDS = {"subject": "請補繳文件", "doc_number": "示範字第1150000001號", "deadline_text": "收到本函後15日內",
                 "deadline": "2026-10-10", "required_actions": ["補繳身分證影本"]}
NO_QR = {"present": False, "readable": False}


def _label(file, split, template, variant, condition, doc_type, **values):
    row = {"file": file, "split": split, "template": template, "variant": variant, "condition": condition,
           "doc_type": doc_type, "vendor": None, "date": None, "amount": None, "invoice_number": None,
           "fields": {}, "qr": NO_QR}
    row.update(values)
    return row


LABELS = [
    _label("inv_ok.png", "dev", "einvoice_a", 1, "clean", "發票", vendor="示範超市", date="2026-07-04",
           amount=350, invoice_number="ZX10293847", qr={"present": True, "readable": True}),
    _label("inv_misread.png", "test", "einvoice_a", 2, "clean", "發票", date="2026-08-15", amount=1280,
           invoice_number="QA55667788", qr={"present": True, "readable": True}),
    _label("inv_blur.png", "test", "einvoice_a", 3, "blur", "發票", date="2026-07-20", amount=530,
           invoice_number="AB12345678", qr={"present": True, "readable": False}),
    _label("bill_glare.png", "test", "bill_power", 1, "glare", "帳單", date="2026-09-20", amount=920,
           fields={"due_date": "2026-10-12", "bill_kind": "電費"}),
    _label("bill_ok.png", "dev", "bill_power", 2, "clean", "帳單", vendor="示範電力公司", date="2026-09-20",
           amount=1854, fields={"due_date": "2026-10-15", "bill_kind": "電費"}),
    _label("bill_wrong_due.png", "test", "bill_power", 3, "clean", "帳單", date="2026-09-21", amount=1286,
           fields={"due_date": "2026-10-20", "bill_kind": "電費"}),
    _label("letter_ok.png", "test", "letter_tax", 1, "clean", "公文", vendor="示範區公所", date="2026-09-25",
           fields=LETTER_FIELDS),
    _label("letter_wrong_date.png", "test", "letter_tax", 2, "clean", "公文", date="2026-09-25",
           fields=LETTER_FIELDS),
    _label("med_bag.png", "test", "med_clinic", 1, "clean", "藥袋", vendor="示範診所", date="2026-09-30",
           fields={"items": MED_TRUTH}),
    _label("broken.png", "test", "receipt_shop", 1, "clean", "收據", date="2026-09-01", amount=89),
]


def _bill(day, amount, due, conf, summary):
    return ExtractionResult(doc_type="帳單", date=day, vendor="示範電力公司", amount=amount, confidence=conf,
                            fields={"due_date": due, "bill_kind": "電費"}, plain_summary=summary)


def _letter(day, summary, deadline_text="收到本函後15日內"):
    return ExtractionResult(doc_type="公文", date=day, vendor="示範區公所", confidence=0.9,
                            fields={"subject": "請補繳文件", "doc_number": "示範字第1150000001號",
                                    "deadline_text": deadline_text}, plain_summary=summary)


READS = {
    "inv_ok.png": ExtractionResult(
        doc_type="發票", date="2026-07-04", vendor="示範超市", amount=350.0, invoice_number="ZX-10293847",
        confidence=0.9, fields={"seller_tax_id": SELLER, "random_code": "5847", "period": "115年07-08月"},
        plain_summary="這是 7 月 4 日在示範超市買東西的發票,總共 350 元。"),
    "inv_misread.png": ExtractionResult(
        doc_type="發票", date="2026-08-15", amount=1820.0, invoice_number="QA55667788", confidence=0.95,
        fields={"seller_tax_id": SELLER, "random_code": "1234"},
        plain_summary="這是 8 月 15 日的發票,總共 1,820 元。"),
    "inv_blur.png": ExtractionResult(
        doc_type="發票", date="2026-07-20", amount=580.0, invoice_number="AB12345678", confidence=0.9,
        plain_summary="這是 7 月 20 日的發票,總共 580 元。"),
    "bill_glare.png": _bill("2026-09-20", 920.0, "2026-10-12", 0.5, "這是電費帳單,要在 10 月 12 日前繳 920 元。"),
    "bill_ok.png": _bill("2026-09-20", 1854.0, "2026-10-15", 0.92, "這是電費帳單,要在10月15日前繳1,854元。"),
    "bill_wrong_due.png": _bill("2026-09-21", 1286.0, "2026-10-21", 0.92, "這是電費帳單,要在 10/21 前繳 1,286 元。"),
    "letter_ok.png": _letter("2026-09-25", "示範區公所來信,請在收到信後 15 天內補繳文件。",
                             deadline_text="收到本函後 15 日內"),
    "letter_wrong_date.png": _letter("2026-09-26", "示範區公所來信,要在 10 月 11 日前補繳文件。"),
    "med_bag.png": ExtractionResult(
        doc_type="藥袋", date="2026-09-30", vendor="示範診所", confidence=0.9,
        fields={"items": [{"name": "範例錠A 500mg", "timing": ["早"], "prn": False},
                          {"name": "範例止痛錠", "timing": [], "prn": True},
                          {"name": "範例維他命D", "timing": ["晚"], "prn": False}]},
        plain_summary="示範診所開的藥,共 3 種,請照藥袋上的時間服用。"),
}


class ByName:
    """依檔名回傳固定讀值的假 analyzer;broken.png 模擬模型逾時。記下每次呼叫的檔名與類型提示。"""

    def __init__(self, reads=READS):
        self.reads = reads
        self.calls: list[tuple[str, str | None]] = []

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.calls.append((file_path.name, doc_type_hint))
        if file_path.name not in self.reads:
            raise TimeoutError("模型逾時")
        return copy.deepcopy(self.reads[file_path.name])


def _qr_page(text: str, path: Path) -> None:
    """仿證明聯:左側 QR(規格 V6 以上、容錯 L)貼在白底上。"""
    code = qrcode.QRCode(version=6, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=4, border=4)
    code.add_data(text.encode("utf-8"))
    code.make(fit=True)
    qr = code.make_image(fill_color="black", back_color="white").convert("RGB")
    page = Image.new("RGB", (qr.width + 120, qr.height + 240), "white")
    page.paste(qr, (40, 160))
    page.save(path)


def build_dataset(folder: Path) -> Path:
    """寫出 10 張樣本影像(兩張有真的 QR,其餘白圖)與 labels.jsonl。"""
    folder.mkdir(parents=True, exist_ok=True)
    for row in LABELS:
        Image.new("RGB", (400, 300), "white").save(folder / row["file"])
    _qr_page(QR_OK, folder / "inv_ok.png")
    _qr_page(QR_MISREAD, folder / "inv_misread.png")
    (folder / LABELS_JSONL).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in LABELS) + "\n", encoding="utf-8")
    return folder


def run_dataset(folder: Path, analyzer=None, hint_mode: str = "none") -> list[dict]:
    analyzer = analyzer or ByName()
    return [evaluate_file(analyzer, folder / truth.filename, truth, cfg=AppConfig(),
                          hint=truth.doc_type if hint_mode == "label" else None)
            for truth in load_dataset(folder)]


@pytest.fixture(scope="module")
def records(tmp_path_factory) -> list[dict]:
    return run_dataset(build_dataset(tmp_path_factory.mktemp("threeway")))


def _decisions(records, method):
    return [r["做法"][method]["決策"] for r in records]


# ---- 三種做法:每筆的決策與對錯 ----------------------------------------------------------

def test_each_method_decides_as_designed(records):
    assert _decisions(records, "A") == ["archive", "archive", "archive", "review", "archive", "archive",
                                        "archive", "archive", "archive", "failed"]
    assert _decisions(records, "B") == ["archive", "archive"] + ["review"] * 8
    assert _decisions(records, "C") == ["archive", "review", "archive", "review", "archive", "archive",
                                        "archive", "archive", "archive", "failed"]
    model_correct = [r["correct"] for r in records]
    assert model_correct == [True, False, False, True, True, False, True, False, False, False]
    assert [r["做法"]["B"]["correct"] for r in records[:2]] == [True, True]   # QR 的日期與總計額都對


def test_model_is_called_once_per_sample(tmp_path):
    analyzer = ByName()
    run_dataset(build_dataset(tmp_path / "s"), analyzer)
    assert [name for name, _ in analyzer.calls] == [row["file"] for row in LABELS]


def test_hint_none_passes_no_hint_and_label_passes_truth_type(tmp_path):
    folder = build_dataset(tmp_path / "s")
    plain, hinted = ByName(), ByName()
    run_dataset(folder, plain, "none")
    rows = run_dataset(folder, hinted, "label")
    assert {hint for _, hint in plain.calls} == {None}
    assert [hint for _, hint in hinted.calls] == [row["doc_type"] for row in LABELS]
    assert [r["類型提示"] for r in rows] == [row["doc_type"] for row in LABELS]


def test_self_only_ignores_verification_but_layered_uses_it(records):
    misread = records[1]
    # 模型自評 0.95,A 照單全收;C 的 QR 總計額不符,分數壓到 0.10
    assert "模型自評信心 0.95" in misread["做法"]["A"]["理由"]
    assert misread["score"] == 0.10 and "QR" in misread["做法"]["C"]["理由"]
    assert misread["辨識結果"]["verified_confidence"] == 0.10   # A 用的是深拷貝,原讀值照樣有核對結果


def test_recognition_failure_fails_a_and_c_but_b_still_runs(records):
    broken = records[-1]
    assert broken["錯誤"].startswith("TimeoutError") and broken["辨識結果"] is None
    assert broken["做法"]["A"]["決策"] == broken["做法"]["C"]["決策"] == "failed"
    assert broken["做法"]["A"]["correct"] is None
    assert broken["做法"]["B"] == {"決策": "review", "理由": "文件沒有電子發票 QR,只解 QR 無從判讀",
                                  "correct": None, "fields": {}}
    assert broken["證據類別"] == EVIDENCE_FAILED


def test_records_carry_label_metadata_and_are_json_serializable(records):
    first = records[0]
    assert (first["doc_type"], first["split"], first["template"], first["variant"], first["condition"]) == (
        "發票", "dev", "einvoice_a", 1, "clean")
    assert first["QR"] == {"present": True, "readable": True} and first["來源"] == LABELS_JSONL
    json.dumps(records, ensure_ascii=False)   # 原始結果要寫成 JSON


# ---- 做法 B:只解 QR -------------------------------------------------------------------

def test_qr_only_skips_documents_without_qr(tmp_path):
    path = tmp_path / "a.png"
    _qr_page(QR_OK, path)
    truth = GroundTruth(filename="a.png", doc_type="發票", date="2026-07-04", amount=350.0, qr_present=False)
    assert qr_only(path, truth)["決策"] == "review"   # 標準答案說沒有 QR 就不嘗試


def test_qr_only_treats_decoded_qr_as_invoice(tmp_path):
    # 解得開就當發票接受:標準答案若是收據,類型就錯(只解 QR 的做法分不出來)
    path = tmp_path / "r.png"
    _qr_page(QR_OK, path)
    truth = GroundTruth(filename="r.png", doc_type="收據", date="2026-07-04", amount=350.0, qr_present=True)
    outcome = qr_only(path, truth)
    assert outcome["決策"] == "archive" and outcome["correct"] is False
    assert outcome["fields"]["doc_type"] is False and outcome["fields"]["amount"] is True


def test_qr_only_unreadable_or_corrupt_file_is_handed_off(tmp_path):
    corrupt = tmp_path / "c.png"
    corrupt.write_bytes(b"not an image")
    truth = GroundTruth(filename="c.png", doc_type="發票", qr_present=True)
    assert qr_only(corrupt, truth)["決策"] == "review"
    assert qr_only(tmp_path / "missing.png", truth)["決策"] == "review"


def test_qr_only_decoder_error_is_handed_off(tmp_path, monkeypatch):
    path = tmp_path / "a.png"
    _qr_page(QR_OK, path)

    def boom(image):
        raise RuntimeError("解碼器壞了")

    monkeypatch.setattr(einvoice, "decode_qr_texts", boom)
    truth = GroundTruth(filename="a.png", doc_type="發票", qr_present=True)
    assert qr_only(path, truth)["決策"] == "review"


# ---- 指標 ----------------------------------------------------------------------------

def test_overall_method_summary(records):
    a, b, c = (method_summary(records, m) for m in ("A", "B", "C"))
    assert (a["樣本數"], a["自動接受"], a["接受錯誤"], a["轉人工"]) == (10, 8, 5, 2)
    assert (a["自動接受比例"], a["接受錯誤比例"], a["轉人工比例"]) == (0.8, 0.625, 0.2)
    assert (b["自動接受"], b["接受錯誤"], b["轉人工"]) == (2, 0, 8)
    assert (b["自動接受比例"], b["接受錯誤比例"], b["轉人工比例"]) == (0.2, 0.0, 0.8)
    assert (c["自動接受"], c["接受錯誤"], c["轉人工"]) == (7, 4, 3)
    assert c["接受錯誤比例"] == pytest.approx(4 / 7) and c["轉人工比例"] == 0.3


def test_three_metrics_in_method_summary(records):
    # 手算自檔頭的 10 筆表:模型讀對的是 1、4、5、7 四筆;自動接受又全對的只有 1、5、7(4 讀對但轉人工)
    a, b, c = (method_summary(records, m) for m in ("A", "B", "C"))
    assert (a["關鍵欄位讀對"], a["關鍵欄位讀對率"]) == (4, 0.4)
    assert (a["正確且免更正完成"], a["正確且免更正完成率"]) == (3, 0.3)
    assert (b["關鍵欄位讀對"], b["關鍵欄位讀對率"]) == (2, 0.2)     # B 只有解得開 QR 的兩筆有讀值
    assert (b["正確且免更正完成"], b["正確且免更正完成率"]) == (2, 0.2)
    assert (c["關鍵欄位讀對"], c["關鍵欄位讀對率"]) == (4, 0.4)
    assert (c["正確且免更正完成"], c["正確且免更正完成率"]) == (3, 0.3)


@pytest.mark.parametrize("index, read_right, completed", [
    (0, 1, 1),     # inv_ok:自動接受且全對,才算完成
    (3, 1, 0),     # bill_glare:讀對但轉人工,要有人看,不算完成
    (5, 0, 0),     # bill_wrong_due:自動接受卻讀錯,不算完成
    (1, 0, 0),     # inv_misread:讀錯、被 QR 攔下轉人工
    (9, 0, 0),     # broken:辨識失敗也留在分母
])
def test_completion_needs_both_auto_accepted_and_all_key_fields_right(records, index, read_right, completed):
    summary = method_summary([records[index]], "C")
    assert summary["樣本數"] == 1
    assert (summary["關鍵欄位讀對"], summary["正確且免更正完成"]) == (read_right, completed)
    assert summary["正確且免更正完成率"] == float(completed)


def test_completion_rate_without_samples_is_unknown():
    assert method_summary([], "C")["正確且免更正完成率"] is None     # 沒有樣本不代表完成率是 0


def test_completion_rate_by_doc_type(records):
    table = breakdown(records, "doc_type")
    assert table["帳單"]["C"]["正確且免更正完成率"] == pytest.approx(1 / 3)
    assert table["藥袋"]["C"]["正確且免更正完成率"] == 0.0
    assert table["收據"]["C"]["正確且免更正完成率"] == 0.0            # 只有一筆,辨識失敗
    assert table["帳單"]["B"]["正確且免更正完成率"] == 0.0            # 只解 QR 不處理帳單


def test_field_errors_among_accepted(records):
    assert method_summary(records, "A")["欄位錯誤"] == {
        "amount": 2, "fields.due_date": 1, "date": 1, "fields.deadline": 1, "fields.items": 1}
    assert method_summary(records, "B")["欄位錯誤"] == {}
    assert method_summary(records, "C")["欄位錯誤"] == {
        "amount": 1, "fields.due_date": 1, "date": 1, "fields.deadline": 1, "fields.items": 1}


def test_no_accepted_means_unknown_error_rate_not_zero():
    # 全部轉人工時錯誤歸零只是假象:接受結果中的錯誤比例沒有分母,是 None(報告顯示「—」)
    record = {"做法": {"A": {"決策": "review", "理由": "", "correct": True, "fields": {}}}}
    summary = method_summary([record], "A")
    assert summary["接受錯誤比例"] is None and summary["轉人工比例"] == 1.0


def test_breakdown_by_doc_type(records):
    table = breakdown(records, "doc_type")
    assert list(table) == ["發票", "收據", "藥袋", "帳單", "公文"]   # 依 DOC_TYPES 的順序
    invoice = table["發票"]
    assert (invoice["A"]["自動接受"], invoice["A"]["接受錯誤"]) == (3, 2)
    assert (invoice["B"]["自動接受"], invoice["B"]["接受錯誤"], invoice["B"]["轉人工"]) == (2, 0, 1)
    assert (invoice["C"]["自動接受"], invoice["C"]["接受錯誤"], invoice["C"]["轉人工"]) == (2, 1, 1)
    bills = table["帳單"]
    assert (bills["A"]["自動接受"], bills["A"]["接受錯誤"], bills["A"]["轉人工"]) == (2, 1, 1)
    assert bills["B"]["轉人工比例"] == 1.0 and bills["B"]["接受錯誤比例"] is None
    assert table["收據"]["C"]["轉人工"] == 1


def test_breakdown_by_split_template_and_condition(records):
    split = breakdown(records, "split")
    assert list(split) == ["dev", "test"]
    assert (split["dev"]["A"]["自動接受"], split["dev"]["A"]["接受錯誤"]) == (2, 0)
    assert (split["dev"]["B"]["自動接受"], split["dev"]["B"]["轉人工"]) == (1, 1)
    assert list(breakdown(records, "template")) == ["bill_power", "einvoice_a", "letter_tax", "med_clinic",
                                                    "receipt_shop"]
    condition = breakdown(records, "condition")
    assert list(condition) == ["clean", "blur", "glare"]
    assert condition["blur"]["C"]["接受錯誤"] == 1 and condition["glare"]["C"]["轉人工"] == 1


def test_unlabeled_groups_go_last():
    rows = [{"split": ""}, {"split": "test"}, {"split": None}]
    assert list(group_records(rows, "split")) == ["test", "(未標註)"]


def test_evidence_classes_for_layered_method(records):
    assert [r["證據類別"] for r in records] == [
        EVIDENCE_QR, EVIDENCE_QR_CONFLICT] + [EVIDENCE_RULES] * 7 + [EVIDENCE_FAILED]
    table = breakdown(records, "證據類別", methods=("C",))
    assert list(table) == [EVIDENCE_QR, EVIDENCE_QR_CONFLICT, EVIDENCE_RULES, EVIDENCE_FAILED]
    qr, conflict, rules = table[EVIDENCE_QR]["C"], table[EVIDENCE_QR_CONFLICT]["C"], table[EVIDENCE_RULES]["C"]
    assert (qr["樣本數"], qr["自動接受"], qr["接受錯誤"]) == (1, 1, 0)
    assert (conflict["自動接受"], conflict["轉人工"]) == (0, 1)
    assert (rules["樣本數"], rules["自動接受"], rules["接受錯誤"], rules["轉人工"]) == (7, 6, 4, 1)


def test_deadline_errors_bills_and_letters_separately(records):
    a, b, c = (deadline_summary(records, m) for m in ("A", "B", "C"))
    assert a["帳單"] == {"樣本數": 3, "自動接受": 2, "有期限答案": 2, "期限錯誤": 1, "期限錯誤比例": 0.5,
                       "依證據類別": {EVIDENCE_RULES: {"自動接受": 2, "期限錯誤": 1}}}
    assert (a["公文"]["自動接受"], a["公文"]["期限錯誤"], a["公文"]["期限錯誤比例"]) == (2, 1, 0.5)
    assert b["帳單"]["自動接受"] == 0 and b["帳單"]["期限錯誤比例"] is None
    assert c["帳單"]["依證據類別"] == {EVIDENCE_RULES: {"自動接受": 2, "期限錯誤": 1}}
    assert (c["公文"]["自動接受"], c["公文"]["期限錯誤"]) == (2, 1)


def test_deadline_rows_report_three_metrics_on_the_deadline_alone(records):
    a, b, c = (deadline_metrics(records, m) for m in ("A", "B", "C"))
    # 帳單 3 張:bill_glare 期限對但轉人工、bill_ok 對且接受、bill_wrong_due 期限錯卻接受
    bills = c["帳單"]
    assert (bills["樣本數"], bills["關鍵欄位讀對"], bills["自動接受"], bills["接受錯誤"], bills["正確且免更正完成"]) == (
        3, 2, 2, 1, 1)
    assert bills["關鍵欄位讀對率"] == pytest.approx(2 / 3) and bills["接受錯誤比例"] == 0.5
    assert bills["正確且免更正完成率"] == pytest.approx(1 / 3)
    assert a["帳單"] == bills                 # 自評 0.5 的 bill_glare 在 A 也轉人工,其餘相同
    # 只解 QR 不讀帳單:沒有接受就不知道錯誤率(不是 0),完成率是 0
    assert (b["帳單"]["樣本數"], b["帳單"]["關鍵欄位讀對"], b["帳單"]["自動接受"]) == (3, 0, 0)
    assert b["帳單"]["接受錯誤比例"] is None and b["帳單"]["正確且免更正完成率"] == 0.0
    # 公文 2 份都接受:發文日讀錯的那份,程式算出的期限跟著錯
    letters = c["公文"]
    assert (letters["樣本數"], letters["關鍵欄位讀對"], letters["接受錯誤"], letters["正確且免更正完成"]) == (2, 1, 1, 1)


def _accepted(doc_type, deadline=None, **fields):
    """手寫一筆自動接受的紀錄(做法 C),只帶指標要用的鍵。"""
    return {"doc_type": doc_type, "期限": deadline,
            "做法": {"C": {"決策": "archive", "理由": "", "correct": all(fields.values()), "fields": fields}}}


def test_bill_deadline_is_not_hidden_by_the_overall_average():
    # 9 張發票都對、1 張帳單期限錯卻自動接受:整體看起來只錯 1 成,帳單期限那一列是 100%
    rows = [_accepted("發票", amount=True) for _ in range(9)]
    rows.append(_accepted("帳單", {"標準答案": "2026-10-15", "讀值": "2026-10-16"}, **{"fields.due_date": False}))
    assert method_summary(rows, "C")["接受錯誤比例"] == 0.1
    assert deadline_metrics(rows, "C")["帳單"]["接受錯誤比例"] == 1.0


def test_bill_deadline_row_looks_only_at_the_deadline():
    # 金額錯、期限對:整份不算完成,期限那一列算對
    rows = [_accepted("帳單", {"標準答案": "2026-10-15", "讀值": "2026-10-15"},
                      amount=False, **{"fields.due_date": True})]
    assert method_summary(rows, "C")["正確且免更正完成"] == 0
    row = deadline_metrics(rows, "C")["帳單"]
    assert (row["關鍵欄位讀對"], row["接受錯誤"], row["正確且免更正完成"]) == (1, 0, 1)


def _bill_truth(**values):
    return GroundTruth(filename="b.png", doc_type="帳單", **values)


def test_bill_deadline_failed_read_stays_in_the_denominator(tmp_path):
    # 辨識失敗的帳單沒有讀值,但期限答案還在:分母要算它,不能因為讀不出來就從這一列消失
    path = tmp_path / "b.png"
    Image.new("RGB", (400, 300), "white").save(path)
    row = evaluate_file(ByName({}), path, _bill_truth(fields={"due_date": "2026-10-15"}), cfg=AppConfig())
    assert row["錯誤"] and row["期限"] == {"標準答案": "2026-10-15", "讀值": None}
    bills = deadline_metrics([row], "C")["帳單"]
    assert (bills["樣本數"], bills["關鍵欄位讀對"], bills["自動接受"]) == (1, 0, 0)
    assert bills["正確且免更正完成率"] == 0.0 and bills["接受錯誤比例"] is None


def test_bill_deadline_without_an_answer_is_left_out(tmp_path):
    # 標準答案沒有期限的帳單(舊 CSV):沒得比,不進這一列;辨識失敗時也一樣
    path = tmp_path / "b.png"
    Image.new("RGB", (400, 300), "white").save(path)
    read = {"b.png": _bill("2026-09-20", 920.0, "2026-10-12", 0.9, "")}
    rows = [evaluate_file(ByName(read), path, _bill_truth(), cfg=AppConfig()),
            evaluate_file(ByName({}), path, _bill_truth(), cfg=AppConfig())]
    bills = deadline_metrics(rows, "C")["帳單"]
    assert bills["樣本數"] == 0 and bills["關鍵欄位讀對率"] is None and bills["正確且免更正完成率"] is None


def test_bill_read_as_another_type_counts_as_a_wrong_deadline(tmp_path):
    # 帳單讀成收據還自動接受:不會有繳費提醒,期限等於漏了,算錯誤自動接受
    path = tmp_path / "b.png"
    Image.new("RGB", (400, 300), "white").save(path)
    read = {"b.png": ExtractionResult(doc_type="收據", date="2026-09-20", amount=920.0, confidence=0.9)}
    row = evaluate_file(ByName(read), path, _bill_truth(fields={"due_date": "2026-10-12"}), cfg=AppConfig())
    assert row["做法"]["C"]["決策"] == "archive"
    bills = deadline_metrics([row], "C")["帳單"]
    assert (bills["關鍵欄位讀對"], bills["自動接受"], bills["接受錯誤"], bills["正確且免更正完成"]) == (0, 1, 1, 0)


def test_degraded_abstain_vs_wrongly_accepted(records):
    assert degraded_summary(records, "A") == {"樣本數": 2, "棄答": 1, "錯了卻被接受": 1, "對且被接受": 0,
                                              "棄答比例": 0.5, "錯了卻被接受比例": 0.5}
    assert degraded_summary(records, "B")["棄答"] == 2
    assert degraded_summary(records, "C")["錯了卻被接受"] == 1


def test_medication_missing_and_extra(records):
    summary = medication_summary(records)
    assert summary["樣本數"] == summary["有讀值"] == 1
    assert summary["品項"] == {"漏讀": {"品項": 1, "件數": 1}, "多讀": {"品項": 1, "件數": 1},
                             "時段不符": {"品項": 1, "件數": 1}, "需要時不符": {"品項": 0, "件數": 0}}
    assert summary["做法"] == {"A": {"自動接受": 1, "接受結果中有漏讀或多讀": 1},
                             "C": {"自動接受": 1, "接受結果中有漏讀或多讀": 1}}
    assert records[8]["藥袋"]["漏讀"] == ["範例膠囊C"] and records[8]["藥袋"]["多讀"] == ["範例維他命D"]


def test_summary_conflicts_counted_per_method(records):
    assert [bool(r["白話解說矛盾"]) for r in records] == [
        False, True, True, False, False, True, False, True, False, False]
    assert records[1]["白話解說矛盾"] == ["金額 1,820 元(標準答案 1,280 元)"]
    assert records[5]["白話解說矛盾"] == ["日期 10 月 21 日(不是標準答案的日期或期限)"]
    assert summary_conflict_summary(records, "A") == {"有解說": 9, "解說矛盾": 4, "自動接受": 8,
                                                      "接受結果中解說矛盾": 4}
    assert summary_conflict_summary(records, "C")["接受結果中解說矛盾"] == 3
    assert summary_conflict_summary(records, "B") is None   # 只解 QR 沒有白話解說


# ---- 證據類別與白話解說矛盾的細節 ----------------------------------------------------------

def test_evidence_class_without_checks_or_with_broken_verifier(tmp_path, monkeypatch):
    path = tmp_path / "x.png"
    Image.new("RGB", (100, 80), "white").save(path)
    other = ExtractionResult(doc_type="其他", confidence=0.9)
    verify_mod.verify_or_flag(other, path)
    assert other.verified_confidence is None and evidence_class(other) == EVIDENCE_SELF

    def boom(result, file_path):
        raise RuntimeError("核對程式壞了")

    monkeypatch.setattr(verify_mod, "verify_result", boom)
    bill = ExtractionResult(doc_type="帳單", amount=100.0, confidence=0.9, fields={"due_date": "2026-10-15"})
    verify_mod.verify_or_flag(bill, path)
    assert bill.verified_confidence == 0.0 and evidence_class(bill) == EVIDENCE_VERIFY_ERROR
    assert evidence_class(None) == EVIDENCE_FAILED


def test_evidence_conflict_beats_qr_match():
    # 必要欄位都與 QR 相符,但賣方統編與 QR 矛盾:分數上限 0.10,歸「與 QR 不符」
    result = ExtractionResult(doc_type="發票", confidence=0.9, verified_confidence=0.10, verification={
        einvoice.CHECK_QR_DATE: {"status": "pass", "detail": "", "fields": ["date"]},
        einvoice.CHECK_QR_TOTAL: {"status": "pass", "detail": "", "fields": ["amount"]},
        einvoice.CHECK_QR_SELLER: {"status": "fail", "detail": "", "fields": ["fields.seller_tax_id"]},
    })
    assert evidence_class(result) == EVIDENCE_QR_CONFLICT


def test_evidence_partial_qr_match_is_rules_only():
    # QR 只證實日期、總計額讀不到(skip):沒有涵蓋全部必要欄位,不能算 QR 相符
    result = ExtractionResult(doc_type="發票", confidence=0.9, verified_confidence=0.85, verification={
        einvoice.CHECK_QR_DATE: {"status": "pass", "detail": "", "fields": ["date"]},
        einvoice.CHECK_QR_TOTAL: {"status": "skip", "detail": "", "fields": ["amount"]},
    })
    assert evidence_class(result) == EVIDENCE_RULES


@pytest.mark.parametrize("summary, expected", [
    ("要在１０月１５日前繳１，８５４元。", []),                                  # 全形數字
    ("要在 115/10/15 前繳 NT$1,854。", []),                                   # 民國年日期、NT$
    ("要在 2026-10-16 前繳 1854 元", ["日期 10 月 16 日(不是標準答案的日期或期限)"]),
    ("9/20 出帳,10/15 前繳 1,850 元", ["金額 1,850 元(標準答案 1,854 元)"]),
    ("一次吃 1/2 顆,一千八百元", []),                                         # 劑量不是日期、國字數字抽不到
    ("", []),
])
def test_summary_conflicts_heuristic(summary, expected):
    truth = GroundTruth(filename="b.png", doc_type="帳單", date="2026-09-20", amount=1854.0,
                        fields={"due_date": "2026-10-15"})
    assert summary_conflicts(summary, truth) == expected


def test_summary_conflicts_skip_what_truth_does_not_have():
    truth = GroundTruth(filename="m.png", doc_type="藥袋")   # 沒有金額、沒有任何日期
    assert summary_conflicts("10 月 3 日領的藥,自付 50 元", truth) == []


# ---- 報告與命令列(tools/evaluate.py)------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import evaluate as evaluate_tool  # noqa: E402

DOCS_REPORT = BASE_DIR / "docs" / "模型實測結果.md"


def _result(records, model="假模型") -> dict:
    n = len(records)
    return {"模型": model, "樣本數": n, "整體正確率": sum(1 for r in records if r["correct"]) / n,
            "平均耗時秒": 0.0, "平均模型自評": 0.9, "辨識失敗": 1, "split": "all", "類型提示": "none",
            "自動接受門檻": 0.80, "records": records}


@pytest.fixture(scope="module")
def report_text(records, tmp_path_factory) -> str:
    folder = tmp_path_factory.mktemp("report")
    out = evaluate_tool._write_report([_result(records)], 0.90, folder, out=folder / "r.md")
    return out.read_text(encoding="utf-8")


def test_report_keeps_old_sections_and_adds_three_way(report_text):
    for heading in ("## 模型比較", "## 三種做法對照(同一測試集)", "## 信心門檻校準", "## 匿名逐筆明細",
                    "### 總表", "### 接受結果中各欄位錯誤數", "### 依類型", "### 依證據類別(做法 C)",
                    "### 期限錯誤(帳單、公文分開)", "### 藥袋漏讀/多讀",
                    "### 降質影像:棄答 vs 錯了卻被接受", "### 依 split", "### 依版型",
                    "### 白話解說矛盾(啟發式)"):
        assert heading in report_text, heading
    assert report_text.index("## 三種做法對照") < report_text.index("## 信心門檻校準")
    assert "effective_score" in report_text and "自動接受門檻 0.80" in report_text


def _metrics_section(text: str) -> str:
    return text[text.index("### 三個指標"):text.index("### 總表")]


def test_report_three_metrics_come_first_with_definitions(report_text):
    assert report_text.index("### 三個指標") < report_text.index("### 總表")
    section = _metrics_section(report_text)
    assert "| 範圍 | 做法 | 樣本數 | 關鍵欄位讀對率 | 錯誤自動接受率 | 正確且免更正完成率 |" in section
    assert "| 整體 | A 只依自評 | 10 | 40.0%(4/10) | 62.5%(5/8) | 30.0%(3/10) |" in section
    assert "| 整體 | B 只解 QR | 10 | 20.0%(2/10) | 0.0%(0/2) | 20.0%(2/10) |" in section
    assert "| 整體 | C 模型加分層核對 | 10 | 40.0%(4/10) | 57.1%(4/7) | 30.0%(3/10) |" in section
    assert "自動接受(不用人看)而且關鍵欄位全對" in section   # 定義要寫在報告上
    assert "服藥時間表" in section                            # 藥袋的自動接受只代表文件歸檔


def test_report_bill_deadline_has_its_own_rows(report_text):
    section = _metrics_section(report_text)
    assert "| 帳單期限 | A 只依自評 | 3 | 66.7%(2/3) | 50.0%(1/2) | 33.3%(1/3) |" in section
    assert "| 帳單期限 | B 只解 QR | 3 | 0.0%(0/3) | — | 0.0%(0/3) |" in section    # 沒有接受 → 錯誤率「—」
    assert "| 帳單期限 | C 模型加分層核對 | 3 | 66.7%(2/3) | 50.0%(1/2) | 33.3%(1/3) |" in section
    assert "| 公文期限 | C 模型加分層核對 | 2 | 50.0%(1/2) | 50.0%(1/2) | 50.0%(1/2) |" in section


def test_report_three_metrics_by_doc_type_for_the_layered_method(report_text):
    section = _metrics_section(report_text)
    assert "| 發票 | C 模型加分層核對 | 3 | 33.3%(1/3) | 50.0%(1/2) | 33.3%(1/3) |" in section
    assert "| 收據 | C 模型加分層核對 | 1 | 0.0%(0/1) | — | 0.0%(0/1) |" in section     # 辨識失敗
    assert "| 藥袋 | C 模型加分層核對 | 1 | 0.0%(0/1) | 100.0%(1/1) | 0.0%(0/1) |" in section
    assert "| 公文 | C 模型加分層核對 | 2 | 50.0%(1/2) | 50.0%(1/2) | 50.0%(1/2) |" in section


def test_report_without_bills_or_letters_has_no_deadline_rows(records, tmp_path):
    invoices = [r for r in records if r["doc_type"] == "發票"]
    text = evaluate_tool._write_report([_result(invoices)], 0.90, tmp_path, out=tmp_path / "r.md").read_text(
        encoding="utf-8")
    section = _metrics_section(text)
    assert "| 整體 | C 模型加分層核對 | 3 |" in section
    assert "帳單期限 |" not in section and "公文期限 |" not in section


def test_console_prints_three_metrics_and_bill_deadline(records, capsys):
    evaluate_tool._print_three_way(_result(records))
    out = capsys.readouterr().out
    assert "關鍵欄位讀對 40.0%(4/10)、錯誤自動接受 57.1%(4/7)、正確且免更正完成 30.0%(3/10)" in out
    assert "帳單期限(做法 C):讀對 66.7%(2/3)、錯誤自動接受 50.0%(1/2)、正確且免更正完成 33.3%(1/3)" in out


def test_report_overall_table_numbers(report_text):
    assert "| 做法 | 樣本數 | 自動接受比例 | 接受結果中的錯誤比例 | 轉人工比例 |" in report_text
    assert "| A 只依自評 | 10 | 80.0%(8/10) | 62.5%(5/8) | 20.0%(2/10) |" in report_text
    assert "| B 只解 QR | 10 | 20.0%(2/10) | 0.0%(0/2) | 80.0%(8/10) |" in report_text
    assert "| C 模型加分層核對 | 10 | 70.0%(7/10) | 57.1%(4/7) | 30.0%(3/10) |" in report_text


def test_report_field_errors_type_and_evidence(report_text):
    assert ("| 做法 | 自動接受 | `date` | `amount` | `fields.due_date` | `fields.deadline` | `fields.items` |"
            in report_text)
    assert "| A 只依自評 | 8 | 1 | 2 | 1 | 1 | 1 |" in report_text
    assert "| B 只解 QR | 2 | 0 | 0 | 0 | 0 | 0 |" in report_text
    assert "| 發票 | B 只解 QR | 3 | 66.7%(2/3) | 0.0%(0/2) | 33.3%(1/3) |" in report_text
    assert "| 帳單 | B 只解 QR | 3 | 0.0%(0/3) | — | 100.0%(3/3) |" in report_text   # 沒有接受 → 錯誤率「—」
    assert "| QR 相符 | 1 | 100.0%(1/1) | 0.0%(0/1) | 0.0%(0/1) |" in report_text
    assert "| 與 QR／條碼不符 | 1 | 0.0%(0/1) | — | 100.0%(1/1) |" in report_text
    assert "| 只有規則 | 7 | 85.7%(6/7) | 66.7%(4/6) | 14.3%(1/7) |" in report_text


def test_report_deadline_medication_degraded_and_groups(report_text):
    assert "| 帳單 | C 模型加分層核對 | 3 | 2 | 50.0%(1/2) | 只有規則 2/1 |" in report_text
    assert "| 公文 | A 只依自評 | 2 | 2 | 50.0%(1/2) | — |" in report_text
    assert "| 帳單 | B 只解 QR | 3 | 0 | — | — |" in report_text
    assert "| 漏讀(藥袋上有、讀值沒有) | 1 | 1 |" in report_text
    assert "| 多讀(讀值有、藥袋上沒有,即幻覺) | 1 | 1 |" in report_text
    assert "| B 只解 QR | 0 | —(不讀藥名) |" in report_text
    assert "| A 只依自評 | 2 | 50.0%(1/2) | 50.0%(1/2) | 0.0%(0/2) |" in report_text   # 降質:棄答 vs 錯了卻被接受
    assert "| blur | C 模型加分層核對 | 1 | 100.0%(1/1) | 100.0%(1/1) | 0.0%(0/1) |" in report_text
    assert "| dev | A 只依自評 | 2 | 100.0%(2/2) | 0.0%(0/2) | 0.0%(0/2) |" in report_text
    assert "| einvoice_a | B 只解 QR | 3 | 66.7%(2/3) | 0.0%(0/2) | 33.3%(1/3) |" in report_text
    assert "同版型數值變化不算獨立泛化證據" in report_text


def test_report_summary_conflicts_and_details(report_text):
    assert "| A 只依自評 | 9 | 4 | 8 | 50.0%(4/8) |" in report_text
    assert "| C 模型加分層核對 | 9 | 4 | 7 | 42.9%(3/7) |" in report_text
    assert "| B 只解 QR | — | — | 2 | —(沒有白話解說) |" in report_text
    assert "只是啟發式" in report_text
    assert "| 發票 | clean | 接受/接受/轉人工 |" in report_text          # 樣本 02:金額與 QR 不符
    assert "| 收據 | clean | 失敗/轉人工/失敗 |" in report_text          # 樣本 10:辨識失敗
    assert "inv_misread" not in report_text                               # 逐筆明細匿名


def test_report_mixed_sources_adds_old_csv_table(records, tmp_path):
    mixed = copy.deepcopy(records)
    for row in mixed[:3]:            # 三張發票當成舊 CSV 樣本
        row["來源"] = "labels.csv"
    text = evaluate_tool._write_report([_result(mixed)], 0.90, tmp_path, out=tmp_path / "r.md").read_text(
        encoding="utf-8")
    old = text[text.index("### 舊 CSV 樣本"):]
    assert "| A 只依自評 | 3 | 100.0%(3/3) | 66.7%(2/3) | 0.0%(0/3) |" in old
    assert "| B 只解 QR | 3 | 66.7%(2/3) | 0.0%(0/2) | 33.3%(1/3) |" in old


def _csv_samples(folder: Path) -> Path:
    """舊格式:一張真的電子發票 QR + 一張模糊的白圖,labels.csv。"""
    folder.mkdir(parents=True, exist_ok=True)
    _qr_page(QR_OK, folder / "e01_電子發票.png")
    Image.new("RGB", (400, 300), "white").save(folder / "e02_電子發票_模糊.png")
    (folder / "labels.csv").write_text(
        "檔名,doc_type,date,vendor,amount,invoice_number,degrade\n"
        "e01_電子發票.png,發票,2026-07-04,示範超市,350,ZX10293847,\n"
        "e02_電子發票_模糊.png,發票,2026-07-05,示範超市,120,AB12345678,模糊\n", encoding="utf-8-sig")
    return folder


@pytest.fixture
def no_side_effects(tmp_path, monkeypatch):
    """命令列測試的原始結果寫到 tmp_path;預設報告路徑也換掉,真的 docs/模型實測結果.md 不能被碰到。"""
    monkeypatch.setattr(evaluate_tool, "RESULTS_DIR", tmp_path / "raw")
    monkeypatch.setattr(evaluate_tool, "REPORT_PATH", tmp_path / "預設報告.md")
    before = DOCS_REPORT.stat().st_mtime_ns if DOCS_REPORT.exists() else None
    yield tmp_path
    assert (DOCS_REPORT.stat().st_mtime_ns if DOCS_REPORT.exists() else None) == before


def _raw_results(folder: Path) -> list[dict]:
    (raw,) = (folder / "raw").glob("eval_*.json")
    return json.loads(raw.read_text(encoding="utf-8"))


def test_cli_mock_old_csv_writes_report_flag_path(no_side_effects, capsys):
    tmp = no_side_effects
    out = tmp / "out" / "r.md"
    assert evaluate_tool.main(["--mock", "--samples", str(_csv_samples(tmp / "einvoice")),
                               "--report", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "## 三種做法對照(同一測試集)" in text and "## 信心門檻校準" in text
    # Mock 讀值的字軌固定 AB12345678、日期是今天:B 解得開第一張;C 在第一張與 QR 不符 → 轉人工
    assert "| B 只解 QR | 2 | 50.0%(1/2) | 0.0%(0/1) | 50.0%(1/2) |" in text
    assert "上面的總表就是舊 CSV 樣本的三方法總表" in text
    assert "樣本沒有標註「split」,不分組。" in text and "| 模糊 | B 只解 QR |" in text   # degrade 欄當拍攝狀況
    assert not (tmp / "預設報告.md").exists()
    assert "三種做法(模型 mock" in capsys.readouterr().out
    (result,) = _raw_results(tmp)
    assert result["split"] == "all" and result["三種做法"]["B"]["自動接受"] == 1


def test_cli_mock_without_report_never_writes_docs_report(no_side_effects):
    tmp = no_side_effects   # REPORT_PATH 已換成 tmp 裡的檔:Mock 沒給 --report 也不能寫到 REPORT_PATH
    assert evaluate_tool.main(["--mock", "--samples", str(_csv_samples(tmp / "einvoice"))]) == 0
    assert not (tmp / "預設報告.md").exists()
    assert len(list((tmp / "raw").glob("report_*.md"))) == 1


def test_default_report_path_only_for_real_model_on_default_samples(no_side_effects, monkeypatch):
    # 公開文件引用的數字只由「真實模型 × 預設的合成電子發票」重現;其他組合寫到 logs/eval/
    tmp = no_side_effects
    monkeypatch.setattr(evaluate_tool, "DEFAULT_SAMPLES_DIR", tmp / "einvoice")
    assert evaluate_tool.default_report_path(tmp / "einvoice", mock=False) == tmp / "預設報告.md"
    for samples, mock in ((tmp / "einvoice", True), (tmp / "eval", False), (tmp / "eval", True)):
        path = evaluate_tool.default_report_path(samples, mock=mock)
        assert path.parent == tmp / "raw" and path.name.startswith("report_")


def test_cli_split_and_hint_label(no_side_effects):
    tmp = no_side_effects
    folder = build_dataset(tmp / "jsonl")
    assert evaluate_tool.main(["--mock", "--samples", str(folder), "--split", "test", "--hint", "label",
                               "--report", str(tmp / "r.md")]) == 0
    (result,) = _raw_results(tmp)
    assert result["split"] == "test" and result["類型提示"] == "label"
    assert [r["檔名"] for r in result["records"]] == [row["file"] for row in LABELS if row["split"] == "test"]
    assert all(r["類型提示"] == r["doc_type"] for r in result["records"])
    assert "split:test;類型提示:標準答案的類型" in (tmp / "r.md").read_text(encoding="utf-8")


def test_cli_raw_results_carry_the_three_metrics(no_side_effects):
    tmp = no_side_effects
    assert evaluate_tool.main(["--mock", "--samples", str(build_dataset(tmp / "jsonl")),
                               "--report", str(tmp / "r.md")]) == 0
    (result,) = _raw_results(tmp)
    overall = result["三種做法"]["C"]
    assert overall["樣本數"] == 10 and {"關鍵欄位讀對率", "接受錯誤比例", "正確且免更正完成率"} <= set(overall)
    assert result["期限指標"]["C"]["帳單"]["樣本數"] == 3   # 3 張帳單的標準答案都有繳費期限
    assert "### 三個指標" in (tmp / "r.md").read_text(encoding="utf-8")


def test_cli_without_labels_returns_1(no_side_effects):
    empty = no_side_effects / "empty"
    empty.mkdir()
    assert evaluate_tool.main(["--mock", "--samples", str(empty), "--report", str(empty / "r.md")]) == 1


def test_cli_split_with_no_samples_stops(no_side_effects):
    # 舊 CSV 樣本沒有 split:只評 dev 時沒有任何樣本,直接停下來
    folder = _csv_samples(no_side_effects / "einvoice")
    with pytest.raises(SystemExit, match="標準答案為空"):
        evaluate_tool.main(["--mock", "--samples", str(folder), "--split", "dev",
                            "--report", str(no_side_effects / "r.md")])


def test_cloud_evaluation_never_sends_medication_bags(tmp_path, monkeypatch):
    # 量雲端模型時藥袋與收據一律略過:系統本來就不會把它們送雲端(原則 6),合成的也不送
    import src.providers.workers_ai as workers_ai

    fake = ByName()
    monkeypatch.setattr(workers_ai, "WorkersAIAnalyzer", lambda cfg: fake)
    result = evaluate_tool.evaluate_model(AppConfig(), "雲端模型", mock=False, provider="workers_ai",
                                          samples_dir=build_dataset(tmp_path / "s"))
    assert "med_bag.png" not in [name for name, _ in fake.calls]
    assert result["樣本數"] == 8 and not {"藥袋", "收據"} & {r["doc_type"] for r in result["records"]}
