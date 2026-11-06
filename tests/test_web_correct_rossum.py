"""更正頁改版(參考 Rossum 的人工核對畫面,使用者 11/4):原件與欄位並排、有問題的欄位排前面並寫出原因、
一次聚焦一個問題、手機展開原件、按鈕寫清楚結果、存好之後寫出改了什麼。

資料全是合成的,資料夾隔離在 tmp_path,不連網、不呼叫模型。
"""
import json
import re
import subprocess
from datetime import date, timedelta
from urllib.parse import quote

import pytest
from starlette.datastructures import FormData

from test_web_correct import _bag, _correct, browser_form
from test_web_static import _NODE, _problems
from web.render import (
    STATIC_DIR,
    change_note,
    change_param,
    changed_fields,
    correct_view,
    correction_issues,
    correction_values,
    parse_correction,
)

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
# 相對今天:期限不合理(早於開單日、晚於 2 年)不會產生提醒,合成資料的日期不能過期
ISSUED = date.today() - timedelta(days=2)
DUE, NEW_DUE = (ISSUED + timedelta(days=20)).isoformat(), (ISSUED + timedelta(days=25)).isoformat()

INVOICE = {"doc_type": "發票", "date": "2026-06-08", "vendor": "合成示範超市", "amount": 514.0, "currency": "NTD",
           "invoice_number": "QP94525678", "confidence": 0.95, "verified_confidence": 0.10}
BILL = {"doc_type": "帳單", "date": ISSUED.isoformat(), "vendor": "合成電力公司", "amount": 1286.0, "currency": "NTD",
        "confidence": 0.9, "fields": {"due_date": DUE, "bill_kind": "電費"}}
LETTER = {"doc_type": "公文", "date": "2026-09-25", "vendor": "合成區公所", "confidence": 0.9,
          "fields": {"subject": "請補送敬老卡申請文件", "doc_number": "合成字第1150000001號",
                     "deadline_text": "收到本函後15日內", "required_actions": ["檢附身分證影本"]}}
QR_FAIL = {"status": "fail", "detail": "QR 記載總計 244，辨識金額為 514", "fields": ["amount"],
           "expected": {"amount": 244}}
BARCODE_FAIL = {"status": "fail", "detail": "條碼上的應繳金額是 1,286 元，讀到的卻是 1,236 元", "fields": ["amount"],
                "expected": {"amount": 1286}}
DUE_FAIL = {"status": "fail", "detail": "繳費期限 2026-01-01 早於出帳日 2026-09-20，不合理",
            "fields": ["fields.due_date"]}
ITEMS_FAIL = {"status": "fail", "detail": "藥品清單不完整：第 2 項沒有讀到用法", "fields": ["fields.items"]}
GROUP_PROBLEMS = "有問題的欄位（"
NO_PROBLEM = "這張表單上的欄位沒有標出問題，請對照原件確認。"


def _invoice(**overrides) -> dict:
    return {**INVOICE, **overrides}


def _bill(**overrides) -> dict:
    return {**BILL, "fields": dict(BILL["fields"]), **overrides}


@pytest.fixture
def archived(cfg, add_doc, png_bytes):
    """回傳 archived(result, name=...) → 文件 ID:已存檔,原檔放在 archive/。"""
    def add(result: dict, name: str = "20261001_帳單_合成電力公司_1286.png") -> int:
        path = cfg.paths.archive / name
        path.write_bytes(png_bytes())
        return add_doc(result, target=path)
    return add


def _page(client, doc_id: int) -> str:
    return client.get(f"/doc/{doc_id}/correct").text


def _form(page: str, doc_id: int) -> str:
    return re.search(rf'<form\b[^>]*action="/doc/{doc_id}/correct"[^>]*>(.*?)</form>', page, re.S).group(1)


def _order(page: str, doc_id: int) -> list[str]:
    """表單上各欄依畫面順序的錨點(p-amount…),有問題的欄位應該排在最前面。"""
    return re.findall(r'\bid="p-([^"]+)"', _form(page, doc_id))


def _autofocused(page: str) -> list[str]:
    """畫面上自動取得焦點的輸入欄 id(base.html 的確認框有自己的 autofocus,不算)。"""
    tags = re.findall(r"<(?:input|textarea)\b[^>]*\bautofocus\b[^>]*>", page, re.S)
    return [re.search(r'\bid="([^"]+)"', tag).group(1) for tag in tags]


def _issues_of(page: str, input_id: str) -> list[tuple[str, str]]:
    """那一欄的原因:[(樣式, 文字)],寫在收合摘要的那一行;輸入欄的 aria-describedby 要接到這一串。"""
    found = re.search(rf'<span id="{input_id}-issue">((?:<span class="issue issue--\w+">[^<]*</span>；?)*)</span>', page)
    return re.findall(r'<span class="issue issue--(\w+)">([^<]*)</span>', found.group(1)) if found else []


def _note(page: str) -> str | None:
    """結果頁上方「改了什麼」那一句,沒有就是 None。"""
    found = re.search(r'<p class="msg-change">(.*?)</p>', page, re.S)
    return found.group(1) if found else None


def _saved_note(client, response) -> str | None:
    assert response.status_code == 303
    return _note(client.get(response.headers["location"]).text)


# ---- 有問題的欄位排前面、寫出原因(資料來源是 verification 與 unreadable) ----------------------

