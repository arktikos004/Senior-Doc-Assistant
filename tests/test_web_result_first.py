"""結果頁第一屏先回答三個問題(使用者 11/4):這是什麼、多少錢或要做什麼、什麼時候前,再一句核對結果;
下面才是處理狀態與操作,核對細項、辨識來源與完整欄位收起來。長輩與家人看到不同的主要操作。
文件與身分都是合成的,不呼叫模型、不連網。"""
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from samples import BILL, INVOICE, LETTER
from src.providers import MockAnalyzer
from src.verify import VERIFY_ERROR_SUMMARY
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import STATIC_DIR, action_view, answer_view, due_text, status_rows, verification_view

TODAY = date.today()
SOON = (TODAY + timedelta(days=10)).isoformat()

_PASS = {"status": "pass", "detail": "規則通過"}
BARCODE_OK = {"條碼應繳金額": {"status": "pass", "detail": "條碼上的應繳金額 1,854 元，和讀到的金額一樣", "fields": ["amount"]},
              "繳費期限": {**_PASS, "fields": ["fields.due_date"]}, "金額合理性": {**_PASS, "fields": ["amount"]}}
BARCODE_BAD = {"條碼應繳金額": {"status": "fail", "detail": "條碼上的應繳金額是 1,286 元，讀到的卻是 1,854 元", "fields": ["amount"]},
               "繳費期限": {**_PASS, "fields": ["fields.due_date"]}}
BILL_RULES = {"超商代收條碼": {"status": "skip", "detail": "找不到超商代收條碼", "fields": []},
              "繳費期限": {**_PASS, "fields": ["fields.due_date"]}, "金額合理性": {**_PASS, "fields": ["amount"]}}
QR_OK = {"QR 字軌號碼": {**_PASS, "fields": ["invoice_number"]}, "QR 開立日期": {**_PASS, "fields": ["date"]},
         "QR 總計額": {**_PASS, "fields": ["amount"]}, "賣方統編檢查碼": {**_PASS, "fields": ["fields.seller_tax_id"]}}
QR_BAD = {"QR 總計額": {"status": "fail", "detail": "QR 記載總計 230，辨識金額 320", "fields": ["amount"]},
          "QR 開立日期": {**_PASS, "fields": ["date"]}}
LETTER_RULES = {"主旨完整": {**_PASS, "fields": ["fields.subject"]}}
RECEIPT = {"doc_type": "收據", "date": "2026-09-18", "vendor": "示範藥局", "amount": 150.0, "currency": "NTD", "fields": {},
           "verification": {"日期合理性": {**_PASS, "fields": ["date"]}, "金額合理性": {**_PASS, "fields": ["amount"]}}}
MEDS = {"doc_type": "藥袋", "date": "2026-09-30", "vendor": "示範診所", "plain_summary": "",
        "fields": {"items": [{"name": "示範藥甲", "dose_text": "1顆", "frequency_text": "睡前", "timing": ["睡前"], "prn": False, "days": 7}]},
        "verification": {"藥品清單完整": {**_PASS, "fields": ["fields.items"]}}}


def _as(cfg, store, role="elder", name="媽媽"):
    """用某個家庭成員的身分開網頁;role=None 是不在名單裡、只能查看的人。"""
    email = f"{role or 'viewer'}@example.test"
    if role:
        store.set_member(email, role, name)
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))
    c.get("/")
    return c


def _post(c, url, **form):
    return c.post(url, data={CSRF_FIELD: c.cookies[CSRF_COOKIE], **form}, follow_redirects=False)


def _main(html):
    """<main> 裡的內容:導覽列的「家人確認」不是這一頁帶去的。"""
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _first(html):
    return html.split('<section class="first"', 1)[1].split("</section>", 1)[0]


def _status(html):
    return html.split('<section class="section" id="status"', 1)[1].split("</section>", 1)[0]


def _check(html):
    """核對那一句的字。"""
    return re.search(r'<div class="first__check[^"]*">\s*<p data-speak-part>(.*?)</p>', html, re.S).group(1)


def _texts(html):
    return re.sub(r"<[^>]+>", " ", html)


