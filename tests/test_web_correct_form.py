"""更正頁與改類型頁的表單規範(使用者 11/4 貼的表單互動規範,11/5 決定套用):共用的 ff 巨集、紅色星號、
框下只寫填寫要求、有格式的欄位離開就檢查、伺服器照樣檢查、錯誤寫在框的正下方、選項多的用下拉。

資料都是測試當場產生的,資料夾隔離在 tmp_path,不連網、不呼叫模型。
"""
import html as html_lib
import re

import pytest
from starlette.datastructures import FormData

from test_web_correct import _bag, _correct, browser_form
from test_web_static import _problems
from web.app import MSG_RETYPE_CHOOSE
from web.render import (
    BILL_KINDS,
    MSG_AMOUNT,
    MSG_CURRENCY,
    MSG_DAYS,
    MSG_INVOICE,
    MSG_REQUIRED,
    RETYPE_CHOOSE,
    _FORM_RULES,
    correction_values,
    parse_correction,
)

INVOICE = {"doc_type": "發票", "date": "2026-06-08", "vendor": "測試商店", "amount": 514.0, "currency": "NTD",
           "invoice_number": "QP94525678", "confidence": 0.95}
RECEIPT = {"doc_type": "收據", "date": "2026-06-08", "vendor": "測試商店", "amount": 514.0, "currency": "NTD",
           "invoice_number": "收-001", "confidence": 0.95}
BILL = {"doc_type": "帳單", "date": "2026-09-20", "vendor": "測試電力公司", "amount": 1286.0, "currency": "NTD",
        "confidence": 0.9, "fields": {"due_date": "2026-10-15", "bill_kind": "電費"}}
LETTER = {"doc_type": "公文", "date": "2026-09-25", "vendor": "測試區公所", "confidence": 0.9,
          "fields": {"subject": "請補送文件", "doc_number": "測字第1號", "deadline_text": "收到本函後15日內",
                     "required_actions": ["檢附身分證影本"]}}


def _page(client, doc_id: int) -> str:
    return client.get(f"/doc/{doc_id}/correct").text


def _ff(page: str, input_id: str) -> str:
    """那個輸入欄的整個 ff 區塊(框、標籤、灰字、錯誤)。"""
    found = re.search(rf'<div class="ff[^"]*">\s*<div class="ff__box">\s*<(?:input|select|textarea)[^>]*\bid="{input_id}"'
                      r".*?data-ff-error[^>]*>.*?</p>\s*</div>", page, re.S)
    assert found, input_id
    return found.group(0)


def _tag(page: str, input_id: str) -> str:
    """輸入框本身的開始標籤,屬性已經還原跳脫。"""
    found = re.search(rf'<(?:input|select|textarea)\b[^>]*\bid="{input_id}"[^>]*>', page, re.S)
    assert found, input_id
    return html_lib.unescape(found.group(0))


def _help(page: str, input_id: str) -> str | None:
    found = re.search(rf'<p class="ff__help" id="{input_id}-help">(.*?)</p>', page)
    return found.group(1) if found else None


# ---- 必填、固定標籤 ------------------------------------------------------------------

def test_required_fields_have_a_red_asterisk_and_the_word_is_gone(client, review_doc):
    page = _page(client, review_doc(BILL))
    assert '<span class="ff__req" aria-hidden="true">*</span><span class="visually-hidden">（必填）</span>' in _ff(page, "f-amount")
    assert "ff__req" in _ff(page, "f-fields-due_date")
    for optional in ("f-vendor", "f-date", "f-fields-bill_kind"):
        assert "ff__req" not in _ff(page, optional), optional
    assert 'class="req"' not in page and ">必填<" not in page and "（選填）" not in page


def test_labels_float_and_dates_dropdowns_and_long_text_stay_on_top(client, review_doc):
    page = _page(client, review_doc(LETTER))
    for fixed in ("f-date", "f-fields-received_date", "f-fields-required_actions", "f-category"):
        assert 'class="ff ff--fixed"' in _ff(page, fixed), fixed
    for floating in ("f-vendor", "f-fields-subject", "f-fields-deadline_text", "f-fields-doc_number"):
        assert 'class="ff"' in _ff(page, floating) and 'placeholder=" "' in _tag(page, floating), floating
    assert page.count('class="ff__label"') == page.count('class="ff') - page.count('class="ff__')   # 每個框一個標籤
    assert _problems(page) == []


def test_date_fields_are_real_date_inputs_without_a_placeholder(client, review_doc):
    tag = _tag(_page(client, review_doc(BILL)), "f-fields-due_date")
    assert 'type="date"' in tag and "placeholder" not in tag and f'data-error="{MSG_REQUIRED}"' in tag