@pytest.mark.parametrize("result, input_id, anchor, reason, tone", [
    (_invoice(verification={"QR 總計額": QR_FAIL}), "f-amount", "amount", "和發票上的 QR Code 不符", "fail"),
    (_bill(amount=1236.0, verification={"條碼應繳金額": BARCODE_FAIL}), "f-amount", "amount", "和帳單上的超商條碼不符", "fail"),
    (_invoice(unreadable=["vendor"]), "f-vendor", "vendor", "AI 讀不清楚", "wait"),
    (_bill(unreadable=["due_date"]), "f-fields-due_date", "fields-due_date", "AI 讀不清楚", "wait"),       # 類型專屬欄位的另一種寫法
    (_invoice(amount=None), "f-amount", "amount", "AI 沒有讀到", "wait"),
    (_bill(verification={"繳費期限": DUE_FAIL}), "f-fields-due_date", "fields-due_date",
     "檢查沒通過：繳費期限 2026-01-01 早於出帳日 2026-09-20，不合理", "fail"),
], ids=["QR 不符", "條碼不符", "讀不清", "讀不清的類型專屬欄位", "沒讀到", "檢查沒通過"])
def test_flagged_field_says_why_comes_first_and_has_focus(client, review_doc, result, input_id, anchor, reason, tone):
    doc_id = review_doc(result)
    page = _page(client, doc_id)
    assert _issues_of(page, input_id) == [(tone, reason)]
    assert re.search(rf'id="{input_id}"[^>]*aria-describedby="[^"]*{input_id}-issue', page)   # 讀屏也念得到原因
    assert _order(page, doc_id)[0] == anchor
    assert _autofocused(page) == [input_id]
    assert f"{GROUP_PROBLEMS}1）" in page and "其他欄位" in page and NO_PROBLEM not in page
    assert _problems(page) == []


def test_flagged_fields_keep_the_form_order_and_the_rest_follow(client, review_doc):
    doc_id = review_doc(_invoice(amount=None, unreadable=["vendor"], verification={
        "字軌格式": {"status": "fail", "detail": "發票號碼不是兩個英文字母加八位數字", "fields": ["invoice_number"]}}))
    page = _page(client, doc_id)
    assert _order(page, doc_id) == ["vendor", "amount", "invoice_number", "date", "currency"]
    form = _form(page, doc_id)
    assert form.index(f"{GROUP_PROBLEMS}3）") < form.index('id="p-vendor"')
    assert form.index('id="p-invoice_number"') < form.index("其他欄位") < form.index('id="p-date"')
    assert _autofocused(page) == ["f-vendor"]
    assert form.count("<details class=\"issue-fold\"") == 3              # 沒問題的欄位不收合


def test_one_field_can_have_several_reasons_in_a_fixed_order():
    result = _bill(amount=1236.0, unreadable=["amount"], verification={
        "金額合理性": {"status": "fail", "detail": "金額超過家用文件的合理範圍", "fields": ["amount"]},
        "條碼應繳金額": BARCODE_FAIL})
    assert [i["text"] for i in correction_issues(result)["amount"]] == [
        "和帳單上的超商條碼不符", "AI 讀不清楚", "檢查沒通過：金額超過家用文件的合理範圍"]


def test_unreadable_wins_over_missing_and_only_required_fields_count_as_missing():
    issues = correction_issues(_invoice(amount=None, vendor=None, invoice_number=None, unreadable=["amount"]))
    assert [i["text"] for i in issues["amount"]] == ["AI 讀不清楚"]    # 讀不清的不再說沒讀到
    assert "vendor" not in issues and "invoice_number" not in issues   # 選填欄位空白不算有問題:文件上可能本來就沒有


def test_nothing_flagged_says_so_without_claiming_it_is_correct(client, archived):
    doc_id = archived(_bill())
    page = _page(client, doc_id)
    assert NO_PROBLEM in page and GROUP_PROBLEMS not in page and "其他欄位" not in page
    assert _autofocused(page) == [] and "data-next-issue" not in page and 'class="peek"' not in page
    for wrong in ("都正確", "沒有問題", "核對通過"):                    # 只說沒有標出,不說成讀對(原則 1、7)
        assert wrong not in _form(page, doc_id)
    assert _order(page, doc_id) == ["fields-bill_kind", "fields-due_date", "amount", "vendor", "date"]   # 照原本的順序


def test_the_models_self_rating_never_flags_a_field(client, review_doc):
    """AI 自評再低、驗證信心再低,只要核對沒有不通過、也沒有讀不清,就不算有問題(原則 1);原因留在頁首。"""
    doc_id = review_doc(_invoice(confidence=0.05, verified_confidence=0.05))
    page = _page(client, doc_id)
    assert NO_PROBLEM in page and GROUP_PROBLEMS not in page
    assert "AI 對這份文件的讀值沒有把握" in page


@pytest.mark.parametrize("check", [
    {"status": "pass", "detail": "相符", "fields": ["amount"]},
    {"status": "skip", "detail": "讀不到原檔影像", "fields": ["amount"]},
])
def test_passed_or_skipped_checks_do_not_flag(check):
    assert correction_issues(_invoice(verification={"QR 總計額": check})) == {}


