"""verify_result 與 verified_confidence 公式的測試:邊界值、單調性、各類型的檢查組合、Pipeline 串接。"""
from datetime import date
from itertools import product
from pathlib import Path

import pytest
import qrcode
from PIL import Image

from src.config import AppConfig, PathsConfig
from src.decision import decide
from src.models import ExtractionResult
from src.pipeline import Pipeline
from src.verify import einvoice, run_verification, score_verification, verify_result
from src.verify import checks as c

TODAY = date(2026, 9, 30)
CFG = AppConfig()  # auto_threshold 0.80


def chk(status: str, *fields: str) -> dict:
    return {"status": status, "detail": "", "fields": list(fields)}


QR3 = {
    einvoice.CHECK_QR_INVOICE: chk("pass", "invoice_number"),
    einvoice.CHECK_QR_DATE: chk("pass", "date"),
    einvoice.CHECK_QR_TOTAL: chk("pass", "amount"),
}


def test_independent_checks_reexported_from_package():
    # 介面判斷「獨立證據」時從 verify 套件取,不必深入 einvoice 模組
    from src.verify import INDEPENDENT_CHECKS

    assert INDEPENDENT_CHECKS is einvoice.INDEPENDENT_CHECKS


# --- 公式邊界 ---

def test_no_checks_or_all_skip_is_none():
    assert score_verification({}, "發票", 0.9)[0] is None
    assert score_verification({"x": chk("skip", "date")}, "發票", 0.9)[0] is None


def test_qr_three_way_match_ignores_low_model_confidence():
    score, summary = score_verification(QR3, "發票", 0.6)
    assert score == pytest.approx(0.95)
    assert "QR 相符" in summary


def test_qr_extra_fields_add_small_bonus_capped():
    found = {**QR3,
             einvoice.CHECK_QR_SELLER: chk("pass", "fields.seller_tax_id"),
             einvoice.CHECK_QR_RANDOM: chk("pass", "fields.random_code")}
    assert score_verification(found, "發票", 0.5)[0] == pytest.approx(0.97)
    found[einvoice.CHECK_QR_BUYER] = chk("pass", "fields.buyer_tax_id")
    assert score_verification(found, "發票", 0.5)[0] == pytest.approx(einvoice_max := 0.98)
    assert einvoice_max < 1.0


def test_qr_required_fields_only_is_090():
    found = {**QR3, einvoice.CHECK_QR_INVOICE: chk("skip", "invoice_number")}
    assert score_verification(found, "發票", 0.5)[0] == pytest.approx(0.90)


def test_qr_conflict_caps_even_when_model_is_sure():
    found = {**QR3, einvoice.CHECK_QR_TOTAL: chk("fail", "amount")}
    score, summary = score_verification(found, "發票", 0.99)
    assert score == pytest.approx(0.10)
    assert "總計額" in summary


def test_qr_conflict_on_auxiliary_field_also_caps():
    found = {**QR3, einvoice.CHECK_QR_SELLER: chk("fail", "fields.seller_tax_id")}
    assert score_verification(found, "發票", 0.99)[0] <= 0.10


@pytest.mark.parametrize("ratio_checks, conf, expected", [
    ({c.CHECK_DATE: chk("pass", "date"), c.CHECK_AMOUNT: chk("pass", "amount")}, 0.92, 0.85),
    ({c.CHECK_DATE: chk("pass", "date"), c.CHECK_AMOUNT: chk("pass", "amount")}, 0.70, 0.70),
    ({c.CHECK_DATE: chk("pass", "date")}, 0.95, 0.725),          # 覆蓋 1/2
    ({c.CHECK_INVOICE_FORMAT: chk("pass", "invoice_number")}, 0.95, 0.60),  # 覆蓋 0/2
])
def test_rule_only_range_by_coverage(ratio_checks, conf, expected):
    assert score_verification(ratio_checks, "發票", conf)[0] == pytest.approx(expected)


def test_rule_fail_on_required_field_caps_at_030():
    found = {**QR3, c.CHECK_PERIOD: chk("fail", "date", "fields.period")}
    score, summary = score_verification(found, "發票", 0.9)
    assert score == pytest.approx(0.30)
    assert c.CHECK_PERIOD in summary


def test_rule_fail_on_other_field_caps_at_060():
    found = {**QR3, c.CHECK_SELLER_TAX_ID: chk("fail", "fields.seller_tax_id")}
    assert score_verification(found, "發票", 0.9)[0] == pytest.approx(0.60)


def test_underscore_keys_are_ignored():
    found = {**QR3, "_coverage": {"date": True}, "_summary": "x"}
    assert score_verification(found, "發票", 0.6)[0] == pytest.approx(0.95)


# --- 單調性(窮舉):skip→pass 不會變低;skip→fail、pass→fail 不會變高 ---