# ---- 框下的灰字只寫填寫要求 --------------------------------------------------------------

@pytest.mark.parametrize("result, input_id, words", [
    (INVOICE, "f-invoice_number", "2 個英文字母加 8 位數字"),
    (INVOICE, "f-amount", "只寫數字"),
    (BILL, "f-amount", "只寫數字"),
    (INVOICE, "f-currency", "3 個英文字母，台幣填 NTD"),
    (LETTER, "f-fields-required_actions", "一行寫一項"),
    (_bag(), "m0-days", "只寫數字"),
])
def test_fields_with_a_format_say_it_under_the_box(client, review_doc, result, input_id, words):
    page = _page(client, review_doc(result))
    assert _help(page, input_id) == words
    assert f"{input_id}-help" in _tag(page, input_id).split('aria-describedby="')[1]    # 讀屏也念得到


@pytest.mark.parametrize("result, input_id", [
    (INVOICE, "f-vendor"), (INVOICE, "f-date"), (BILL, "f-fields-bill_kind"), (LETTER, "f-fields-subject"),
    (LETTER, "f-fields-deadline_text"), (LETTER, "f-fields-doc_number"), (RECEIPT, "f-invoice_number"),
    (_bag(), "m0-name"), (_bag(), "m0-usage"), (INVOICE, "f-category"),
])
def test_fields_without_a_format_have_no_grey_line(client, review_doc, result, input_id):
    assert _help(_page(client, review_doc(result)), input_id) is None


# ---- 離開欄位才檢查:pattern、inputmode、data-error ------------------------------------------

@pytest.mark.parametrize("result, input_id, kind, message", [
    (INVOICE, "f-invoice_number", "invoice", MSG_INVOICE),
    (INVOICE, "f-amount", "amount", MSG_AMOUNT),
    (INVOICE, "f-currency", "currency", MSG_CURRENCY),
    (_bag(), "m0-days", "number", MSG_DAYS),
])
def test_format_fields_are_checked_when_leaving_them(client, review_doc, result, input_id, kind, message):
    tag = _tag(_page(client, review_doc(result)), input_id)
    assert f'pattern="{_FORM_RULES[kind]["pattern"]}"' in tag and f'data-error="{message}"' in tag


def test_keyboards_match_the_field(client, review_doc):
    page = _page(client, review_doc(INVOICE))
    assert 'inputmode="decimal"' in _tag(page, "f-amount")
    assert 'autocapitalize="characters"' in _tag(page, "f-invoice_number")
    assert 'inputmode="numeric"' in _tag(_page(client, review_doc(_bag(), name="b.png")), "m0-days")


def test_required_text_is_checked_and_optional_text_is_not(client, review_doc):
    letter = _page(client, review_doc(LETTER))
    assert f'data-error="{MSG_REQUIRED}"' in _tag(letter, "f-fields-subject")
    for optional in ("f-vendor", "f-fields-deadline_text", "f-fields-doc_number", "f-fields-required_actions"):
        assert "data-error" not in _tag(letter, optional), optional
    assert f'data-error="{MSG_REQUIRED}"' in _tag(_page(client, review_doc(_bag(), name="b.png")), "m0-name")


def test_a_receipt_number_has_no_format_of_its_own(client, review_doc):
    """收據的號碼各家寫法不同:不套發票的格式,灰字、pattern、伺服器都不擋。"""
    doc_id = review_doc(RECEIPT)
    tag = _tag(_page(client, doc_id), "f-invoice_number")
    assert "pattern" not in tag and "data-error" not in tag
    assert _correct(client, doc_id, invoice_number="收-002").status_code == 303


# pattern 要和伺服器收的寫法一致:伺服器收的不能在離開欄位時被說錯,伺服器不收的也要說
@pytest.mark.parametrize("kind, doc_type, key, good, bad", [
    ("amount", "發票", "amount", ["1286", "1,286", " 1286 ", "1286.5", "１２８６", "1，286", "12 345"],
     ["一千", "1286元", "NT$100", "12.", "-5", "1.2.3"]),
    ("invoice", "發票", "invoice_number", ["AB12345678", "ab12345678", "AB-12345678", "AB 12345678", "ＡＢ１２３４５６７８", " AB12345678 "],
     ["AB1234567", "A12345678", "ABC12345678", "1234567890", "AB123456789", "發票12345678"]),
    ("currency", "發票", "currency", ["NTD", "usd", " USD "], ["NT", "美金元", "US1", "ABCD"]),
])
def test_patterns_agree_with_the_server(kind, doc_type, key, good, bad):
    pattern = re.compile(_FORM_RULES[kind]["pattern"])
    result = {**INVOICE, "doc_type": doc_type}

    def server_errors(text):
        return parse_correction(result, FormData(list({**correction_values(result), key: text}.items())))[2]

    for text in good:
        assert pattern.fullmatch(text), text
        assert key not in server_errors(text), text
    for text in bad:
        assert not pattern.fullmatch(text), text