def _board(first):
    """第一屏的重點看板(<div class="board board--answer"> 到它自己的 </div>,照 <div> 的層數找結尾)。"""
    start = first.index('<div class="board board--answer">')
    depth = 0
    for m in re.finditer(r"<(/?)div\b", first[start:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return first[start:start + m.end()]
    raise AssertionError("看板沒有結尾")


class _Spoken(HTMLParser):
    """照 speech.js 收集「聽重點」會唸的字:一個 data-speak-part 元素一段,略過 aria-hidden 的部分(翻牌只給眼睛看)。"""
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.parts = [], []     # stack:每一層 (在唸的段落裡, 藏起來)

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID:
            return
        attrs = dict(attrs)
        speaking, hidden = self.stack[-1] if self.stack else (False, False)
        if not speaking and "data-speak-part" in attrs:
            self.parts.append("")
        self.stack.append((speaking or "data-speak-part" in attrs, hidden or attrs.get("aria-hidden") == "true"))

    def handle_endtag(self, tag):
        if tag not in self._VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == (True, False):
            self.parts[-1] += data


def _spoken(html):
    parser = _Spoken()
    parser.feed(html)
    return [" ".join(part.split()) for part in parser.parts]


@pytest.fixture
def original(cfg, png_bytes):
    path = cfg.paths.archive / "a.png"
    path.write_bytes(png_bytes())
    return path


# ---- 第一屏:帳單 ------------------------------------------------------------------

def test_bill_answers_what_how_much_and_when_in_that_order(client, add_doc, original):
    html = client.get(f"/doc/{add_doc(dict(BILL, verification=BARCODE_OK), target=original)}").text
    first = _first(html)
    order = [first.index(text) for text in ("這是電費帳單", "1,854 元", "10月15日前繳費", "金額與條碼相符；期限請對照原件。",
                                            "聽重點", "查看原件")]
    assert order == sorted(order)
    assert '<h1 id="answer-title" data-speak-part>這是電費帳單</h1>' in first
    assert '<a class="btn btn--secondary btn--lg" href="#original">查看原件</a>' in first
    assert 'id="original"' in html


def test_what_how_much_and_when_sit_on_one_board(client, store, add_doc):
    """v5 樣稿(使用者 11/6 點頭):標題、金額、期限包進一塊深墨看板;核對那一句與按鈕留在看板下面。
    金額用窄體大字;期限畫成翻牌,牌只給眼睛看(aria-hidden),原本的字藏起來給讀屏與朗讀。"""
    doc_id = add_doc(dict(BILL, verification=BARCODE_OK))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    first = _first(client.get(f"/doc/{doc_id}").text)
    board = _board(first)
    assert 'id="answer-title"' in board and '<span class="first__amount">1,854 元</span>' in board
    assert "first__check" not in board and "聽重點" not in board
    due = re.search(r'<p class="first__line first__line--big first__line--due" data-speak-part>(.*?)</p>', board, re.S).group(1)
    shown = re.search(r'<span class="first__due" aria-hidden="true">(.*?)</span>\s*<span class="visually-hidden">', due, re.S).group(1)
    assert re.findall(r'<span class="flap__d">(\d)</span>', shown) == ["1", "0", "1", "5"]
    assert '<span class="first__verb">前繳費</span>' in shown
    assert '<span class="visually-hidden">10月15日前繳費</span>' in due
    assert re.search(r'<span class="tile tile--plain">還有 \d+ 天</span>', due)     # 狀態牌:字寫清楚,一樣寬


def test_a_deadline_that_is_not_a_date_is_written_not_drawn(client, add_doc):
    first = _first(client.get(f"/doc/{add_doc(dict(BILL, fields={'due_date': '下個月底', 'bill_kind': '電費'}))}").text)
    assert "下個月底前繳費" in first and 'class="flap' not in first and "first__due" not in first


def test_days_left_stay_beside_the_deadline_while_the_reminder_is_live(client, store, add_doc):
    doc_id = add_doc(dict(BILL, fields={"due_date": (TODAY + timedelta(days=2)).isoformat(), "bill_kind": "電費"}))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": (TODAY + timedelta(days=2)).isoformat()})
    assert '<span class="tile tile--wait">還有 2 天</span>' in _first(client.get(f"/doc/{doc_id}").text)


def test_view_original_only_when_there_is_one(client, add_doc):
    assert "查看原件" not in client.get(f"/doc/{add_doc(dict(BILL))}").text


@pytest.mark.parametrize("result, sentence", [
    # 只有獨立證據比對過的欄位說相符;條碼的代收期限只是輔助,期限照樣請對照原件
    (dict(BILL, verification=BARCODE_OK), "金額與條碼相符；期限請對照原件。"),
    (dict(BILL, verification={**BARCODE_OK, "條碼代收期限": {**_PASS, "fields": ["fields.due_date"]}}), "金額與條碼相符；期限請對照原件。"),
    (dict(INVOICE, fields={}, verification=QR_OK), "日期、金額與 QR Code 相符；商家請對照原件。"),
    # 只過規則:不說相符,寫請對照原件(原則 1)
    (dict(BILL, verification=BILL_RULES), "沒有讀到條碼，金額、期限請對照原件。"),
    (dict(INVOICE, fields={}, verification={"電子發票 QR": {"status": "skip", "detail": "找不到 QR", "fields": []},
                                            "日期合理性": {**_PASS, "fields": ["date"]}}),
     "沒有讀到 QR Code，商家、日期、金額請對照原件。"),
    (RECEIPT, "商家、日期、金額請對照原件。"),
    (dict(LETTER, verification=LETTER_RULES), "機關、期限、要做的事請對照原件。"),
    (MEDS, "藥名與服藥時段請對照藥袋。"),
    # 沒有任何檢查、核對程式出錯都照實說(原則 5)
    (dict(BILL, verification={}), "未核對：沒有能自動核對的項目，請對照原件。"),
    (dict(BILL, verification={"_summary": VERIFY_ERROR_SUMMARY}), "這次的核對沒有完成，請對照原件。"),
])
def test_check_sentence_only_calls_independent_evidence_a_match(client, add_doc, result, sentence):
    assert _check(client.get(f"/doc/{add_doc(result)}").text) == sentence


@pytest.mark.parametrize("result", [dict(BILL, verification=BILL_RULES), RECEIPT, MEDS, dict(LETTER, verification=LETTER_RULES)])
def test_rule_checks_never_say_match_anywhere_on_the_page(client, add_doc, result):
    assert "相符" not in client.get(f"/doc/{add_doc(result)}").text


@pytest.mark.parametrize("result, sentence, note", [
    (dict(BILL, verification=BARCODE_BAD), "讀到的金額和帳單上的超商條碼不一樣，請對照原件確認。", "和條碼不符"),
    (dict(INVOICE, fields={}, verification=QR_BAD), "讀到的金額和發票上的 QR Code 不一樣，請對照原件確認。", "和 QR Code 不符"),
])
@pytest.mark.parametrize("action", ["archive", "review"])
def test_mismatch_is_on_the_first_screen_and_not_folded(client, add_doc, result, sentence, note, action):
    html = client.get(f"/doc/{add_doc(result, action=action)}").text
    first = _first(html)
    assert _check(html) == sentence and "first__check--manual" in first
    assert f'<span class="tile tile--manual">{note}</span>' in first                  # 紅牌也寫成字,不只靠顏色
    before = first[:first.index(sentence)]
    assert before.count("<details") == before.count("</details>")                     # 不在收合區裡
    assert html.count(sentence) == 1                                                  # 待複核的原因是同一句,只寫一次


def test_rule_failure_is_flagged_but_not_called_a_mismatch(client, add_doc):
    verification = {"條碼應繳金額": BARCODE_OK["條碼應繳金額"],
                    "繳費期限": {"status": "fail", "detail": "早於出帳日", "fields": ["fields.due_date"]}}
    first = _first(client.get(f"/doc/{add_doc(dict(BILL, verification=verification))}").text)
    assert "金額與條碼相符；期限沒通過檢查，請對照原件。" in first and "first__check--manual" in first
    assert '<span class="tile tile--manual">沒通過檢查</span>' in first and "不符" not in first


def test_family_can_read_how_the_check_was_done_beside_the_sentence(client, add_doc):
    first = _first(client.get(f"/doc/{add_doc(dict(BILL, verification=BARCODE_OK))}").text)
    note = first[first.index('class="first__check'):]
    assert "不能證明帳單屬實" in note and "期限由 AI 讀取，請對照帳單。" in note
    assert '<details class="note">' in note and "<details class=\"note\" open" not in note


def test_review_reason_and_correction_stay_on_top(client, review_doc):
    doc_id = review_doc(dict(BILL, verification=BILL_RULES))
    first = _first(client.get(f"/doc/{doc_id}").text)
    head = first[first.index('class="page-head__reason"'):first.index("1,854 元")]
    assert "等待複核" in head and "AI 對這份文件的讀值沒有把握" in head and "data-speak-part" in head
    assert f'href="/doc/{doc_id}/correct"' in head and f'href="/doc/{doc_id}/retype"' in head
    # 待複核的原因、「更正讀值」「類型不對?」也在看板裡(展示資料沒有待複核的文件,只能靠這裡確認)
    board = _board(first)
    assert "等待複核" in board and f'<a class="btn btn--primary" href="/doc/{doc_id}/correct">' in board
    assert f'<a class="first__link" href="/doc/{doc_id}/retype">類型不對？</a>' in board


def test_unreadable_and_missing_values_are_said_plainly(client, add_doc):
    """讀不到就說讀不到,不留空白也不猜(原則 5)。"""
    result = dict(BILL, amount=None, unreadable=["fields.due_date"])
    first = _first(client.get(f"/doc/{add_doc(result)}").text)
    assert "沒有讀到應繳金額" in first and "繳費期限讀不清楚，請看原件" in first and "10月15日前" not in first
    missing = {"doc_type": "收據", "date": "2026-09-18", "fields": {}}
    first = _first(client.get(f"/doc/{add_doc(missing)}").text)
    assert "沒有讀到商家" in first and "沒有讀到金額" in first and "2026年9月18日" in first


def test_dates_not_in_iso_form_are_shown_as_read(client, add_doc):
    result = dict(BILL, fields={"due_date": "下個月底", "bill_kind": "電費"})
    assert "下個月底前繳費" in _first(client.get(f"/doc/{add_doc(result)}").text)


# ---- 第一屏:公文、藥袋、發票、收據 ---------------------------------------------------

def test_letter_answers_issuer_deadline_and_what_to_do(client, store, add_doc):
    doc_id = add_doc(dict(LETTER, fields=dict(LETTER["fields"], deadline=SOON), verification=LETTER_RULES))
    store.add_action(doc_id, "calendar", "auto", {"title": "公文期限：請補繳文件", "date": SOON})
    first = _first(client.get(f"/doc/{doc_id}").text)
    due = f"{(TODAY + timedelta(days=10)).month}月{(TODAY + timedelta(days=10)).day}日前辦理"
    order = [first.index(text) for text in ("這是公文", "發文機關：", "示範區公所", due, "還有 10 天", "要做的事：",
                                            "補繳身分證影本", "親自簽名", "機關、期限、要做的事請對照原件。")]
    assert order == sorted(order)


def test_letter_without_actions_falls_back_to_the_subject(client, add_doc):
    result = dict(LETTER, fields={"subject": "請補繳文件", "deadline": "2099-10-10", "deadline_text": "2099年10月10日前"},
                  verification=LETTER_RULES)
    first = _first(client.get(f"/doc/{add_doc(result)}").text)
    assert "主旨：" in first and "請補繳文件" in first and "機關、期限、主旨請對照原件。" in first


def test_letter_deadline_that_cannot_be_computed_is_copied_not_guessed(client, add_doc):
    words = dict(LETTER, fields={"subject": "請補繳文件", "deadline_text": "儘速辦理"})
    first = _first(client.get(f"/doc/{add_doc(words)}").text)
    assert "期限：" in first and "儘速辦理" in first and "前辦理" not in first
    nothing = dict(LETTER, fields={"subject": "請補繳文件"})
    assert "沒有讀到辦理期限" in _first(client.get(f"/doc/{add_doc(nothing)}").text)


def test_medication_answers_with_the_schedule_and_keeps_the_disclaimer(client, add_doc):
    html = client.get(f"/doc/{add_doc(MEDS)}").text
    first = _first(html)
    assert "這是藥袋" in first and "服藥時間表" in first and "示範藥甲" in first and "睡前" in first
    assert first.index("示範藥甲") < first.index("藥名與服藥時段請對照藥袋。")
    # 藥袋聲明收在「說明」裡、聽重點照樣會唸,整頁只有一次
    assert '<span class="note__line" data-speak-part>本系統只協助閱讀，不提供醫療建議；用藥請依醫師與藥師指示。</span>' in first
    assert html.count("本系統只協助閱讀，不提供醫療建議") == 1


def test_medication_hint_without_a_schedule_still_has_the_disclaimer(client, store, add_doc):
    doc_id = add_doc({"doc_type": "藥袋", "vendor": "示範診所", "fields": {}})
    first = _first(client.get(f"/doc/{doc_id}").text)
    assert "沒有讀到藥品清單" in first and "本系統只協助閱讀，不提供醫療建議" in first


@pytest.mark.parametrize("status, text, cls", [("pending", "等家人確認 2 天", "wait"), ("done", "家人已確認", "plain"), ("rejected", "已退回", "plain")])
def test_medication_status_is_on_the_first_screen(client, store, add_doc, status, text, cls):
    doc_id = add_doc(MEDS)
    action_id = store.add_action(doc_id, "medication_schedule", "confirm", {"title": "示範診所的服藥時間表",
                                 "items": MEDS["fields"]["items"]}, status=status)
    with store._conn() as conn:
        conn.execute("UPDATE actions SET created_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"), action_id))
    html = client.get(f"/doc/{doc_id}").text
    # v5 收尾(11/6):狀態是重點看板裡的狀態牌(等待是實心黃牌,其他是外框牌),和帳單的「還有 N 天」同一個位置
    board = _first(html).split('<div class="board board--answer">')[1].split('<div class="first__med">')[0]
    assert f'<p class="first__state" data-speak-part><span class="tile tile--{cls}">{text}</span></p>' in board
    assert text in _status(html)