_TEMPLATE = [
    (einvoice.CHECK_QR_INVOICE, ("invoice_number",)),
    (einvoice.CHECK_QR_DATE, ("date",)),
    (einvoice.CHECK_QR_TOTAL, ("amount",)),
    (einvoice.CHECK_QR_SELLER, ("fields.seller_tax_id",)),
    (c.CHECK_DATE, ("date",)),
    (c.CHECK_AMOUNT, ("amount",)),
    (c.CHECK_SELLER_TAX_ID, ("fields.seller_tax_id",)),
]


@pytest.mark.parametrize("conf", [0.3, 0.75, 0.99])
def test_score_is_monotonic(conf):
    def score(statuses):
        found = {name: chk(s, *f) for (name, f), s in zip(_TEMPLATE, statuses)}
        return score_verification(found, "發票", conf)[0]

    for statuses in product(("skip", "pass", "fail"), repeat=len(_TEMPLATE)):
        before = score(statuses)
        if before is None:
            continue
        for i, s in enumerate(statuses):
            for new in ("pass", "fail"):
                if s == new or (s == "fail" and new == "pass"):
                    continue
                after = score(statuses[:i] + (new,) + statuses[i + 1:])
                if new == "pass":
                    assert after >= before, (statuses, i)
                else:
                    assert after <= before, (statuses, i)


# --- verify_result 整合 ---

LEFT = einvoice.build_left_qr("ZX10293847", date(2026, 7, 4), "5847", 333, 350,
                              "00000000", "04595257", "SyntheticTestOnly0000A==",
                              tail=":**********:1:1:1：鮮乳：2:175:")


def _invoice_png(path: Path, text: str = LEFT) -> Path:
    code = qrcode.QRCode(version=6, error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=5, border=4)
    code.add_data(text.encode("utf-8"))
    code.make(fit=True)
    qr = code.make_image(fill_color="black", back_color="white").convert("RGB")
    page = Image.new("RGB", (qr.width + 200, qr.height + 300), "white")
    page.paste(qr, (100, 200))
    page.save(path)
    return path


def _invoice(**overrides) -> ExtractionResult:
    base = dict(doc_type="發票", date="2026-07-04", vendor="合成測試商店", amount=350.0,
                invoice_number="ZX10293847", confidence=0.6,
                fields={"seller_tax_id": "04595257", "random_code": "5847", "period": "115年07-08月"})
    base.update(overrides)
    return ExtractionResult(**base)


def test_qr_match_archives_even_at_self_confidence_060(tmp_path):
    result = _invoice(confidence=0.6)
    run_verification(result, _invoice_png(tmp_path / "e.png"), TODAY)
    assert result.verified_confidence >= 0.95
    assert result.verification["_coverage"] == {"date": True, "amount": True}
    decision = decide(result, CFG)
    assert decision.action == "archive"
    assert "驗證信心" in decision.reason and "QR 相符" in decision.reason


def test_qr_mismatch_goes_to_review_even_at_self_confidence_099(tmp_path):
    result = _invoice(confidence=0.99, amount=530.0)
    run_verification(result, _invoice_png(tmp_path / "e.png"), TODAY)
    assert result.verified_confidence <= 0.30
    assert result.verification[einvoice.CHECK_QR_TOTAL]["status"] == "fail"
    decision = decide(result, CFG)
    assert decision.action == "review"
    assert "驗證信心" in decision.reason and "QR" in decision.reason


def test_invoice_without_qr_uses_rule_checks(tmp_path):
    fake = tmp_path / "無QR.png"
    fake.write_bytes(b"fake image bytes")
    result = _invoice(confidence=0.92)
    run_verification(result, fake, TODAY)
    v = result.verification
    assert v[einvoice.CHECK_QR]["status"] == "skip"
    assert v[einvoice.CHECK_CODE39]["status"] == "skip"
    assert v[c.CHECK_SELLER_TAX_ID]["status"] == "pass"
    assert v[c.CHECK_PERIOD]["status"] == "pass"
    assert v[c.CHECK_AMOUNT_SUM]["status"] == "skip"
    assert result.verified_confidence == pytest.approx(0.85)


def test_verification_entries_follow_contract(tmp_path):
    result = _invoice()
    run_verification(result, _invoice_png(tmp_path / "e.png"), TODAY)
    for name, item in result.verification.items():
        if name.startswith("_"):
            continue
        assert set(item) == {"status", "detail", "fields"}
        assert item["status"] in ("pass", "fail", "skip")
        assert isinstance(item["detail"], str) and item["detail"]
        assert isinstance(item["fields"], list)
    assert isinstance(result.verification["_summary"], str)


def test_verify_does_not_change_answer_fields(tmp_path):
    result = _invoice(amount=530.0, invoice_number="zx-10293847")
    before = result.to_dict()
    verify_result(result, _invoice_png(tmp_path / "e.png"))
    after = result.to_dict()
    for key in ("verification", "verified_confidence"):
        before.pop(key), after.pop(key)
    assert after == before


def test_broken_qr_decoder_still_runs_rule_checks(tmp_path, monkeypatch):
    def boom(result, file_path):
        raise RuntimeError("解碼器壞了")

    monkeypatch.setattr(einvoice, "verify_einvoice", boom)
    result = _invoice(confidence=0.9)
    run_verification(result, tmp_path / "e.png", TODAY)
    assert result.verification[einvoice.CHECK_QR]["status"] == "skip"
    assert result.verification[c.CHECK_DATE]["status"] == "pass"
    assert result.verified_confidence == pytest.approx(0.85)