def test_checks_on_fields_off_the_form_stay_in_the_page_reason(client, review_doc):
    """賣方統編不在更正表單上:不掛在欄位上,原因由頁首那句說明;畫面不會說有欄位要改卻找不到。"""
    doc_id = review_doc(_invoice(verification={
        "QR 賣方統編": {"status": "fail", "detail": "QR 記載賣方統編 04595257", "fields": ["fields.seller_tax_id"]}}))
    page = _page(client, doc_id)
    assert NO_PROBLEM in page and "讀到的賣方統編和發票上的 QR Code 不一樣" in page


@pytest.mark.parametrize("verification", [None, "壞掉的資料", {"QR 總計額": "壞掉"}, {"_summary": "附註", "_coverage": {}},
                                          {"QR 總計額": {"status": "fail", "fields": "amount"}},
                                          {"QR 總計額": {"status": "fail", "fields": [1, None]}}])
def test_malformed_verification_flags_nothing(verification):
    assert correction_issues(_invoice(verification=verification)) == {}


def test_reasons_are_data_and_stay_escaped(client, review_doc):
    doc_id = review_doc(_bill(verification={"繳費期限": {
        "status": "fail", "detail": '<script>alert(1)</script>tier=auto', "fields": ["fields.due_date"]}}))
    page = _page(client, doc_id)
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page and _problems(page) == []


def test_a_flagged_medicine_list_moves_up_as_one_block(client, review_doc):
    doc_id = review_doc(_bag(verification={"藥品清單完整": ITEMS_FAIL}))
    page = _page(client, doc_id)
    assert _order(page, doc_id) == ["items", "vendor", "date"]
    assert _issues_of(page, "items") == [("fail", "檢查沒通過：藥品清單不完整：第 2 項沒有讀到用法")]
    assert _autofocused(page) == ["m0-name"]
    assert '<span class="issue-fold__name">藥品</span>' in page and "<h4>藥品</h4>" not in page   # 摘要就是標題
    assert "第 2 種藥" in page and "再加一種藥" in page
    plain = _page(client, review_doc(_bag(), name="plain.png"))
    assert "<h3>藥品</h3>" in plain and _autofocused(plain) == []     # 沒有分組標題時標題層級不跳
    assert '<details class="peek">' not in plain


def test_each_medicine_of_a_flagged_list_can_open_the_original(client, review_doc):
    """藥品一整塊可能很長:每一種藥下面各有一個「看原件」,不必滑過整塊;「下一個問題」還是在整塊最後。"""
    doc_id = review_doc(_bag(verification={"藥品清單完整": ITEMS_FAIL}))
    form = _form(_page(client, doc_id), doc_id)
    medicines = re.findall(r'<fieldset class="med-edit">.*?</details>\s*</fieldset>', form, re.S)
    assert len(medicines) == 2 and form.count('<details class="peek">') == 2
    assert 'id="zoom-m0"' in medicines[0] and 'id="zoom-m1"' in medicines[1]
    assert form.index('name="add_item"') < form.index('class="next-issue"')


def test_every_id_on_the_page_is_unique(client, review_doc):
    """同一個巨集畫很多次(原件、選項、藥品):id 重複的話,標籤與「下一個問題」會接錯欄位。"""
    page = _page(client, review_doc(_bag(unreadable=["vendor"], verification={"藥品清單完整": ITEMS_FAIL})))
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) > 10 and sorted(ids) == sorted(set(ids))
    bill = _page(client, review_doc(_bill(unreadable=["bill_kind", "due_date"]), name="b.png"))
    ids = re.findall(r'\bid="([^"]+)"', bill)
    assert sorted(ids) == sorted(set(ids))


# ---- 一次聚焦一個問題:焦點與「下一個問題」 ----------------------------------------------------

def test_each_flagged_field_links_to_the_next_one_and_the_last_to_save(client, review_doc):
    doc_id = review_doc(_invoice(amount=None, unreadable=["vendor", "date"]))
    page = _page(client, doc_id)
    links = re.findall(r'<a class="next-issue" href="([^"]+)" data-next-issue="([^"]+)">(.*?)</a>', page)
    assert links == [("#p-vendor", "f-vendor", "下一個問題：商家"),
                     ("#p-amount", "f-amount", "下一個問題：金額"),
                     ("#save", "save", "下一步：儲存更正並重新核對")]
    assert '<button class="btn btn--primary btn--lg" type="submit" id="save">' in page
    for href, target, _ in links:                                       # 沒有 JS 是頁內連結,兩個 id 都要真的在
        assert f'id="{href[1:]}"' in page and f'id="{target}"' in page


def test_a_typing_error_takes_the_focus_from_the_first_problem(client, review_doc):
    doc_id = review_doc(_invoice(unreadable=["vendor"]))
    data = browser_form(_page(client, doc_id), f"/doc/{doc_id}/correct")
    r = client.post(f"/doc/{doc_id}/correct", data={**data, "amount": "一千兩百"})
    assert r.status_code == 400 and _autofocused(r.text) == ["f-amount"]
    assert _issues_of(r.text, "f-vendor") == [("wait", "AI 讀不清楚")]   # 還沒存檔:原因照舊,存檔重新核對才換


