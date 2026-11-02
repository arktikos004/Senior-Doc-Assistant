"""逐類型比對:帳單繳費期限、公文期限(與 plan_actions 同一個函式、同一個基準日)、藥袋品項配對。

全部用手做的 ExtractionResult 與標準答案驗算,不呼叫模型、不讀檔。
"""
import copy
from datetime import date

import pytest

from src.actions import plan_actions
from src.config import AppConfig
from src.evaluation import GroundTruth, compare, compare_items, program_deadline, program_due_date
from src.models import Decision, ExtractionResult

CFG = AppConfig()
ARCHIVE = Decision(action="archive", reason="測試")
RECEIVED = date(2026, 10, 1)   # 上傳日:公文讀不到發文日期時從這天起算


# ---- 帳單 ----------------------------------------------------------------------------

def _bill_truth(**fields) -> GroundTruth:
    return GroundTruth(filename="bill.png", doc_type="帳單", date="2026-09-20", amount=1854.0,
                       fields={"due_date": "2026-10-15", "bill_kind": "電費", **fields})


def _bill(**overrides) -> ExtractionResult:
    base = dict(doc_type="帳單", date="2026-09-20", vendor="示範電力公司", amount=1854.0, confidence=0.9,
                fields={"due_date": "2026-10-15", "bill_kind": "電費"})
    base.update(overrides)
    return ExtractionResult(**base)


def test_bill_correct_needs_amount_and_due_date():
    row = compare(_bill(), _bill_truth())
    assert row["correct"] is True
    assert row["fields"] == {"doc_type": True, "date": True, "amount": True,
                             "fields.due_date": True, "fields.bill_kind": True}
    assert row["期限"] == {"標準答案": "2026-10-15", "讀值": "2026-10-15"}


def test_bill_wrong_due_date_is_incorrect():
    row = compare(_bill(fields={"due_date": "2026-10-16", "bill_kind": "電費"}), _bill_truth())
    assert row["correct"] is False and row["fields"]["fields.due_date"] is False
    assert row["期限"]["讀值"] == "2026-10-16"


def test_bill_kind_and_date_are_auxiliary():
    # 帳單的關鍵欄位只有 doc_type、amount、due_date;bill_kind 與帳單日期讀錯只記在欄位錯誤裡
    row = compare(_bill(date="2026-09-21", fields={"due_date": "2026-10-15", "bill_kind": "水費"}), _bill_truth())
    assert row["correct"] is True
    assert row["fields"]["fields.bill_kind"] is False and row["fields"]["date"] is False


def test_bill_roc_due_date_is_read_like_plan_bill():
    # 模型照抄民國年時,程式和 _plan_bill 一樣用 parse_date 換算;換出來的就是提醒的日期
    bill = _bill(fields={"due_date": "115/10/15", "bill_kind": "電費"})
    assert program_due_date(bill) == "2026-10-15"
    (action,) = plan_actions(copy.deepcopy(bill), ARCHIVE, CFG)
    assert action["payload"]["date"] == program_due_date(bill)
    assert compare(bill, _bill_truth())["correct"] is True


def test_bill_read_as_other_type_has_no_due_date():
    # 讀成收據就不會產生繳費提醒:期限算錯,類型也錯
    receipt = _bill(doc_type="收據")
    assert program_due_date(receipt) is None
    row = compare(receipt, _bill_truth())
    assert row["fields"]["fields.due_date"] is False and row["fields"]["doc_type"] is False


def test_bill_unreadable_due_date_is_wrong_not_skipped():
    row = compare(_bill(fields={"bill_kind": "電費"}), _bill_truth())
    assert row["fields"]["fields.due_date"] is False and row["期限"]["讀值"] is None


# ---- 公文 ----------------------------------------------------------------------------

def _letter_truth(**fields) -> GroundTruth:
    return GroundTruth(filename="letter.png", doc_type="公文", date="2026-09-25", vendor="示範區公所",
                       fields={"subject": "請補繳文件", "deadline_text": "收到本函後15日內",
                               "deadline": "2026-10-10", **fields})


def _letter(**overrides) -> ExtractionResult:
    base = dict(doc_type="公文", date="2026-09-25", vendor="示範區公所", confidence=0.9,
                fields={"subject": "請補繳文件", "doc_number": "示範字第1150000001號",
                        "deadline_text": "收到本函後15日內"})
    base.update(overrides)
    return ExtractionResult(**base)


