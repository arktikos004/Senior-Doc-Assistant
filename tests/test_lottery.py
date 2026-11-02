"""統一發票對獎的純函式:期別、號碼正規化、獎項規則(只算最高的一個)、表單檢查、領獎提醒的內容。

號碼都是合成的;不連網。
"""
from datetime import date

import pytest

from src.actions import CALENDAR
from src.lottery import (
    NO_CLAIM_NOTE,
    REMINDER_TITLE,
    Numbers,
    draw_date,
    latest_drawn,
    match,
    normalize_number,
    parse_form,
    parse_period,
    period_label,
    period_of,
    qr_buyer_has_tax_id,
    reminder_action,
)
from src.models import ExtractionResult
from src.verify import einvoice

NUMBERS = Numbers(special="12345678", grand="87654321", first=("11112222", "33334444", "55556678"),
                  claim_by=date(2027, 3, 5))
FORM = {"special": "12345678", "grand": "87654321", "first1": "11112222", "first2": "33334444",
        "first3": "55556678", "claim_by": "2027-03-05"}


@pytest.mark.parametrize("day, period", [
    (date(2026, 10, 10), "115-09"), (date(2026, 9, 1), "115-09"), (date(2026, 1, 15), "115-01"),
    (date(2026, 12, 31), "115-11"), (date(2025, 8, 3), "114-07"),
])
def test_period_of(day, period):
    assert period_of(day) == period


def test_period_helpers():
    assert parse_period("115-09") == (2026, 9)
    assert parse_period("115-10") is None and parse_period("9-10") is None and parse_period(None) is None
    assert period_label("115-09") == "115 年 9、10 月期"
    assert draw_date("115-09") == date(2026, 11, 25)
    assert draw_date("115-11") == date(2027, 1, 25)                    # 11、12 月期在隔年 1 月開獎


@pytest.mark.parametrize("today, period", [
    (date(2026, 10, 29), "115-07"), (date(2026, 11, 25), "115-09"), (date(2026, 11, 24), "115-07"),
    (date(2026, 1, 10), "114-09"), (date(2026, 1, 25), "114-11"),
])
def test_latest_drawn(today, period):
    assert latest_drawn(today) == period


@pytest.mark.parametrize("text, expected", [
    ("12345678", "12345678"), ("１２３４５６７８", "12345678"), (" 1234-5678 ", "12345678"),
    ("1234567", None), ("123456789", None), ("abcdefgh", None), ("", None), (None, None),
])
def test_normalize_number(text, expected):
    assert normalize_number(text) == expected


@pytest.mark.parametrize("invoice, prize, detail", [
    ("AB12345678", "特別獎", "8 碼和特別獎號碼一樣"),
    ("AB87654321", "特獎", "8 碼和特獎號碼一樣"),
    ("AB33334444", "頭獎", "8 碼和頭獎第 2 組一樣"),
    ("AB91112222", "二獎", "末 7 碼和頭獎第 1 組一樣"),
    ("AB99112222", "三獎", "末 6 碼和頭獎第 1 組一樣"),
    ("AB99934444", "四獎", "末 5 碼和頭獎第 2 組一樣"),
    ("AB99996678", "五獎", "末 4 碼和頭獎第 3 組一樣"),
    ("QP94525678", "六獎", "末 3 碼和頭獎第 3 組一樣"),
])
def test_prize_rules(invoice, prize, detail):
    hit = match(invoice, NUMBERS)
    assert (hit.prize.name, hit.detail) == (prize, detail)


def test_only_the_highest_prize_counts():
    # 末 3 碼對到第 1 組(六獎),末 5 碼對到第 3 組(四獎):算四獎,不管組的順序
    numbers = Numbers("00000001", "00000002", ("77777678", "00000003", "99912678"), date(2027, 3, 5))
    hit = match("AB55512678", numbers)
    assert (hit.prize.name, hit.prize.amount) == ("四獎", 4_000)