def test_medication_schedule_is_a_board_and_only_copies_the_bag(client, add_doc):
    """服藥時間表是一塊看板,時段寫在一樣寬的牌上;藥名只照藥袋上的字,不補劑量、用途或任何藥物知識。"""
    first = _first(client.get(f"/doc/{add_doc(MEDS)}").text)
    slots = first[first.index('<dl class="slots board" data-speak-part>'):]
    slots = slots[:slots.index("</dl>")]
    assert re.findall(r'<dt class="slot">(.*?)</dt><dd>(.*?)</dd>', slots) == [("睡前", "示範藥甲")]
    assert "1顆" not in slots and "7 天" not in slots


def test_clinic_date_and_phone_are_two_aligned_columns_on_the_confirm_card(client, store, add_doc):
    """家人確認的服藥時間表(_medication.html 的 on_card):院所、日期、電話寫成兩欄的 <dl>,欄名一欄、內容一欄。"""
    store.add_action(add_doc(MEDS), "medication_schedule", "confirm", {
        "title": "示範診所的服藥時間表", "hospital": "示範診所", "dispensed_date": "2026-09-30",
        "pharmacist_phone": "02-0000-0000", "items": MEDS["fields"]["items"]})
    html = client.get("/confirm").text
    assert ('<dl class="med-info"><dt>醫療院所：</dt><dd>示範診所</dd><dt>調劑日期：</dt><dd>2026年9月30日</dd>'
            '<dt>藥師電話：</dt><dd>02-0000-0000</dd></dl>') in html
    assert '<dl class="slots board" data-speak-part>' in html