def test_days_pattern_agrees_with_the_server():
    pattern = re.compile(_FORM_RULES["number"]["pattern"])
    for text in ("7", "７", " 30 "):
        assert pattern.fullmatch(text), text
    for text in ("七", "7天", "1.5", "-1"):
        assert not pattern.fullmatch(text), text
    bag = _bag()
    values = correction_values(bag)["items"][0]
    form = FormData([("items-0-name", values["name"]), ("items-0-days", "７"), ("date", bag["date"])])
    assert parse_correction(bag, form)[2] == {}


# ---- 伺服器照樣檢查,錯誤寫在框的正下方 --------------------------------------------------------

@pytest.mark.parametrize("wrong", ["AB1234567", "12345678", "發票號碼", "AB12345678X"])
def test_the_server_refuses_a_malformed_invoice_number(client, store, review_doc, wrong):
    doc_id = review_doc(INVOICE)
    r = _correct(client, doc_id, invoice_number=wrong)
    assert r.status_code == 400 and "有 1 個地方要修改" in r.text
    box = _ff(r.text, "f-invoice_number")
    assert "ff--error" in box and f"<span data-ff-error-text>{MSG_INVOICE}</span>" in box
    assert box.index('class="ff__help"') < box.index('class="ff__error"')          # 灰字下面就是錯誤
    assert f'value="{wrong}"' in _tag(r.text, "f-invoice_number")                  # 已填的留著
    assert 'aria-invalid="true"' in _tag(r.text, "f-invoice_number") and "autofocus" in _tag(r.text, "f-invoice_number")
    assert store.get_document(doc_id)["result"]["invoice_number"] == "QP94525678"  # 沒有存


@pytest.mark.parametrize("typed, stored", [("AB12345678", "AB12345678"), ("AB-12345678", "AB-12345678"),
                                           ("ＡＢ１２３４５６７８", "AB12345678"), ("", None)])
def test_good_invoice_numbers_are_saved(client, store, review_doc, typed, stored):
    doc_id = review_doc(INVOICE)
    assert _correct(client, doc_id, invoice_number=typed).status_code == 303       # 選填:空白可以
    assert store.get_document(doc_id)["result"]["invoice_number"] == stored


def test_no_error_means_the_error_line_is_hidden(client, review_doc):
    box = _ff(_page(client, review_doc(INVOICE)), "f-amount")
    assert re.search(r'<p class="ff__error" id="f-amount-error" data-ff-error hidden>', box) and "ff--error" not in box


def test_server_errors_sit_under_the_box_they_belong_to(client, review_doc):
    doc_id = review_doc(BILL)
    r = _correct(client, doc_id, amount="一千")
    box = _ff(r.text, "f-amount")
    assert "ff--error" in box and MSG_AMOUNT in box
    assert box.index('class="ff__box"') < box.index(MSG_AMOUNT)
    assert 'id="f-amount-error"' in box and 'aria-invalid="true"' in _tag(r.text, "f-amount")
    assert "ff--error" not in _ff(r.text, "f-vendor")


def test_the_first_error_on_screen_gets_the_focus(client, review_doc):
    """有問題的欄位排前面:焦點照畫面順序,不是表單原本的順序。"""
    doc_id = review_doc({**INVOICE, "unreadable": ["amount"]})                     # 金額排到日期前面
    data = browser_form(_page(client, doc_id), f"/doc/{doc_id}/correct")
    page = client.post(f"/doc/{doc_id}/correct", data={**data, "date": "", "amount": ""}).text
    assert "autofocus" in _tag(page, "f-amount") and "autofocus" not in _tag(page, "f-date")


# ---- 選項多的用下拉 ------------------------------------------------------------------------

def _options(page: str, name: str) -> list[str]:
    select = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', page, re.S).group(1)
    return re.findall(r'<option value="([^"]*)"', select)


def test_the_category_is_a_dropdown_on_the_correction_page(client, review_doc):
    page = _page(client, review_doc(BILL))
    assert _options(page, "category") == ["身分證明", "財產資產", "醫療與保險", "生活契約", "未分類"]
    assert 'type="radio"' not in page
    assert '<label class="ff__label" for="f-category">這份文件屬於哪一類' in page