@pytest.mark.parametrize("invoice", ["AB00000000", "AB1234567", "12345678", "ab12345678", "", None])
def test_no_prize(invoice):
    assert match(invoice, NUMBERS) is None


def test_form_is_checked_field_by_field():
    numbers, errors = parse_form({**FORM, "first2": "３３３３-４４４４"}, "115-09")
    assert errors == {} and numbers == NUMBERS                          # 全形數字、連字號照樣收
    numbers, errors = parse_form({**FORM, "special": "1234", "first3": ""}, "115-09")
    assert numbers is None and set(errors) == {"special", "first3"}
    assert errors["special"] == "請輸入公告上的 8 碼數字"


@pytest.mark.parametrize("claim_by", ["2026-11-25", "2026-10-01", "2028-03-05", "", "明年三月"])
def test_claim_deadline_must_come_after_the_draw(claim_by):
    numbers, errors = parse_form({**FORM, "claim_by": claim_by}, "115-09")
    assert numbers is None and "claim_by" in errors


def test_numbers_survive_a_round_trip_and_bad_values_are_ignored():
    assert Numbers.from_json(NUMBERS.to_json()) == NUMBERS
    assert Numbers.from_json(None) is None and Numbers.from_json("{") is None
    assert Numbers.from_json('{"special": "12345678", "grand": "87654321", "first": ["11112222"], '
                             '"claim_by": "2027-03-05"}') is None       # 頭獎要三組


def test_reminder_content_is_built_by_the_program():
    action = reminder_action("QP94525678", match("QP94525678", NUMBERS), NUMBERS, "115-09")
    assert (action["kind"], action["tier"]) == (CALENDAR, "auto")
    payload = action["payload"]
    assert payload["title"] == REMINDER_TITLE and payload["date"] == "2027-03-05"
    assert payload["source"] == "lottery" and payload["period"] == "115-09"
    lines = payload["description"].splitlines()
    assert lines[0] == "發票號碼：QP-94525678" and lines[1] == "對獎：115 年 9、10 月期"
    assert lines[2] == "可能中了六獎（200 元）：末 3 碼和頭獎第 3 組一樣"
    assert "116年3月5日前" in lines[3] and lines[-1] == NO_CLAIM_NOTE


# --- 打統編的發票不能兌獎:看 QR 記載的買方統編 ---

def _qr_checked(qr_buyer: str, model_buyer: str) -> dict:
    """用真的核對程式(einvoice.compare_with_result)產生核對結果;QR 與讀值都是合成的。"""
    left = einvoice.build_left_qr("ZX10293847", date(2026, 9, 4), "5847", 333, 350, qr_buyer, "04595257",
                                  "SyntheticTestOnly0000A==")
    result = ExtractionResult(doc_type="發票", date="2026-09-04", amount=350.0, invoice_number="ZX10293847",
                              fields={"buyer_tax_id": model_buyer, "seller_tax_id": "04595257", "random_code": "5847"})
    return {"doc_type": "發票", "verification": einvoice.compare_with_result(einvoice.parse_left_qr(left), result)}


@pytest.mark.parametrize("qr_buyer, model_buyer, has_tax_id", [
    ("00000000", "", False),            # 一般消費者
    ("00000000", "12345675", False),    # 模型多讀了一組統編,QR 記載的是一般消費者
    ("04595257", "", True),             # 打統編,模型沒讀到
    ("04595257", "04595257", True),     # 打統編,模型讀對
    ("04595257", "12345675", True),     # 打統編,模型讀錯
])
def test_buyer_tax_id_comes_from_the_qr(qr_buyer, model_buyer, has_tax_id):
    assert qr_buyer_has_tax_id(_qr_checked(qr_buyer, model_buyer)) is has_tax_id


def test_buyer_tax_id_is_unknown_without_a_qr():
    assert qr_buyer_has_tax_id({"doc_type": "發票", "verification": {}}) is None
    assert qr_buyer_has_tax_id({"doc_type": "發票", "verification": "fail"}) is None
    assert qr_buyer_has_tax_id(None) is None