@pytest.mark.parametrize("result, title", [(dict(INVOICE, fields={}), "這是發票"), (RECEIPT, "這是收據")])
def test_invoice_and_receipt_answer_vendor_date_and_amount(client, add_doc, result, title):
    first = _first(client.get(f"/doc/{add_doc(result)}").text)
    order = [first.index(text) for text in (title, "商家：", result["vendor"], "日期：", "2026年9月18日", "金額：", "元")]
    assert order == sorted(order)
    assert "first__line--big" in first


def test_unsupported_type_says_so_in_the_title(client, add_doc):
    html = client.get(f"/doc/{add_doc({'doc_type': '其他', 'vendor': '示範公司'}, action='review')}").text
    assert "這是其他類型的文件" in _first(html) and "這是其他</h1>" not in html


# ---- 讀不出來、系統忙碌的頁面照舊 -----------------------------------------------------

def test_failed_page_is_unchanged(client, add_doc):
    html = client.get(f"/doc/{add_doc(None, action='failed', reason='AI 辨識失敗或回應無法解析')}").text
    main = _main(html)
    assert "這張讀不出來" in main and "重新拍一張" in main and "讀不出來" in main
    assert 'class="first"' not in main and "處理狀態" not in main and "聽重點" not in main and "data-share" not in main
    assert main.count("btn--primary") == 1


def test_busy_page_is_unchanged(client, store, cfg, png_bytes):
    target = cfg.paths.failed / "a.png"
    target.write_bytes(png_bytes())
    doc_id = store.add_document({"原始檔案": target.name, "動作": "failed", "原因": "AI 辨識失敗或回應無法解析", "目標路徑": str(target),
                                 "AI辨識結果": None, "錯誤": "ConnectionError: Failed to connect to Ollama."})
    main = _main(client.get(f"/doc/{doc_id}").text)
    assert "系統暫時忙碌，這張還沒讀" in main and "不用重拍" in main and "重新辨識" in main
    assert 'class="first"' not in main and "聽重點" not in main
    assert main.count("btn--primary") == 1