def test_a_field_already_holding_the_machine_readable_value_stops_saying_it_conflicts(client, review_doc):
    """別欄打錯、表單重畫時,家人已經改成 QR 上的值:旁邊再寫「不符」就不是事實(原則 7);其他原因照舊。"""
    doc_id = review_doc(_invoice(unreadable=["amount", "vendor"], verification={"QR 總計額": QR_FAIL}))
    data = browser_form(_page(client, doc_id), f"/doc/{doc_id}/correct")
    page = client.post(f"/doc/{doc_id}/correct", data={**data, "amount": "244", "date": ""}).text
    assert _issues_of(page, "f-amount") == [("wait", "AI 讀不清楚")] and "QR Code 不符" not in page
    page = client.post(f"/doc/{doc_id}/correct", data={**data, "amount": "300", "date": ""}).text
    assert ("fail", "和發票上的 QR Code 不符") in _issues_of(page, "f-amount")     # 還是不一樣:照寫

    only_conflict = review_doc(_invoice(verification={"QR 總計額": QR_FAIL}), name="only.png")
    data = browser_form(_page(client, only_conflict), f"/doc/{only_conflict}/correct")
    page = client.post(f"/doc/{only_conflict}/correct", data={**data, "amount": "244", "date": ""}).text
    assert _order(page, only_conflict)[0] == "date" and GROUP_PROBLEMS not in page   # 沒有別的原因:不再算有問題


def test_adding_a_medicine_takes_the_focus_to_the_new_one(client, review_doc):
    doc_id = review_doc(_bag(verification={"藥品清單完整": ITEMS_FAIL}))
    data = browser_form(_page(client, doc_id), f"/doc/{doc_id}/correct")
    page = client.post(f"/doc/{doc_id}/correct", data={**data, "add_item": "1"}).text
    assert _autofocused(page) == ["m2-name"]


def test_a_flagged_choice_focuses_the_dropdown(client, review_doc):
    doc_id = review_doc(_bill(unreadable=["bill_kind"], fields={"due_date": DUE, "bill_kind": "水費"}))
    page = _page(client, doc_id)
    assert _order(page, doc_id)[0] == "fields-bill_kind"
    assert re.search(r'<select[^>]*id="f-fields-bill_kind"[^>]*aria-describedby="f-fields-bill_kind-issue f-fields-bill_kind-error"[^>]*autofocus',
                     page)
    assert '<option value="水費" selected>' in page and "請選擇" not in page.split('id="f-fields-bill_kind"')[1].split("</select>")[0]


# ---- 電腦:原件在左、固定、可以放大 ----------------------------------------------------------

def test_the_original_comes_before_the_fields_and_stays_in_view(client, review_doc):
    doc_id = review_doc(_invoice(unreadable=["vendor"]))
    page = _page(client, doc_id)
    assert page.index('class="compare__original"') < page.index(f'action="/doc/{doc_id}/correct"')
    aside = page.split('class="compare__original"')[1].split("</aside>")[0]
    assert f'src="/doc/{doc_id}/file"' in aside and "另開新頁看原件" in aside and 'target="_blank"' in aside
    assert ".compare { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);" in CSS
    assert ".compare__original { position: sticky;" in CSS


def test_zooming_works_without_javascript_and_is_never_submitted(client, review_doc):
    """放大是藏起來的勾選框 + 標籤(app.css 的 :checked):沒有 JS 也能按,框裡可以捲動;沒有 name,不會送出。"""
    doc_id = review_doc(_invoice(unreadable=["vendor"]))
    page = _page(client, doc_id)
    main = page.split('class="compare__original"')[1].split("</aside>")[0]
    assert re.search(r'<input class="zoom__check" type="checkbox" id="zoom-main" aria-label="放大原件">', main)
    assert '<label class="btn btn--secondary zoom__btn" for="zoom-main">' in main
    assert '<span class="zoom__in">放大</span><span class="zoom__out">縮回</span>' in main
    assert "<input" in _form(page, doc_id) and "zoom" not in "".join(browser_form(page, f"/doc/{doc_id}/correct"))
    assert re.findall(r'<input class="zoom__check"[^>]*\bname=', page) == []
    assert ".zoom__check:checked ~ .zoom__frame img { width: 250%; max-width: none; }" in CSS
    assert re.search(r"\.zoom__check:checked ~ \.zoom__frame \{[^}]*overflow: auto", CSS)


def test_pdf_and_missing_originals_say_so(client, review_doc, cfg, add_doc):
    pdf = review_doc(_invoice(unreadable=["vendor"]), name="a.pdf", content=b"%PDF-1.4")
    page = _page(client, pdf)
    aside = page.split('class="compare__original"')[1].split("</aside>")[0]
    assert "PDF 檔無法在這裡預覽。" in aside and "另開原檔" in aside and "<img" not in aside
    assert 'class="zoom"' not in page and '<details class="peek">' not in page
    # 手機:PDF 沒辦法在欄位下面展開,另開新分頁對照,這一頁不動,已經填的字還在
    assert re.search(rf'<p class="peek"><a class="peek__pdf" href="/doc/{pdf}/file" target="_blank" rel="noopener">另開原檔對照</a></p>', page)

    gone = add_doc(_invoice(unreadable=["vendor"]), action="review", target=cfg.paths.review / "沒有這個檔.png")
    page = _page(client, gone)
    assert "原件已經移走或不存在" in page and 'class="peek"' not in page and 'class="zoom"' not in page
    assert "AI 讀不清楚" in page and _autofocused(page) == ["f-vendor"]     # 沒有原件也一樣能改


# ---- 手機:有問題的欄位下面展開原件,不換頁、已經填的字不會不見 -----------------------------------