def test_the_bill_kind_is_a_dropdown_that_keeps_the_reading_when_nothing_matches(client, store, review_doc):
    page = _page(client, review_doc(BILL))
    assert _options(page, "fields.bill_kind") == list(BILL_KINDS) and '<option value="電費" selected>' in page
    unknown = review_doc({**BILL, "fields": {"due_date": "2026-10-15", "bill_kind": "管理費"}}, name="u.png")
    page = _page(client, unknown)
    assert _options(page, "fields.bill_kind") == ["", *BILL_KINDS] and '<option value="" selected>請選擇</option>' in page
    assert _correct(client, unknown, vendor="測試公司").status_code == 303
    assert store.get_document(unknown)["result"]["fields"]["bill_kind"] == "管理費"   # 沒選:維持原讀值


def test_medicine_times_stay_multiple_choice_checkboxes(client, review_doc):
    page = _page(client, review_doc(_bag()))
    for slot in ("早", "中", "晚"):
        assert re.search(rf'type="checkbox" name="items-0-timing" value="{slot}" checked', page)
    assert re.search(r'type="checkbox" name="items-0-timing" value="睡前">', page)


# ---- 改類型頁 ------------------------------------------------------------------------------

def _retype(client, doc_id: int) -> str:
    return client.get(f"/doc/{doc_id}/retype").text


def test_retype_uses_the_shared_dropdown(client, review_doc):
    page = _retype(client, review_doc(BILL))
    assert _options(page, "doc_type") == ["", "發票", "收據", "藥袋", "公文", "其他"]
    box = _ff(page, "f-doc_type")
    assert 'class="ff ff--fixed"' in box and "這份文件其實是" in box and "ff__req" not in box   # 只有一個欄位,不標必填
    tag = _tag(page, "f-doc_type")
    assert "required" in tag and f'data-error="{RETYPE_CHOOSE}"' in tag and "autofocus" not in tag
    assert 'type="radio"' not in page and _problems(page) == []
    assert RETYPE_CHOOSE == MSG_RETYPE_CHOOSE                                      # 離開欄位與伺服器說同一句


def test_retype_without_a_choice_says_so_under_the_box(client, review_doc):
    doc_id = review_doc(BILL)
    r = client.post(f"/doc/{doc_id}/retype", data={"doc_type": ""})
    assert r.status_code == 400
    box = _ff(r.text, "f-doc_type")
    assert "ff--error" in box and f"<span data-ff-error-text>{MSG_RETYPE_CHOOSE}</span>" in box
    tag = _tag(r.text, "f-doc_type")
    assert 'aria-invalid="true"' in tag and "autofocus" in tag and 'class="field-error"' not in r.text


def test_retype_form_sends_the_chosen_type_as_a_browser_would(client, review_doc):
    doc_id = review_doc(BILL)
    data = browser_form(_retype(client, doc_id), f"/doc/{doc_id}/retype")
    assert data["doc_type"] == ""                                                  # 還沒選


# ---- v5 車站時刻表(11/6):表單底部的按鈕一樣寬、置中;退回隔開;新配色 ------------------------------

def _t3_css() -> str:
    from web.render import STATIC_DIR
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    return css.split("/* ---- T3 家人確認、待複核、更正 ---- */")[1].split("/* (T3 家人確認、待複核、更正的規則寫在這一行上面) */")[0]


def test_buttons_at_the_end_of_my_forms_are_the_same_width():
    """更正頁、改類型、PDF 密碼:底部的按鈕一樣寬(最寬那顆的寬度)、整組置中;手機上下排、撐滿。
    只套在這幾張表單(.review-form、.pw-form),設定頁等別的表單不受影響。"""
    rule = re.search(r"\.review-form \.form-actions, \.pw-form \.form-actions \{[^}]*\}", _t3_css()).group(0)
    assert "grid-auto-flow: column" in rule and "grid-auto-columns: 1fr" in rule and "width: fit-content" in rule
    assert "margin-inline: auto" in rule


def test_reject_on_the_correction_page_is_kept_apart():
    """退回沒有復原:和「儲存更正並重新核對」那一組按鈕隔開一段距離(同結果頁的 .end-action)。"""
    assert re.search(r"\.compare__main \.reject-form \{[^}]*margin-top: 3rem", _t3_css())


def test_the_confirm_dialog_uses_the_new_ink():
    """確認框換成新配色:陰影與遮罩是深墨(#14212B),舊的深藍(14, 27, 46)不留。"""
    from web.render import STATIC_DIR
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    dialog = css.split("/* ---- 確認框")[1].split("/* ---- 文件櫃")[0]
    assert "rgba(14, 27, 46" not in css and "rgba(20, 33, 43" in dialog