def test_a_busy_document_with_an_old_reading_shows_the_busy_page(client, add_doc, monkeypatch):
    """正在重新辨識、還留著舊讀值的文件:走忙碌的版面,不把舊讀值當成重點。"""
    doc_id = add_doc(dict(BILL))
    monkeypatch.setattr("web.render.doc_busy", lambda doc: True)
    main = _main(client.get(f"/doc/{doc_id}").text)
    assert "系統暫時忙碌，這張還沒讀" in main and 'class="first"' not in main and "處理狀態" not in main


# ---- 處理狀態與操作:已存檔和已繳費分開 ------------------------------------------------

def _rows(html):
    """處理狀態的每一列:(欄名, 那一列的字)。"""
    rows = re.findall(r'<div class="status-row">\s*<dt>(.*?)</dt>\s*<dd>(.*?)</dd>', _status(html), re.S)
    return {label: " ".join(_texts(body).split()) for label, body in rows}


def test_filed_and_paid_are_separate_rows(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    html = client.get(f"/doc/{doc_id}").text
    rows = _rows(html)
    assert rows["文件"].startswith("已存檔 收在文件櫃，") and "已繳費" not in rows["文件"]
    assert rows["繳費"].startswith("還沒標示已繳費")
    assert rows["提醒"].startswith("已列入提醒") and "取消提醒" in rows["提醒"] and "查看待辦" in rows["提醒"]
    status = _status(html)
    assert f'action="/reminder/{action_id}/done"' in status and 'name="decision" value="complete"' in status
    assert 'name="back" value="doc"' in status
    # 說明寫清楚:存檔不等於繳費、按了只是記錄、提醒只在網頁上
    note = status[status.index('<details class="note">'):status.index("</details>")]
    assert "不代表已繳費" in note and "只是記錄" in note and "不會付款" in note and "不會傳訊息" in note


def test_letter_says_handled_not_paid(client, store, add_doc):
    doc_id = add_doc(dict(LETTER))
    store.add_action(doc_id, "calendar", "auto", {"title": "公文期限：請補繳文件", "date": SOON})
    rows = _rows(client.get(f"/doc/{doc_id}").text)
    assert rows["辦理"].startswith("還沒標示已辦理") and "已辦理" in rows["辦理"] and "繳費" not in rows


def test_marking_paid_from_the_result_page_comes_back_with_who_and_when(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    elder = _as(cfg, store)
    r = _post(elder, f"/reminder/{action_id}/done", decision="complete", back="doc")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{doc_id}?msg=")
    html = elder.get(f"/doc/{doc_id}").text
    rows = _rows(html)
    assert rows["文件"].startswith("已存檔")                                      # 文件還是已存檔,兩件事各寫各的
    assert rows["繳費"].startswith("已繳費") and f"媽媽 {TODAY.month}/{TODAY.day} 標示" in rows["繳費"] and "改回未繳" in rows["繳費"]
    assert "提醒" not in rows and "取消提醒" not in html
    assert '<span class="tile tile--plain">已繳費</span>' in _first(html)              # 期限那一行不再算還有幾天
    r = _post(elder, f"/reminder/{action_id}/done", decision="undo", back="doc")
    assert r.status_code == 303 and _rows(elder.get(f"/doc/{doc_id}").text)["繳費"].startswith("還沒標示已繳費")


def test_old_completed_reminder_without_an_event_does_not_invent_who(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON}, status="completed")
    assert _rows(client.get(f"/doc/{doc_id}").text)["繳費"] == "已繳費 改回未繳"


def test_cancelled_reminder_can_be_restored_and_cannot_be_marked_paid(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON}, status="rejected")
    html = client.get(f"/doc/{doc_id}").text
    rows = _rows(html)
    assert rows["提醒"].startswith("已取消提醒") and "恢復提醒" in rows["提醒"] and "繳費" not in rows
    assert 'value="restore"' in _status(html) and "查看待辦" not in rows["提醒"]