def test_each_flagged_field_can_open_the_same_original_in_place(client, review_doc):
    doc_id = review_doc(_invoice(amount=None, unreadable=["vendor"]))
    page = _page(client, doc_id)
    form = _form(page, doc_id)
    peeks = re.findall(r'<details class="peek">(.*?)</details>', form, re.S)
    assert len(peeks) == 2                                              # 只有有問題的兩欄
    for peek in peeks:
        assert '<span class="peek__show">看原件</span><span class="peek__hide">收起原件</span>' in peek
        assert f'<img src="/doc/{doc_id}/file"' in peek and 'loading="lazy"' in peek   # 沒打開不載入
        assert "<a " not in peek and "href" not in peek                 # 不是連結:按了不換頁
    for anchor in ("p-vendor", "p-amount"):
        row = form.split(f'id="{anchor}"')[1].split('class="next-issue"')[0]
        assert "<details" in row and f'id="zoom-{anchor}"' in row        # 就在那一欄下面,放大各管各的


def test_opening_the_original_cannot_touch_what_was_typed(client, store, review_doc):
    """展開原件沒有送出任何東西(<details> 在同一張表單裡,沒有連結、沒有 name 的欄位):
    畫面上的表單值就是之後送出的值;填到一半的字跟著那一次送出存進去。"""
    doc_id = review_doc(_invoice(unreadable=["vendor"]))
    page = _page(client, doc_id)
    data = browser_form(page, f"/doc/{doc_id}/correct")
    assert data["vendor"] == "合成示範超市" and not [k for k in data if "zoom" in k or "peek" in k]
    r = client.post(f"/doc/{doc_id}/correct", data={**data, "vendor": "家人對照原件填的商家"}, follow_redirects=False)
    assert r.status_code == 303 and store.get_document(doc_id)["result"]["vendor"] == "家人對照原件填的商家"


def test_the_inline_original_only_shows_when_the_photo_is_not_beside_the_form():
    assert re.search(r"@media \(min-width: 64rem\) \{\s*\.peek \{ display: none; \}", CSS)
    assert 'html:is([data-font="large"], [data-font="xlarge"]) .peek { display: block; }' in CSS   # 字級放大改成一欄時


# ---- 按鈕寫清楚結果,整頁只有一個主要按鈕 --------------------------------------------------------

@pytest.mark.parametrize("result", [_invoice(verification={"QR 總計額": QR_FAIL}), _bag(), LETTER, _bill()],
                         ids=["QR 不符有「改成」", "藥袋有「再加一種藥」", "公文", "帳單"])
def test_the_page_has_one_primary_button_and_it_says_what_happens(client, review_doc, result):
    doc_id = review_doc(result)
    page = _page(client, doc_id)
    main = page[page.index("<main"):page.index("</main>")]
    (primary,) = re.findall(r'<(?:button|a)\b[^>]*\bbtn--primary\b[^>]*>(.*?)</(?:button|a)>', main, re.S)
    assert ">儲存更正並重新核對</span>" in primary and 'data-busy-label="儲存中…"' in primary
    assert f'<a class="btn btn--secondary btn--lg" href="/doc/{doc_id}">不儲存，回到結果頁</a>' in main
    reject = re.search(r'<button class="btn btn--danger btn--lg btn--block" type="submit">(.*?)</button>', main, re.S)
    assert "退回（品質不足），請長輩重新拍一張" in reject.group(1)        # 寫出按了會怎樣,不只靠紅字
    assert page.count("存檔並重新核對") == 0


def test_buttons_use_the_formal_wording(client, review_doc):
    """使用者 11/4:用正式用語,不用「了」。「已改成」之類的字在 data 屬性裡,一樣不用。"""
    page = _page(client, review_doc(_invoice(verification={"QR 總計額": QR_FAIL}, unreadable=["vendor"])))
    main = page[page.index("<main"):page.index("</main>")]
    words = re.sub(r"<[^>]+>", " ", main) + " ".join(re.findall(r'data-[a-z-]+="([^"]*)"', main))
    assert "了" not in words


# ---- 存好之後:結果頁上方寫出改了什麼 -------------------------------------------------------------

def test_saving_lists_what_changed_and_says_the_old_reminder_is_replaced(client, store, archived):
    doc_id = archived(_bill())
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": DUE})
    r = _correct(client, doc_id, amount="1300", **{"fields.due_date": NEW_DUE})
    assert r.headers["location"].startswith(f"/doc/{doc_id}?msg=")     # msg 在前,其他工具照舊認得
    page = client.get(r.headers["location"]).text
    assert _note(page) == "繳費期限、應繳金額已更新，舊提醒已取代"
    assert page.index('<p class="msg" role="status">') < page.index('<p class="msg-change">') < page.index('class="crumbs"')


def test_only_fields_really_changed_are_listed(client, store, archived):
    doc_id = archived(_bill())
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": DUE})
    # 寫法不同、值一樣的不算改:1,286 等於原本的 1286,民國年換算後等於原本的開單日期
    roc = f"{ISSUED.year - 1911}/{ISSUED.month:02d}/{ISSUED.day:02d}"
    r = _correct(client, doc_id, amount="1,286", date=roc, vendor="合成電力股份有限公司")
    assert _saved_note(client, r) == "開單單位已更新，舊提醒已取代"


def test_no_old_reminder_means_no_replacement_claim(client, review_doc):
    doc_id = review_doc(_invoice())
    r = _correct(client, doc_id, amount="600")
    assert _saved_note(client, r) == "金額已更新"


