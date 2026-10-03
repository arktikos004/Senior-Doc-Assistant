"""決策代理的單元測試:驗證信心分數與欄位檢查邏輯。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig
from src.decision import decide, effective_score
from src.models import ExtractionResult

CFG = AppConfig()  # auto_threshold 預設 0.80


def make_result(**overrides) -> ExtractionResult:
    base = dict(
        doc_type="發票",
        date="2026-07-04",
        vendor="全聯實業",
        amount=1250.0,
        invoice_number="AB12345678",
        confidence=0.95,
    )
    base.update(overrides)
    return ExtractionResult(**base)


def test_high_confidence_archives():
    assert decide(make_result(), CFG).action == "archive"


def test_none_result_fails():
    assert decide(None, CFG).action == "failed"


def test_low_confidence_goes_to_review():
    assert decide(make_result(confidence=0.5), CFG).action == "review"


def test_confidence_at_threshold_archives():
    assert decide(make_result(confidence=0.80), CFG).action == "archive"


def test_manual_items_go_to_review_whatever_the_reading():
    """上傳時選了初賽不能自動判讀的文件:讀值再好也轉人工,原因寫家人選的名稱;沒有名稱(身分證明選「不確定」)
    沿用身分證明類的原因;讀不出來照一般規則記成 failed。"""
    from src.decision import IDENTITY_REVIEW_REASON, decide_manual

    good = make_result()                                 # 欄位齊全、自評 0.95 的發票,一般規則會存檔
    assert decide(good, CFG).action == "archive"
    decision = decide_manual(good, CFG, label="保單")
    assert (decision.action, decision.reason) == ("review", "「保單」初賽還不能自動判讀,需人工確認")
    assert (decide_manual(good, CFG).action, decide_manual(good, CFG).reason) == ("review", IDENTITY_REVIEW_REASON)
    assert decide_manual(None, CFG, label="保單").action == "failed"


def test_unknown_doc_type_goes_to_review():
    assert decide(make_result(doc_type="其他"), CFG).action == "review"


def test_missing_date_goes_to_review():
    decision = decide(make_result(date=None), CFG)
    assert decision.action == "review"
    assert "日期" in decision.reason


def test_missing_amount_goes_to_review():
    decision = decide(make_result(amount=None), CFG)
    assert decision.action == "review"
    assert "金額" in decision.reason


# --- 看有(第二代):各類型必要欄位 ---

def test_bill_requires_due_date():
    bill = make_result(doc_type="帳單", invoice_number=None)
    assert decide(bill, CFG).action == "review"
    assert "繳費期限" in decide(bill, CFG).reason
    bill.fields = {"due_date": "2026-10-15"}
    assert decide(bill, CFG).action == "archive"


def test_medication_bag_needs_items_not_amount():
    bag = make_result(doc_type="藥袋", amount=None, fields={"items": [{"name": "普拿疼"}]})
    assert decide(bag, CFG).action == "archive"
    bag.fields = {}
    assert "藥品清單" in decide(bag, CFG).reason


def test_official_letter_requires_subject():
    letter = make_result(doc_type="公文", amount=None, fields={"subject": "繳納地價稅"})
    assert decide(letter, CFG).action == "archive"


def test_unreadable_required_field_goes_to_review():
    # 模型填了金額卻自己標記讀不清 → 不可自動歸檔
    decision = decide(make_result(unreadable=["amount"]), CFG)
    assert decision.action == "review" and "金額" in decision.reason


# --- W1-B:有驗證信心時用它比門檻,理由寫明用了哪一種 ---

def test_verified_confidence_overrides_low_self_confidence():
    # QR 相符:模型自評只有 0.6 也能自動歸檔
    r = make_result(confidence=0.6, verified_confidence=0.95,
                    verification={"_summary": "QR 相符:字軌號碼、開立日期、總計額"})
    decision = decide(r, CFG)
    assert decision.action == "archive"
    assert decision.reason.startswith("驗證信心 0.95(QR 相符")


def test_verified_confidence_overrides_high_self_confidence():
    # QR 不符:模型自評 0.99 也要轉人工
    r = make_result(confidence=0.99, verified_confidence=0.10,
                    verification={"_summary": "與 QR 不符:總計額"})
    decision = decide(r, CFG)
    assert decision.action == "review"
    assert "驗證信心 0.10" in decision.reason and "QR 不符" in decision.reason


def test_verified_confidence_at_threshold_archives():
    assert decide(make_result(confidence=0.1, verified_confidence=0.80), CFG).action == "archive"
    assert decide(make_result(confidence=0.99, verified_confidence=0.79), CFG).action == "review"


def test_falls_back_to_self_confidence_and_says_so():
    decision = decide(make_result(confidence=0.95), CFG)
    assert decision.action == "archive"
    assert "模型自評信心 0.95" in decision.reason and "未經驗證" in decision.reason


def test_verified_but_missing_required_field_still_reviews():
    decision = decide(make_result(amount=None, verified_confidence=0.95), CFG)
    assert decision.action == "review" and "金額" in decision.reason


def test_effective_score_is_what_decide_compares():
    # 決策與評測共用同一個分數:有驗證信心就用它(即使比自評低),否則退回模型自評
    assert effective_score(make_result(confidence=0.95)) == 0.95
    assert effective_score(make_result(confidence=0.95, verified_confidence=0.10)) == 0.10
    assert effective_score(make_result(confidence=0.60, verified_confidence=0.95)) == 0.95