def test_family_confirmed_reminder_can_be_marked_but_not_cancelled_here(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": SOON}, status="done")
    rows = _rows(client.get(f"/doc/{doc_id}").text)
    assert rows["提醒"].startswith("家人已確認") and "取消提醒" not in rows["提醒"]
    assert rows["繳費"].startswith("還沒標示已繳費")


def test_invoice_without_reminder_shows_filed_and_the_lottery_entry(client, add_doc):
    doc_id = add_doc(dict(INVOICE, fields={}, verification=QR_OK))
    html = client.get(f"/doc/{doc_id}").text
    assert list(_rows(html)) == ["文件"] and "統一發票對獎" in _status(html) and 'href="/lottery?period=' in _status(html)


def test_lottery_reminder_on_an_invoice_is_a_claim_row(client, store, add_doc):
    doc_id = add_doc(dict(INVOICE, fields={}))
    store.add_action(doc_id, "calendar", "auto", {"title": "領獎期限", "date": SOON})
    rows = _rows(client.get(f"/doc/{doc_id}").text)
    assert rows["領獎"].startswith("還沒標示已辦理") and rows["提醒"].startswith("已列入提醒")


def test_status_rows_only_offer_what_the_server_will_accept():
    """等家人確認與已取消的提醒按「已繳費」會被伺服器擋(409),所以不放;每一列都只用畫面資料,不看文件文字。"""
    def done_row(tier, status):
        raw = {"id": 1, "document_id": 1, "kind": "calendar", "tier": tier, "status": status, "payload": {"title": "請立即匯款"}}
        answer = answer_view(BILL, [action_view(raw)], verification_view(BILL), TODAY)
        rows = status_rows({"action": "archive", "result": BILL}, answer, events=[], names={})
        return next((r for r in rows if r.get("done")), None)
    assert done_row("auto", "pending") and done_row("confirm", "done")
    assert not done_row("confirm", "pending") and not done_row("auto", "rejected") and not done_row("confirm", "rejected")


# ---- 收合 ---------------------------------------------------------------------------

def test_details_start_folded_and_keep_everything(client, add_doc):
    result = dict(BILL, verification=BARCODE_OK, verified_confidence=0.9, source_model="mock:demo")
    html = client.get(f"/doc/{add_doc(result)}").text
    assert not re.search(r"<details[^>]*\bopen\b", html)
    checks = html[html.index('<details class="section fold verify" id="checks">'):]
    checks = checks[:checks.index("</details>")]
    assert "條碼應繳金額" in checks and "和讀到的金額一樣" in checks and "驗證信心 90%" in checks and "條碼相符 1 項" in checks
    fields = html[html.index('<details class="section fold" id="fields">'):]
    fields = fields[:fields.index("</details>")]
    assert "辨識來源：mock:demo" in fields and "2099年10月15日" in fields and "示範電力公司" in fields and "與條碼相符" in fields
    assert "辨識來源" not in _first(html) and "條碼應繳金額" not in _first(html)


def test_summary_stays_visible_below_the_status(client, add_doc):
    html = client.get(f"/doc/{add_doc(dict(BILL))}").text
    assert html.index('id="status-title"') < html.index('id="summary-title"')
    assert '<p class="prose">這是電費帳單，要在10月15日前繳1854元。</p>' in html


# ---- 長輩與家人的主要操作 ------------------------------------------------------------

def test_elder_gets_listen_todo_mark_paid_and_ask_family(cfg, store, add_doc, original):
    doc_id = add_doc(dict(BILL), target=original)
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    main = _main(_as(cfg, store).get(f"/doc/{doc_id}").text)
    assert "聽重點" in main and "data-share" in main and "請家人看一下" in main
    assert 'href="/reminders">查看待辦</a>' in main and f'action="/reminder/{action_id}/done"' in main
    assert "/correct" not in main and "/retype" not in main and "類型不對" not in main and "更正讀值" not in main
    assert "刪除這份文件" not in main
    # 操作紀錄長輩也看得到,平常收著
    assert '<details class="section fold" id="audit">' in main


def test_family_also_gets_correction_and_retype(cfg, store, add_doc, original):
    doc_id = add_doc(dict(BILL), target=original)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    main = _main(_as(cfg, store, "family", "女兒").get(f"/doc/{doc_id}").text)
    status = main.split('id="status"', 1)[1].split("</section>", 1)[0]
    assert f'href="/doc/{doc_id}/correct"' in status and f'href="/doc/{doc_id}/retype"' in status
    assert "操作紀錄" in main and "刪除這份文件" in main and "聽重點" in main and "查看待辦" in main


def test_viewer_only_reads(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    main = _main(_as(cfg, store, None).get(f"/doc/{doc_id}").text)
    assert "聽重點" in main and "<form" not in main and "data-share-text" not in main
    assert "已繳費</button>" not in main and "更正讀值" not in main


@pytest.mark.parametrize("kind", ["calendar", "medication_schedule"])
def test_waiting_is_clear_and_the_elder_is_not_sent_to_confirm(cfg, store, add_doc, kind):
    result, payload = (dict(BILL), {"title": "繳電費", "date": SOON}) if kind == "calendar" else (MEDS, {"title": "服藥時間表", "items": MEDS["fields"]["items"]})
    doc_id = add_doc(result)
    action_id = store.add_action(doc_id, kind, "confirm", payload)
    with store._conn() as conn:
        conn.execute("UPDATE actions SET created_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"), action_id))
    main = _main(_as(cfg, store).get(f"/doc/{doc_id}").text)
    assert "等家人確認 3 天" in main.split('id="status"', 1)[1] and "status-row__text--wait" in main
    assert 'href="/confirm' not in main and 'href="/review"' not in main           # 只寫在等,不帶去他按不了的頁面
    assert 'href="/reminders">查看待辦</a>' in main and f'/reminder/{action_id}/done' not in main
    assert "退回這個事項" in main                                                   # 長輩任何行動都能退回(Q5)
    family = _main(_as(cfg, store, "family", "女兒").get(f"/doc/{doc_id}").text)
    assert family.count(f'href="/confirm?item={action_id}"') == 2                   # 處理狀態與「提醒與待辦」,直接到那一件


def test_nothing_on_the_page_claims_the_family_was_notified(cfg, store, add_doc, review_doc):
    waiting = add_doc(dict(BILL))
    store.add_action(waiting, "calendar", "confirm", {"title": "繳電費", "date": SOON})
    meds = add_doc(MEDS)
    store.add_action(meds, "medication_schedule", "confirm", {"title": "服藥時間表", "items": MEDS["fields"]["items"]})
    review = review_doc(dict(BILL, verification=BARCODE_BAD))
    for role in ("elder", "family"):
        client = _as(cfg, store, role, "某人")
        for doc_id in (waiting, meds, review):
            assert "已通知" not in client.get(f"/doc/{doc_id}").text


