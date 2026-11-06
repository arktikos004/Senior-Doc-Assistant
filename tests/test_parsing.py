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


# ---- 藥袋:只印次數的藥,不留模型推測的時段(使用者 11/4) ------------------------------------------

def _bag(*items):
    return to_result({"doc_type": "藥袋", "vendor": "範例診所", "fields": {"items": list(items)}})


@pytest.mark.parametrize("printed", ["每日三次", "一天 2 次", "3次/日", "TID", "每日三次 飯後"])
def test_counts_without_slot_words_drop_the_guessed_timing(printed):
    """系統只照抄藥袋,不把「一天三次」換算成早、中、晚(Non-Goal 1):只印次數時,模型標的時段清掉。"""
    result = _bag({"name": "範例藥甲", "frequency_text": printed, "timing": ["早", "中", "晚"], "prn": False})
    assert result.fields["items"][0]["timing"] == []
    assert result.raw["fields"]["items"][0]["timing"] == ["早", "中", "晚"]      # 模型原本的回答還在


@pytest.mark.parametrize("printed, timing", [("早晚各一次", ["早", "晚"]), ("每日三次 早午晚飯後", ["早", "中", "晚"]),
                                             ("睡前一粒", ["睡前"]), ("飯後", ["早", "晚"]), ("", ["早"])])
def test_printed_slots_or_no_count_keep_the_timing(printed, timing):
    """印了時段字的照留;沒印次數也沒印時段的(時段可能在勾選格)看不出來,也照留,交給核對與家人確認。"""
    result = _bag({"name": "範例藥乙", "frequency_text": printed, "timing": timing})
    assert result.fields["items"][0]["timing"] == timing


def test_the_count_can_be_in_the_dose_text_and_other_types_are_untouched():
    result = _bag({"name": "範例藥丙", "dose_text": "每日兩次，每次一粒", "timing": ["早", "晚"]},
                  {"name": "範例藥丁", "frequency_text": "需要時", "timing": [], "prn": True})
    assert [item["timing"] for item in result.fields["items"]] == [[], []]
    bill = to_result({"doc_type": "帳單", "fields": {"items": [{"frequency_text": "每日三次", "timing": ["早"]}]}})
    assert "items" not in bill.fields


def test_after_dropping_the_check_has_nothing_left_to_flag():
    """清掉後核對看不到推測的時段(不再因此轉人工);服藥時間表把這種藥列在「未標時段」,一律等家人確認。"""
    from src.actions.medication import build_schedule
    from src.verify.checks import check_medication_timing

    result = _bag({"name": "範例藥甲", "frequency_text": "每日三次", "timing": ["晚"], "prn": False, "days": 7})
    assert check_medication_timing(result.fields["items"])["status"] == "skip"
    schedule = build_schedule(result)
    assert [i["name"] for i in schedule["unscheduled"]] == ["範例藥甲"] and schedule["slots"] == []
