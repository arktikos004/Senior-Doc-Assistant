"""公文期限的起算基準(FIN-HARDEN 第二輪):「收到後 N 日內」「送達次日起」與固定日期分開看。

發文日起算只會偏早;讀不到發文日、改用上傳日起算可能偏晚,所以一律等家人確認;家人填了收到日期就從那天算。
文件是合成的,不呼叫模型。
"""
from datetime import date

import pytest

from src.actions import plan_actions
from src.dates import deadline_uses_base, resolve_deadline
from src.models import Decision, ExtractionResult
from src.parsing import to_result

UPLOADED = date(2026, 10, 1)
ARCHIVE, REVIEW = Decision(action="archive", reason=""), Decision(action="review", reason="")


def _letter(text, *, issued="2026-09-20", received=None):
    fields = {"subject": "請補繳文件", "deadline_text": text}
    if received:
        fields["received_date"] = received
    return ExtractionResult(doc_type="公文", date=issued, vendor="示範區公所", fields=fields)


def _plan(letter, decision=ARCHIVE, cfg=None):
    return plan_actions(letter, decision, cfg, received_on=UPLOADED)


# ---- 哪些原文要靠基準日 ---------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("收到本函後15日內", True),
    ("自送達之次日起30日內", True),
    ("文到7日內", True),
    ("自即日起30日內", True),
    ("於115年10月31日前", False),
    ("自115年10月1日起30日內", False),            # 寫明了從哪天起算,和收到日無關
    ("2026-10-31", False),
    ("", False),
    (None, False),
    ("請儘速辦理", False),
])
def test_which_deadlines_depend_on_the_base_date(text, expected):
    assert deadline_uses_base(text) is expected


# ---- 基準:發文日、上傳日、家人填的收到日 --------------------------------------------

@pytest.mark.parametrize("text, due", [("收到本函後15日內", "2026-10-05"), ("自送達之次日起30日內", "2026-10-20")])
def test_relative_deadline_counts_from_the_issue_date_when_it_can_be_read(text, due):
    """發文日起算只會偏早:可以照決策自動列入,但要寫明實際收到日可能較晚。"""
    letter = _letter(text)
    [action] = _plan(letter)
    assert action["payload"]["date"] == due and action["tier"] == "auto"
    assert "以發文日期 2026-09-20 起算" in action["payload"]["basis"]
    assert letter.fields["deadline"] == due and letter.fields["deadline_basis"] == action["payload"]["basis"]


@pytest.mark.parametrize("text", ["收到本函後15日內", "自送達之次日起30日內"])
def test_relative_deadline_without_issue_date_waits_for_family(text):
    """讀不到發文日,改從上傳日算可能偏晚:就算決策是存檔也要家人確認。"""
    [action] = _plan(_letter(text, issued=None))
    assert action["tier"] == "confirm"
    assert "以上傳日期 2026-10-01 起算" in action["payload"]["basis"]


def test_unparsable_issue_date_counts_as_no_issue_date():
    [action] = _plan(_letter("收到本函後15日內", issued="不是日期"))
    assert action["tier"] == "confirm" and action["payload"]["date"] == "2026-10-16"


@pytest.mark.parametrize("issued", ["2026-09-20", None])
def test_fixed_deadline_does_not_need_a_base(issued):
    """固定日期不靠基準日:照決策分級,不寫起算的說法。"""
    [action] = _plan(_letter("於115年10月31日前", issued=issued))
    assert action["payload"]["date"] == "2026-10-31" and action["tier"] == "auto"
    assert action["payload"]["basis"] == "" and "起算" not in action["payload"]["description"]


def test_received_date_from_family_replaces_the_base():
    letter = _letter("收到本函後15日內", received="2026-09-28")
    [action] = _plan(letter)
    assert action["payload"]["date"] == "2026-10-13" and action["tier"] == "auto"
    assert "以家人填的收到日期 2026-09-28 起算" in action["payload"]["basis"]


def test_received_date_also_settles_a_letter_without_issue_date():
    [action] = _plan(_letter("收到本函後15日內", issued=None, received="2026-09-28"))
    assert action["tier"] == "auto" and action["payload"]["date"] == "2026-10-13"


def test_review_decision_still_waits_for_family_with_any_base():
    [action] = _plan(_letter("收到本函後15日內"), REVIEW)
    assert action["tier"] == "confirm"


def test_received_date_after_a_fixed_deadline_keeps_the_reminder():
    """固定日期不看收到日:填了比期限晚的收到日,提醒也不能因為「期限早於基準日」而消失。"""
    [action] = _plan(_letter("於115年10月31日前", received="2026-11-05"))
    assert action["payload"]["date"] == "2026-10-31"


def test_old_basis_is_not_kept_when_nothing_can_be_computed():
    letter = _letter("請儘速辦理")
    letter.fields.update(deadline="2026-01-01", deadline_basis="舊的說法")
    assert _plan(letter) == [] and "deadline" not in letter.fields and "deadline_basis" not in letter.fields


def test_model_cannot_fill_in_the_received_date():
    """收到日期只有家人知道:模型回傳的一律濾掉,文件上的字改變不了起算基準(原則 2、3)。"""
    result = to_result({"doc_type": "公文", "date": "2026-09-20", "fields": {
        "subject": "請補繳文件", "deadline_text": "收到本函後15日內", "received_date": "2030-01-01",
        "deadline_basis": "以家人填的收到日期 2030-01-01 起算"}})
    assert "received_date" not in result.fields and "deadline_basis" not in result.fields
    [action] = _plan(result)
    assert action["payload"]["date"] == "2026-10-05"


def test_resolve_deadline_is_unchanged_for_the_base_it_is_given():
    assert resolve_deadline("收到本函後15日內", "2026-09-28") == date(2026, 10, 13)
    assert resolve_deadline("於115年10月31日前", "2026-09-28") == date(2026, 10, 31)