def test_bill_checks_due_date_and_amount(tmp_path):
    bill = ExtractionResult(doc_type="帳單", date="2026-09-20", amount=1854.0, confidence=0.92,
                            fields={"due_date": "2026-10-15", "bill_kind": "電費"})
    run_verification(bill, tmp_path / "bill.png", TODAY)
    assert bill.verification[c.CHECK_DUE_DATE]["status"] == "pass"
    assert bill.verification["_coverage"] == {"amount": True, "fields.due_date": True}
    assert bill.verified_confidence == pytest.approx(0.85)
    assert decide(bill, CFG).action == "archive"

    late = ExtractionResult(doc_type="帳單", date="2026-09-20", amount=1854.0, confidence=0.99,
                            fields={"due_date": "2026-09-01"})
    run_verification(late, tmp_path / "bill.png", TODAY)
    assert late.verified_confidence == pytest.approx(0.30)
    assert decide(late, CFG).action == "review"


def test_letter_checks_date_and_subject(tmp_path):
    letter = ExtractionResult(doc_type="公文", date="2026-09-01", confidence=0.95,
                              fields={"subject": "請於收到本函後15日內補正文件"})
    run_verification(letter, tmp_path / "x.png", TODAY)
    assert set(k for k in letter.verification if not k.startswith("_")) == {c.CHECK_DATE, c.CHECK_SUBJECT}
    assert letter.verification["_coverage"] == {"date": True, "fields.subject": True}
    assert letter.verified_confidence == pytest.approx(0.85)   # min(0.95, 0.60 + 0.25)

    short = ExtractionResult(doc_type="公文", date="2026-09-01", confidence=0.95, fields={"subject": "補正"})
    run_verification(short, tmp_path / "x.png", TODAY)
    assert short.verification[c.CHECK_SUBJECT]["status"] == "fail"
    assert decide(short, CFG).action == "review"


def test_medication_bag_checks_date_and_items(tmp_path):
    bag = ExtractionResult(doc_type="藥袋", date="2026-09-29", confidence=0.95,
                           fields={"items": [{"name": "合成測試藥", "frequency_text": "每日三次"},
                                             {"name": "合成止癢藥", "prn": True}]})
    run_verification(bag, tmp_path / "x.png", TODAY)
    assert bag.verification[c.CHECK_MEDICATION_ITEMS]["status"] == "pass"
    assert bag.verified_confidence == pytest.approx(0.85)

    missing_usage = ExtractionResult(doc_type="藥袋", date="2026-09-29", confidence=0.95,
                                     fields={"items": [{"name": "合成測試藥"}]})
    run_verification(missing_usage, tmp_path / "x.png", TODAY)
    assert missing_usage.verification[c.CHECK_MEDICATION_ITEMS]["status"] == "fail"
    assert missing_usage.verified_confidence == pytest.approx(0.30)

    future = ExtractionResult(doc_type="藥袋", date="2026-10-05", confidence=0.95,
                              fields={"items": [{"name": "合成測試藥", "frequency_text": "每日一次"}]})
    run_verification(future, tmp_path / "x.png", TODAY)
    assert future.verification[c.CHECK_DATE]["status"] == "fail"   # 未來日期
    assert future.verified_confidence == pytest.approx(0.30)


def test_other_doc_type_has_no_checks(tmp_path):
    other = ExtractionResult(doc_type="其他", confidence=0.9)
    run_verification(other, tmp_path / "x.png", TODAY)
    assert other.verified_confidence is None
    assert other.verification == {"_coverage": {}, "_summary": "沒有可用的檢查"}


# --- Pipeline 串接:真的 QR 影像走完整條管線 ---

class _FixedAnalyzer:
    def __init__(self, result: ExtractionResult):
        self.result = result

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        return self.result


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(paths=PathsConfig(
        inbox=tmp_path / "inbox", archive=tmp_path / "archive", review=tmp_path / "review",
        failed=tmp_path / "failed", logs=tmp_path / "logs"))
    cfg.ensure_dirs()
    return cfg


def test_pipeline_uses_verified_confidence(tmp_path):
    cfg = _cfg(tmp_path)
    ok = Pipeline(cfg, _FixedAnalyzer(_invoice(confidence=0.6))).process_file(
        _invoice_png(cfg.paths.inbox / "相符.png"))
    assert ok["動作"] == "archive"
    assert ok["AI辨識結果"]["verified_confidence"] >= 0.95

    bad = Pipeline(cfg, _FixedAnalyzer(_invoice(confidence=0.99, amount=530.0))).process_file(
        _invoice_png(cfg.paths.inbox / "不符.png"))
    assert bad["動作"] == "review"
    assert bad["AI辨識結果"]["verification"][einvoice.CHECK_QR_TOTAL]["status"] == "fail"
