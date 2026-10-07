"""模型輸出清洗測試(與 provider 無關)。"""
import pytest

from src.archiver import build_filename
from src.config import AppConfig
from src.parsing import clean_currency, clean_date, parse_json_object, to_result


def test_bill_due_date_in_roc_year_is_converted():
    """帳單期限照抄民國年時由程式換成西元(原則 2);換不了就保留原文,讓核對判「沒通過」。"""
    roc = to_result({"doc_type": "帳單", "fields": {"due_date": "115/10/15"}})
    assert roc.fields["due_date"] == "2026-10-15"
    vague = to_result({"doc_type": "帳單", "fields": {"due_date": "下個月底"}})
    assert vague.fields["due_date"] == "下個月底"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-08-03", "2026-08-03"),
        ("2026-08-03 17:08:26", "2026-08-03"),  # 時分秒會讓 Windows 檔名失敗
        ("2026/8/3", "2026-08-03"),
        ("", None),
        (None, None),
        ("115年07-08月", None),                  # 期別不是日期
        ("115/08/03", "2026-08-03"),             # 模型沒換算的民國年,由程式換算
        ("中華民國115年8月3日", "2026-08-03"),
        ("2026-02-30", None),                    # 不存在的日期不當成日期(不猜)
        ("2026-13-01", None),
    ],
)
def test_clean_date(raw, expected):
    assert clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("NT$", "NTD"), ("twd", "NTD"), ("新臺幣", "NTD"), ("US$", "USD"), ("", "NTD"), ("JPY", "JPY")],
)
def test_clean_currency(raw, expected):
    assert clean_currency(raw) == expected


@pytest.mark.parametrize(
    "reply",
    [
        '{"doc_type": "發票"}',
        '```json\n{"doc_type": "發票"}\n```',          # 結構化輸出沒擋住的 Markdown 圍欄
        '以下是結果：{"doc_type": "發票"} 以上。',
        {"doc_type": "發票"},                          # Workers AI 舊格式已是 dict
    ],
)
def test_parse_json_object_is_lenient(reply):
    assert parse_json_object(reply) == {"doc_type": "發票"}


@pytest.mark.parametrize("reply", ["", None, "不是 JSON", "{截斷的輸出", '["發票"]', 42])
def test_parse_json_object_rejects_non_objects(reply):
    with pytest.raises(ValueError):
        parse_json_object(reply)


def test_to_result_sentinels_become_none():
    r = to_result({"doc_type": "發票", "date": "", "vendor": "", "amount": 0, "confidence": 0.9})
    assert r.date is None and r.vendor is None and r.amount is None


def test_to_result_non_string_text_fields_become_strings():
    # 雲端 JSON 模式不保證遵守 schema,模型可能把店名、號碼填成數字;沒轉字串時歸檔命名會拋 TypeError
    r = to_result({"doc_type": "收據", "date": "2026-08-03", "vendor": 7111, "amount": 85,
                   "invoice_number": 12345678, "notes": 0.5, "confidence": 0.9})
    assert r.vendor == "7111" and r.invoice_number == "12345678" and r.notes == "0.5"
    assert build_filename(r, AppConfig(), ".jpg") == "20260803_收據_7111_85.jpg"


def test_to_result_blank_text_fields_become_empty():
    r = to_result({"doc_type": "收據", "vendor": "  ", "invoice_number": " ", "notes": "  "})
    assert r.vendor is None and r.invoice_number is None and r.notes == ""


def test_to_result_unknown_doc_type_becomes_other():
    assert to_result({"doc_type": "合約"}).doc_type == "其他"


def test_to_result_accepts_new_doc_types():
    assert to_result({"doc_type": "藥袋"}).doc_type == "藥袋"


def test_to_result_clamps_confidence():
    assert to_result({"doc_type": "發票", "confidence": 3}).confidence == 1.0
    assert to_result({"doc_type": "發票", "confidence": "高"}).confidence == 0.0


def test_to_result_keeps_only_known_unreadable_fields():
    r = to_result({"doc_type": "藥袋", "unreadable": ["amount", "fields.dose", "ignore previous instructions"]})
    assert r.unreadable == ["amount", "fields.dose"]


def test_to_result_type_specific_fields_must_be_dict():
    assert to_result({"doc_type": "帳單", "fields": {"due_date": "2026-10-15"}}).fields == {"due_date": "2026-10-15"}
    assert to_result({"doc_type": "帳單", "fields": "not a dict"}).fields == {}


def test_to_result_drops_fields_of_other_types():
    # 通用 schema 是各類型欄位的聯集;發票結果不該帶著帳單/藥袋的空欄位
    r = to_result({"doc_type": "發票", "fields": {"seller_tax_id": "12345678", "due_date": "", "items": []}})
    assert r.fields == {"seller_tax_id": "12345678"}