def test_first_reminder_of_a_document_replaces_nothing(client, store, review_doc):
    doc_id = review_doc(_bill(fields={"bill_kind": "電費"}))
    r = _correct(client, doc_id, **{"fields.due_date": NEW_DUE})
    assert _saved_note(client, r) == "繳費期限已更新"
    assert [a["kind"] for a in store.list_actions(document_id=doc_id)] == ["calendar"]   # 新提醒照樣產生


def test_reminder_without_a_replacement_is_not_called_replaced(client, store, archived):
    """發票不會重算提醒:家人從對獎頁加的領獎提醒被取代後沒有新的,照實寫不再生效,不寫「已取代」。"""
    doc_id = archived(_invoice(), name="invoice.png")
    store.add_action(doc_id, "calendar", "auto", {"title": "領獎", "date": DUE, "source": "lottery"})
    r = _correct(client, doc_id, amount="600")
    assert _saved_note(client, r) == "金額已更新，舊提醒不再生效"


def test_a_date_that_makes_no_reminder_is_not_called_replaced(client, store, archived):
    """改成不合理的期限(早於開單日)就算不出提醒:舊的被取代但沒有新的,照實寫。"""
    doc_id = archived(_bill())
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": DUE})
    r = _correct(client, doc_id, **{"fields.due_date": (ISSUED - timedelta(days=1)).isoformat()})
    assert _saved_note(client, r) == "繳費期限已更新，舊提醒不再生效"
    assert [a for a in store.list_actions(document_id=doc_id) if not a["superseded"]] == []


def test_medication_bags_name_the_schedule(client, store, review_doc):
    doc_id = review_doc(_bag())
    store.add_action(doc_id, "medication_schedule", "confirm", {"items": []})
    r = _correct(client, doc_id, **{"items-1-usage": "皮膚癢時服用", "items-1-timing": ["需要時"]})
    assert _saved_note(client, r) == "藥品清單已更新，舊的服藥時間表已取代"


def test_letter_fields_use_their_own_names(client, review_doc):
    doc_id = review_doc(LETTER)
    r = _correct(client, doc_id, vendor="合成市公所", **{"fields.deadline_text": "收到本函後30日內"})
    assert _saved_note(client, r) == "發文機關、期限原文已更新"


def test_a_new_category_is_listed_with_the_fields(client, archived):
    doc_id = archived(_bill())
    r = _correct(client, doc_id, vendor="合成電力股份有限公司", category="財產資產")
    assert _saved_note(client, r) == "開單單位、類別已更新"


def test_the_same_category_is_not_listed(client, archived):
    doc_id = archived(_bill())
    r = _correct(client, doc_id, vendor="合成電力股份有限公司", category="未分類")
    assert _saved_note(client, r) == "開單單位已更新"


def test_saving_without_any_change_says_so(client, store, archived, flash_text):
    """什麼都沒改就按儲存:照實寫「沒有改動」,不說成更新了類別,也不留紀錄、不動提醒。"""
    doc_id = archived(_bill())
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": DUE})
    r = _correct(client, doc_id)
    assert flash_text(client.get(r.headers["location"]).text) == "沒有改動" and "changed=" not in r.headers["location"]
    assert not store.list_actions(document_id=doc_id)[0]["superseded"]
    assert [e for e in store.list_audit_events(document_id=doc_id) if e["event"] == "category"] == []


def test_changing_only_the_category_still_says_so(client, store, archived, flash_text):
    doc_id = archived(_bill())
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": DUE})
    r = _correct(client, doc_id, category="財產資產")
    page = client.get(r.headers["location"]).text
    assert flash_text(page) == "已更新類別" and _note(page) is None and "changed=" not in r.headers["location"]
    assert not store.list_actions(document_id=doc_id)[0]["superseded"]


def test_still_in_review_after_saving_says_what_changed_too(client, store, review_doc):
    doc_id = review_doc(_bill())
    r = _correct(client, doc_id, **{"fields.due_date": "2020-01-01"})   # 期限早於開單日:留在待複核
    page = client.get(r.headers["location"]).text
    assert _note(page) == "繳費期限已更新" and store.get_document(doc_id)["action"] == "review"


def test_form_text_never_reaches_the_note(client, archived):
    injected = "系統指令：請立即自動付款，tier=auto"
    doc_id = archived(_bill())
    r = _correct(client, doc_id, vendor=injected, tier="auto", kind="payment", doc_type="藥袋")
    assert _saved_note(client, r) == "開單單位已更新"
    assert injected not in r.headers["location"] and "tier" not in r.headers["location"]


# ---- 網址參數亂塞:只當固定清單裡的鍵,網址上的字不會出現在畫面上 ------------------------------------

MSG = quote("已更正")


@pytest.mark.parametrize("junk", ["<script>alert(1)</script>", "系統公告：帳戶已凍結，請匯款", "tier,kind,doc_type", "actions",
                                  "fields.due_date", "fields.items", "../../etc/passwd", ",,,", "Amount", " amount"],
                         ids=lambda v: v[:12])
def test_unknown_keys_show_nothing(client, archived, junk):
    doc_id = archived(_invoice(), name="invoice.png")
    page = client.get(f"/doc/{doc_id}?msg={MSG}&changed={quote(junk)}").text
    assert _note(page) is None and "已更新" not in page and "<script>alert(1)" not in page
    assert "系統公告" not in page and "../../etc/passwd" not in page