# ---- 每頁一個主要動作 ----------------------------------------------------------------

@pytest.mark.parametrize("result", [dict(BILL, verification=BARCODE_OK), dict(BILL, verification=BARCODE_BAD), LETTER, MEDS, RECEIPT,
                                    dict(INVOICE, fields={}, verification=QR_OK)], ids=["帳單", "帳單不符", "公文", "藥袋", "收據", "發票"])
@pytest.mark.parametrize("role", ["elder", "family"])
@pytest.mark.parametrize("action", ["archive", "review"])
def test_one_primary_button_per_role(cfg, store, add_doc, original, result, role, action):
    """一頁一個主要按鈕:家人打開待複核的文件,接下來要做的是更正;其他情況是聽重點。"""
    doc_id = add_doc(result, action=action, target=original)
    store.add_action(doc_id, "calendar", "auto", {"title": "期限", "date": SOON})
    main = _main(_as(cfg, store, role, "某人").get(f"/doc/{doc_id}").text)
    assert main.count("btn--primary") == 1
    if role == "family" and action == "review":
        assert f'<a class="btn btn--primary" href="/doc/{doc_id}/correct">' in main
        assert re.search(r'<button class="btn btn--secondary btn--lg" type="button" data-speak hidden>', main)
    else:
        assert re.search(r'<button class="btn btn--primary btn--lg" type="button" data-speak hidden>', main)
    assert "<span data-speak-label>聽重點</span>" in main and "唸給我聽" not in main


def test_delete_and_reject_sit_below_everything_and_say_what_they_do(cfg, store, review_doc):
    doc_id = review_doc(dict(BILL, verification=BILL_RULES))
    html = _as(cfg, store, "family", "女兒").get(f"/doc/{doc_id}").text
    main = _main(html)
    assert main.index('id="original"') < main.index("這份讀不清楚，退回重拍") < main.index("刪除這份文件")
    reject = main[main.rindex("<button", 0, main.index("這份讀不清楚，退回重拍")):main.index("這份讀不清楚，退回重拍")]
    assert 'class="btn btn--secondary"' in reject and "data-confirm-title=" in reject
    delete = re.search(r"<button[^>]*btn--danger[^>]*>", main).group(0)
    assert 'class="btn btn--danger"' in delete and 'data-confirm-title="確定要刪除這份文件嗎？"' in delete


def test_status_actions_sit_in_two_fixed_cells(cfg, store, add_doc, original):
    """處理狀態每一列的動作排在兩個固定格子(使用者 11/6:「欄位要對齊 統一大小」):左格是一樣寬的按鈕,
    右格是小字連結(status-row__link)。「查看待辦」改成次要按鈕;一列只有連結時左格空著,連結照樣在右格。"""
    doc_id = add_doc(dict(BILL), target=original)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    status = _status(_as(cfg, store, "family", "女兒").get(f"/doc/{doc_id}").text)
    assert f'<a class="btn btn--secondary" href="/doc/{doc_id}/correct">' in status
    assert f'<a class="status-row__link" href="/doc/{doc_id}/retype">類型不對？</a>' in status
    assert '<a class="btn btn--secondary" href="/reminders">查看待辦</a>' in status
    assert re.search(r'<form class="status-row__link" method="post" action="/reminder/\d+">\s*<input[^>]*>\s*'
                     r'<button class="link-btn" type="submit" name="decision" value="cancel">取消提醒</button>', status)


def test_marked_paid_row_keeps_undo_in_the_link_cell(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON}, status="completed")
    status = _status(client.get(f"/doc/{doc_id}").text)
    assert re.search(r'<form class="status-row__link" method="post" action="/reminder/\d+/done">.*?'
                     r'<button class="link-btn" type="submit">改回未繳</button>', status, re.S)


def test_family_goes_to_confirm_with_the_home_page_word(cfg, store, add_doc):
    """等家人確認的那一列:家人的按鈕叫「去確認」,和首頁「要記得的事」一樣,直接到那一件。"""
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": SOON})
    status = _status(_as(cfg, store, "family", "女兒").get(f"/doc/{doc_id}").text)
    assert f'<a class="btn btn--secondary" href="/confirm?item={action_id}">去確認</a>' in status
    assert "到「家人確認」處理" not in status