def _plan_actions_deadline(result: ExtractionResult, received_on: date) -> str | None:
    """系統實際產生提醒時寫進 fields["deadline"] 的值(plan_actions 會改 result,先複製)。"""
    probe = copy.deepcopy(result)
    plan_actions(probe, ARCHIVE, CFG, received_on=received_on)
    return probe.fields.get("deadline")


@pytest.mark.parametrize("letter, expected", [
    (_letter(), "2026-10-10"),                                                         # 發文日 + 15 日
    (_letter(date=None), "2026-10-16"),                                                # 讀不到發文日 → 上傳日起算
    (_letter(fields={"deadline_text": "收到本函後15日內", "deadline": "2099-01-01"}), "2026-10-10"),  # 模型自己填的不採信
    (_letter(fields={"deadline_text": "自即日起30日內"}), "2026-10-24"),                 # 即日算入
    (_letter(fields={"deadline_text": "於115年10月31日前"}), "2026-10-31"),
    (_letter(fields={"deadline_text": "儘速辦理"}), None),                              # 算不出就沒有期限
    (_letter(doc_type="帳單"), None),                                                  # 讀成帳單就不會算公文期限
])
def test_official_deadline_is_exactly_what_plan_actions_writes(letter, expected):
    before = letter.to_dict()
    assert program_deadline(letter, RECEIVED) == _plan_actions_deadline(letter, RECEIVED) == expected
    assert letter.to_dict() == before   # 評測不能改到模型的讀值


def test_official_compare_uses_program_deadline():
    row = compare(_letter(), _letter_truth(), received_on=RECEIVED)
    assert row["correct"] is True
    assert row["fields"]["fields.deadline"] is True and row["fields"]["fields.deadline_text"] is True
    assert row["期限"] == {"標準答案": "2026-10-10", "讀值": "2026-10-10"}


def test_official_misread_issue_date_shifts_deadline():
    # 發文日讀錯一天,程式算出的期限跟著錯一天:日期與期限都算錯
    row = compare(_letter(date="2026-09-26"), _letter_truth(), received_on=RECEIVED)
    assert row["correct"] is False
    assert row["fields"]["date"] is False and row["fields"]["fields.deadline"] is False
    assert row["期限"]["讀值"] == "2026-10-11"


def test_official_deadline_text_is_compared_after_nfkc_and_punctuation():
    letter = _letter(fields={"deadline_text": "「收到本函後 １５ 日內。」"})
    row = compare(letter, _letter_truth(), received_on=RECEIVED)
    assert row["fields"]["fields.deadline_text"] is True and row["correct"] is True


@pytest.mark.parametrize("model_text, truth_text", [
    ("於115年10月31日前", "請於115年10月31日前"),          # 少了開頭的「請」
    ("於收到本函後15日內", "收到本函後15日內"),            # 多了開頭的「於」
    ("應於收到本函後15日內", "請於收到本函後15日內"),
])
def test_official_deadline_text_ignores_leading_please_and_at(model_text, truth_text):
    # 開頭的「請」「應」「並」「於」是語氣與介詞,不影響程式算出的期限;期限本身另由 fields.deadline 比
    truth = _letter_truth(deadline_text=truth_text)
    row = compare(_letter(fields={"deadline_text": model_text}), truth, received_on=RECEIVED)
    assert row["fields"]["fields.deadline_text"] is True


def test_official_deadline_text_still_wrong_when_the_date_differs():
    truth = _letter_truth(deadline_text="請於115年10月31日前")
    row = compare(_letter(fields={"deadline_text": "於115年10月30日前"}), truth, received_on=RECEIVED)
    assert row["fields"]["fields.deadline_text"] is False


def test_official_different_deadline_text_is_wrong():
    letter = _letter(fields={"deadline_text": "收到本函後10日內"})
    row = compare(letter, _letter_truth(), received_on=RECEIVED)
    assert row["fields"]["fields.deadline_text"] is False and row["fields"]["fields.deadline"] is False


def test_official_without_deadline_in_truth_is_not_scored():
    truth = GroundTruth(filename="letter.png", doc_type="公文", date="2026-09-25", fields={"subject": "通知"})
    row = compare(_letter(), truth, received_on=RECEIVED)
    assert "fields.deadline" not in row["fields"] and "fields.deadline_text" not in row["fields"]
    assert row["correct"] is True and row["期限"] is None