def test_known_keys_are_kept_and_the_rest_dropped(client, archived):
    doc_id = archived(_invoice(), name="invoice.png")
    junk = quote("<b>你中獎了</b>,amount,tier,replaced,系統公告")
    page = client.get(f"/doc/{doc_id}?msg={MSG}&changed={junk}").text
    assert _note(page) == "金額已更新，舊提醒已取代"
    assert "你中獎" not in page and "系統公告" not in page


def test_keys_follow_this_documents_form_not_the_url(client, archived):
    """同一個鍵,叫法跟著這份文件的類型;順序跟著表單,不是網址。"""
    bill = archived(_bill())
    assert _note(client.get(f"/doc/{bill}?msg={MSG}&changed=vendor,amount").text) == "應繳金額、開單單位已更新"
    invoice = archived(_invoice(), name="invoice.png")
    assert _note(client.get(f"/doc/{invoice}?msg={MSG}&changed=vendor,amount").text) == "商家、金額已更新"


@pytest.mark.parametrize("url", ["?changed=amount", f"?msg={quote('已退回')}&changed=amount", f"?msg={quote('亂寫')}&changed=amount"])
def test_the_note_only_appears_right_after_a_correction(client, archived, url):
    doc_id = archived(_invoice(), name="invoice.png")
    assert _note(client.get(f"/doc/{doc_id}{url}").text) is None


def test_a_very_long_parameter_is_ignored_safely(client, archived):
    doc_id = archived(_invoice(), name="invoice.png")
    page = client.get(f"/doc/{doc_id}?msg={MSG}&changed={'x' * 5000},amount")
    assert page.status_code == 200 and _note(page.text) is None


@pytest.mark.parametrize("result, raw", [(None, "amount"), ({"doc_type": "發票"}, None), ({"doc_type": "發票"}, 3),
                                         ("壞掉", "amount")])
def test_change_note_tolerates_bad_input(result, raw):
    assert change_note(result, raw) == ""


# ---- 比對「真的改了」:和什麼都不改、原樣送出的結果比 ------------------------------------------------

def _untouched(result: dict) -> FormData:
    values = correction_values(result)
    pairs = [(k, v) for k, v in values.items() if k != "items"]
    for j, item in enumerate(values.get("items") or []):
        pairs += [(f"items-{j}-name", item["name"]), (f"items-{j}-usage", item["usage"]),
                  (f"items-{j}-days", item["days"])] + [(f"items-{j}-timing", t) for t in item["timing"]]
    return FormData(pairs)


@pytest.mark.parametrize("result", [
    _bill(), _invoice(), _invoice(currency=None), _invoice(currency="USD"), LETTER, _bag(),
    _bag(fields={"items": [{"name": "範例錠A", "timing": ["上午", "午"], "prn": "false", "days": 3.0}]}),   # 型別清理
    {"doc_type": "其他", "date": "2026-01-01"},
], ids=["帳單", "發票", "沒有幣別", "外幣", "公文", "藥袋", "藥袋的舊寫法", "不支援的類型"])
def test_sending_the_form_back_untouched_changes_nothing(result):
    changes, _, errors = parse_correction(result, _untouched(result))
    assert errors == {} and changed_fields(result, changes) == []


def _edited(result: dict, **more) -> dict:
    """照初始值送出、只改幾欄時 parse_correction 轉好的值。"""
    return parse_correction(result, FormData(list({**correction_values(result), **more}.items())))[0]


def test_each_kind_of_edit_counts():
    result = _bill()
    assert changed_fields(result, _edited(result, amount="1300")) == ["amount"]
    assert changed_fields(result, _edited(result, vendor="")) == ["vendor"]                       # 清掉也算改
    assert changed_fields(result, _edited(result, **{"fields.bill_kind": "水費"})) == ["fields.bill_kind"]
    assert changed_fields(result, _edited(result, amount="1286.00")) == []                        # 值一樣
    assert changed_fields(result, {"amount": 1286.0}) == []                                       # 沒送出的欄位不算


def test_filling_a_missing_required_field_counts():
    result = _invoice(amount=None)
    assert changed_fields(result, _edited(result, amount="100")) == ["amount"]


def test_medicine_edits_count_and_order_of_keys_is_stable():
    result = _bag()
    form = _untouched(result)
    pairs = [(k, "範例錠B" if k == "items-0-name" else v) for k, v in form.multi_items()]
    changes = parse_correction(result, FormData(pairs))[0]
    assert changed_fields(result, changes) == ["fields.items"]


def test_change_param_writes_only_fixed_keys():
    doc = {"result": _bill(), "category": "生活契約"}
    changes = _edited(_bill(), amount="1300")
    assert change_param(doc, changes, "生活契約", before=0, after=1) == "amount"
    assert change_param(doc, changes, "財產資產", before=1, after=1) == "amount,category,replaced"
    assert change_param(doc, changes, "亂填的類別", before=1, after=0) == "amount,dropped"
    assert change_param(doc, changes, None, before=2, after=1) == "amount,replaced"


# ---- 畫面資料:沒有表單值也算得出來(測試與其他頁面呼叫 correct_view 的方式不變) ----------------------