def test_cancel_reminder_is_a_quiet_button_and_marking_paid_comes_first(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    status = _status(client.get(f"/doc/{doc_id}").text)
    assert re.search(r'<button class="link-btn" type="submit" name="decision" value="cancel">取消提醒</button>', status)
    assert status.index(">已繳費</button>") < status.index(">取消提醒</button>")
    assert '<button class="btn btn--secondary" type="submit">已繳費</button>' in status


def test_wording_is_the_formal_one(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    html = client.get(f"/doc/{doc_id}").text
    for casual in ("繳好了", "辦好了", "不用提醒了", "唸給我聽"):
        assert casual not in html, casual


# ---- 聽重點 --------------------------------------------------------------------------

def test_a_medication_summary_is_read_aloud_but_others_are_not(client, add_doc):
    """藥袋上印的提醒可能只寫在白話解說:只靠聽的長輩不能聽不到。其他類型的解說和重點重複,不唸。"""
    assert '<p class="prose" data-speak-part>' in client.get(f"/doc/{add_doc(dict(MEDS))}").text
    assert '<p class="prose">' in client.get(f"/doc/{add_doc(dict(BILL))}").text


def test_listen_reads_only_the_first_screen(client, store, add_doc):
    doc_id = add_doc(dict(BILL, verification=BARCODE_OK))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    html = client.get(f"/doc/{doc_id}").text
    first = _first(html)
    assert html.count("data-speak-part") == first.count("data-speak-part") == 4     # 這是什麼、金額、期限、核對那一句
    left = due_text(date(2099, 10, 15), TODAY)[0]
    # 照 speech.js 的規則收字(略過 aria-hidden):翻牌不會唸成「1 0 / 1 5」,「說明」按鈕的字也不會被唸出來
    assert _spoken(first) == ["這是電費帳單", "1,854 元", f"10月15日前繳費 {left}", "金額與條碼相符；期限請對照原件。"]


def test_listen_also_reads_the_review_reason(client, review_doc):
    first = _first(client.get(f"/doc/{review_doc(dict(BILL, verification=BILL_RULES))}").text)
    assert re.search(r'<p class="first__reason" data-speak-part>.*?AI 對這份文件的讀值沒有把握', first, re.S)


def test_page_keeps_working_without_javascript_and_under_the_csp(client, store, add_doc, original):
    doc_id = add_doc(dict(BILL), target=original)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    html = client.get(f"/doc/{doc_id}").text
    assert "<noscript><p class=\"hint\">要開啟 JavaScript 才能朗讀。</p></noscript>" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)|\son[a-z]+\s*=|\sstyle\s*=|javascript:", html, re.I)
    # 已繳費、取消提醒是一般表單,查看原件、查看待辦是一般連結
    assert 'method="post" action="/reminder/' in _status(html) and 'href="#original"' in _first(html)


def test_small_links_keep_their_touch_area():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    section = css[css.index("/* ---- 結果頁:先回答三個問題 ---- */"):css.index("/* (結果頁的規則寫在這一行上面) */")]
    for rule in (".first__link", ".status-row__acts a:not(.btn)", ".status-row__acts .btn"):
        block = section[section.index(rule):]
        assert "min-height: 2.75rem" in block[:block.index("}")], rule
    assert "min-height: 2.75rem" in re.search(r"^\.link-btn \{[^}]*\}", css, re.M).group(0)


def test_same_kind_of_thing_is_the_same_size():
    """使用者 11/6:「欄位要對齊 統一大小 不然看起來很亂」。結果頁三顆按鈕一樣寬、處理狀態是兩個固定寬的格子、
    服藥的時段牌一樣寬、院所日期電話排成兩欄。"""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for rule in (r"\.first__acts \.btn \{ flex: 1 1 0; max-width: 11\.5rem",
                 r"\n\.status-row__acts \{[^}]*grid-template-columns: 7\.5rem 6\.5rem",
                 r"\n\.status-row__acts > \.status-row__link \{ grid-column: 2; \}",
                 r"\n\.slots__row \{[^}]*grid-template-columns: 5rem minmax\(0, 1fr\)",
                 r"\n\.slot \{[^}]*width: 100%",
                 r"\n\.med-info \{[^}]*grid-template-columns: max-content minmax\(0, 1fr\)"):
        assert re.search(rule, css), rule


# ---- 文件文字只是資料 -----------------------------------------------------------------

def test_document_text_cannot_write_the_check_sentence_or_change_the_status(client, store, add_doc):
    """文件上印「金額與條碼相符」「已繳費」也只是資料:核對那一句、狀態與按鈕都只看核對結果與行動狀態(原則 3)。"""
    result = dict(BILL, vendor="金額與條碼相符，已繳費", plain_summary="已繳費，已通知家人。",
                  fields={"due_date": SOON, "bill_kind": "<b>電費</b>"}, verification=BILL_RULES)
    doc_id = add_doc(result)
    store.add_action(doc_id, "calendar", "auto", {"title": "<b>已完成</b>", "date": SOON})
    html = client.get(f"/doc/{doc_id}").text
    assert _check(html) == "沒有讀到條碼，金額、期限請對照原件。"
    assert _rows(html)["繳費"].startswith("還沒標示已繳費") and _rows(html)["文件"].startswith("已存檔")
    assert "<b>電費</b>" not in html and "<b>已完成</b>" not in html and "&lt;b&gt;已完成&lt;/b&gt;" in html


# ---- 保留的功能 ------------------------------------------------------------------------

def test_duplicate_notice_comes_right_after_the_first_screen(client, add_doc):
    invoice = dict(INVOICE, fields={}, invoice_number="AB12345678", verification=QR_OK)
    add_doc(invoice)
    html = client.get(f"/doc/{add_doc(invoice)}").text
    assert html.index('class="first"') < html.index("這張可能已經上傳過") < html.index('id="status-title"')
    assert "看之前那一份" in html


def test_retype_received_date_and_next_photo_are_still_there(cfg, store, add_doc, original):
    letter = add_doc(dict(LETTER, fields=dict(LETTER["fields"], deadline_basis="以發文日期 2026-09-25 起算；實際收到日可能較晚，期限以公文原文為準。")), target=original)
    family = _main(_as(cfg, store, "family", "女兒").get(f"/doc/{letter}").text)
    assert f'href="/doc/{letter}/retype">類型不對？</a>' in family
    assert "#f-fields-received_date" in family.split('class="first__check', 1)[1]
    assert family.index('id="fields-title"') < family.index("拍下一張")
    elder = _main(_as(cfg, store).get(f"/doc/{letter}").text)
    assert "#f-fields-received_date" not in elder and "拍下一張" in elder