# ---- 藥袋 ----------------------------------------------------------------------------

TRUTH_ITEMS = [
    {"name": "範例錠A 500mg", "timing": ["早", "晚"], "prn": False},
    {"name": "範例止痛錠", "timing": [], "prn": True},
    {"name": "範例膠囊C", "timing": ["睡前"], "prn": False},
]


def _med_truth(items=None) -> GroundTruth:
    return GroundTruth(filename="med.png", doc_type="藥袋", date="2026-09-30",
                       fields={"items": TRUTH_ITEMS if items is None else items})


def _med(items) -> ExtractionResult:
    return ExtractionResult(doc_type="藥袋", date="2026-09-30", vendor="示範診所", confidence=0.9,
                            fields={"items": items})


def test_medication_all_match_after_name_and_timing_normalization():
    items = [
        {"name": "範例錠a 500 MG", "dose_text": "1 顆", "timing": ["早上", "晚上"], "prn": False},
        {"name": "範例止痛錠", "timing": [], "prn": "是"},
        {"name": "範例膠囊Ｃ", "timing": ["睡前"], "prn": False},    # 全形字母
    ]
    row = compare(_med(items), _med_truth())
    assert row["correct"] is True
    assert row["藥袋"] == {"ok": True, "漏讀": [], "多讀": [], "時段不符": [], "需要時不符": []}


def test_medication_missing_extra_timing_and_prn_are_reported():
    items = [
        {"name": "範例錠A 500mg", "timing": ["早"], "prn": False},      # 少了晚
        {"name": "範例止痛錠", "timing": [], "prn": False},             # 漏了「需要時」
        {"name": "範例維他命D", "timing": ["早"], "prn": False},        # 藥袋上沒有:多讀(幻覺)
    ]                                                                    # 範例膠囊C 漏讀
    row = compare(_med(items), _med_truth())
    assert row["correct"] is False and row["fields"]["fields.items"] is False
    assert row["藥袋"] == {"ok": False, "漏讀": ["範例膠囊C"], "多讀": ["範例維他命D"],
                          "時段不符": ["範例錠A 500mg"], "需要時不符": ["範例止痛錠"]}


def test_medication_same_name_pairs_in_order():
    truth = [{"name": "範例錠A", "timing": ["早"], "prn": False}, {"name": "範例錠A", "timing": ["晚"], "prn": False}]
    result = compare_items([{"name": "範例錠A", "timing": ["早"], "prn": False}], truth)
    assert result["漏讀"] == ["範例錠A"] and result["時段不符"] == [] and result["ok"] is False


def test_medication_nothing_read_is_all_missing():
    row = compare(ExtractionResult(doc_type="藥袋", date="2026-09-30", confidence=0.4), _med_truth())
    assert row["藥袋"]["漏讀"] == ["範例錠A 500mg", "範例止痛錠", "範例膠囊C"]
    assert row["correct"] is False


def test_medication_dispensed_date_is_auxiliary():
    row = compare(ExtractionResult(doc_type="藥袋", date="2026-09-29", confidence=0.9,
                                   fields={"items": TRUTH_ITEMS}), _med_truth())
    assert row["fields"]["date"] is False and row["correct"] is True


def test_medication_truth_without_items_is_not_scored():
    row = compare(_med([]), GroundTruth(filename="med.png", doc_type="藥袋"))
    assert "fields.items" not in row["fields"] and row["correct"] is True and row["藥袋"] is None


# ---- 其他 ----------------------------------------------------------------------------

def test_other_types_use_invoice_definition():
    truth = GroundTruth(filename="x.png", doc_type="其他", date="2026-09-01", amount=100.0)
    row = compare(ExtractionResult(doc_type="其他", date="2026-09-01", amount=999.0), truth)
    assert row["correct"] is False and row["fields"]["amount"] is False


def test_invoice_definition_unchanged():
    truth = GroundTruth(filename="a.png", doc_type="發票", date="2026-07-04", amount=350.0,
                        invoice_number="AB12345678")
    row = compare(ExtractionResult(doc_type="發票", date="2026-07-04", amount=350.0, invoice_number="CD00000000",
                                   confidence=0.9), truth)
    assert row["correct"] is True and row["fields"]["invoice_number"] is False   # 字軌只是輔助
    assert set(row["fields"]) == {"doc_type", "date", "amount", "invoice_number"}
    assert row["藥袋"] is None and row["期限"] is None