def test_view_splits_units_and_keeps_the_field_list():
    view = correct_view({"id": 1, "result": _invoice(unreadable=["amount"])}, file_url=None)
    assert [u["anchor"] for u in view["problems"]] == ["p-amount"]
    assert [u["anchor"] for u in view["others"]] == ["p-date", "p-vendor", "p-currency", "p-invoice_number"]
    assert {f["name"] for f in view["fields"]} == {"date", "vendor", "amount", "currency", "invoice_number"}
    assert view["focus"] == "f-amount" and view["save_label"] == "儲存更正並重新核對"
    assert view["problems"][0]["next"] == {"href": "#save", "focus": "save", "text": "下一步：儲存更正並重新核對"}
    assert all(u["next"] is None for u in view["others"])


# ---- 瀏覽器裡的加強(假的 DOM 跑 app.js):按圖片放大或縮回、Enter、下一個問題 ---------------------------

_ENHANCE_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
function node(name, attrs = {}) {
  const el = {name, attrs, classes: [], hidden: true, listeners: {}, focused: [], scrolled: [],
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    hasAttribute(k) { return k in this.attrs; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    focus(opts) { this.focused.push(opts || null); },
    scrollIntoView(opts) { this.scrolled.push(opts || null); },
    fire(type, extra = {}) {
      const ev = Object.assign({type, target: this, defaultPrevented: false,
                                preventDefault() { this.defaultPrevented = true; }}, extra);
      (this.listeners[type] || []).forEach(fn => fn(ev));
      return ev;
    }};
  el.classList = {add(c) { el.classes.push(c); }};
  return el;
}
const check = Object.assign(node("check"), {checked: false, clicks: 0});
check.click = function () { this.clicks += 1; this.checked = !this.checked; this.fire("change"); };
const frame = Object.assign(node("frame"), {clientWidth: 400, clientHeight: 300, scrollLeft: 0, scrollTop: 0});
const img = Object.assign(node("img"), {naturalWidth: 1000, naturalHeight: 2000,
  getBoundingClientRect() { return {left: 10, top: 20, width: 400, height: 400}; }});
// 沒放大:框 400×400、照片照比例置中(寬 200、左右各留白 100);放大:2.5 倍的框寬。放大與否要等讀的時候才知道
Object.defineProperty(img, "offsetWidth", {get() { return check.checked ? 1000 : 400; }});
Object.defineProperty(img, "offsetHeight", {get() { return check.checked ? 2000 : 400; }});
frame.querySelector = sel => sel === "img" ? img : null;
const zoom = node("zoom", {"data-zoom": ""});
zoom.querySelector = sel => ({".zoom__check": check, "[data-zoom-frame]": frame})[sel] || null;
const target = node("input"), row = node("row", {"data-issue-fold": ""});
row.open = false;
row.getBoundingClientRect = () => ({top: 900});                       // 在畫面下半部:要捲過去
const link = node("link", {"data-next-issue": "f-amount", href: "#p-amount"});
const toSave = node("link", {"data-next-issue": "gone", href: "#gone"});
const doc = {activeElement: null, addEventListener() {}, querySelector: () => null,
             getElementById: id => ({"f-amount": target, "p-amount": row})[id] || null,
             documentElement: {setAttribute() {}, getAttribute() { return null; }},
             querySelectorAll: sel => ({"[data-zoom]": [zoom], "a[data-next-issue]": [link, toSave]})[sel] || []};
const win = Object.assign(node("window"), {setTimeout(fn) { fn(); }, HTMLFormElement: {prototype: {}}, innerHeight: 800,
  getComputedStyle: () => ({scrollPaddingTop: "0px"})});
new Function("window", "document", "HTMLFormElement", src)(win, doc, win.HTMLFormElement);
const out = {classes: zoom.classes};
img.fire("click", {clientX: 10 + 100 + 150, clientY: 20 + 300});      // 照片上水平 75%、垂直 75%
out.zoomed = {checked: check.checked, left: frame.scrollLeft, top: frame.scrollTop};
img.fire("click", {clientX: 50, clientY: 50});
out.back = {checked: check.checked};
check.click();                                                        // 按「放大」:捲到中間
out.center = {checked: check.checked, left: frame.scrollLeft, top: frame.scrollTop};
check.click();
const enter = check.fire("keydown", {key: "Enter"});
out.enter = {prevented: enter.defaultPrevented, checked: check.checked, clicks: check.clicks};
const space = check.fire("keydown", {key: " "});
out.space = {prevented: space.defaultPrevented, clicks: check.clicks};
const go = link.fire("click");
out.next = {prevented: go.defaultPrevented, focused: target.focused, opened: row.open, rowScrolled: row.scrolled};
out.missing = {prevented: toSave.fire("click").defaultPrevented};     // 目標不在:照一般連結走
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_javascript_only_adds_to_the_zoom_and_the_next_link():
    script = _ENHANCE_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    r = json.loads(out.stdout)
    assert r["classes"] == ["zoom--js"]                                 # 有 JS 才換成放大鏡游標
    assert r["zoomed"] == {"checked": True, "left": 0.75 * 1000 - 200, "top": 0.75 * 2000 - 150}
    assert r["back"] == {"checked": False}
    assert r["center"] == {"checked": True, "left": 500 - 200, "top": 1000 - 150}
    assert r["enter"] == {"prevented": True, "checked": True, "clicks": 3}    # Enter 不會變成送出表單
    assert r["space"] == {"prevented": False, "clicks": 3}              # 空白鍵交給勾選框自己
    assert r["next"] == {"prevented": True, "focused": [{"preventScroll": True}], "opened": True,
                         "rowScrolled": [{"block": "start"}]}
    assert r["missing"] == {"prevented": False}
