"""參考產品的優化(使用者 11/4 選了四組):
長輩與家人的交接、提醒與結果頁、家人核對、文件櫃搜尋。身分與文件都是合成的,不呼叫模型。"""
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from samples import BILL, LETTER
from src.providers import MockAnalyzer
from src.review import correct_document
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import answer_view, due_text, reminder_rows, verification_view, waiting_text

TODAY = date.today()
SOON = (TODAY + timedelta(days=10)).isoformat()


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


def _remind(html):
    end = html.index('id="done-title"') if 'id="done-title"' in html else html.index('id="recent-title"')
    return re.sub(r'<details class="note">.*?</details>', "", html[html.index('id="remind-title"'):end], flags=re.S)


def _deadline(action_id, day, **extra):
    return {"id": action_id, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "pending",
            "superseded": 0, "payload": {"title": "繳電費", "date": day}, **extra}


# ---- 1. 退回之後要讓對方知道(Rossum) ---------------------------------------------

def test_rejected_document_tells_the_elder_to_retake(cfg, store, review_doc):
    doc_id = review_doc(dict(BILL))
    family, elder = _as(cfg, store, "family", "女兒"), _as(cfg, store)
    from web.app import CONFIRM_REJECT_FIELD, CONFIRMED
    assert _post(family, f"/doc/{doc_id}/reject", **{CONFIRM_REJECT_FIELD: CONFIRMED}).status_code == 303
    section = _remind(elder.get("/").text)
    assert "請重新拍：帳單" in section and f"女兒 {TODAY.month}/{TODAY.day} 退回" in section and "要重拍" in section
    assert f'href="/doc/{doc_id}"' in section and "1,854" not in section          # 不寫可能讀錯的金額
    assert "請重新拍" not in _remind(family.get("/").text)                        # 自己退回的不必再告訴自己


def test_seen_hides_the_notice_once_and_records_who(cfg, store, review_doc):
    doc_id = review_doc(dict(BILL))
    family, elder = _as(cfg, store, "family", "女兒"), _as(cfg, store)
    from web.app import CONFIRM_REJECT_FIELD, CONFIRMED
    _post(family, f"/doc/{doc_id}/reject", **{CONFIRM_REJECT_FIELD: CONFIRMED})
    event_id = int(re.search(r'action="/rejected/(\d+)/seen"', elder.get("/").text).group(1))
    r = _post(elder, f"/rejected/{event_id}/seen")
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    assert "請重新拍" not in _remind(elder.get("/").text)
    _post(elder, f"/rejected/{event_id}/seen")                                    # 連按兩次只記一次
    seen = [e for e in store.list_audit_events(document_id=doc_id) if e["event"] == "seen_reject"]
    assert len(seen) == 1 and (seen[0]["actor"], seen[0]["detail"]) == ("elder@example.test", {"event_id": event_id})
    assert _post(elder, f"/rejected/{event_id}/seen", back="all").headers["location"].startswith("/reminders?msg=")


def test_only_reject_events_can_be_marked_seen_and_only_by_members(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    other = store.add_audit_event("category", document_id=doc_id, actor="family@example.test", role="family")
    elder = _as(cfg, store)
    assert _post(elder, f"/rejected/{other}/seen").status_code == 404
    assert _post(elder, "/rejected/99999/seen").status_code == 404
    reject = store.add_audit_event("reject_document", document_id=doc_id, actor="family@example.test", role="family")
    assert _post(_as(cfg, store, None), f"/rejected/{reject}/seen").status_code == 403


def test_rejected_action_is_shown_to_the_other_person(cfg, store, add_doc):
    doc_id = add_doc({"doc_type": "藥袋", "vendor": "示範診所"})
    action_id = store.add_action(doc_id, "medication_schedule", "confirm", {"title": "示範診所的服藥時間表", "slots": []})
    store.set_action_status(action_id, "rejected", event="reject", actor="family@example.test", role="family")
    section = _remind(_as(cfg, store).get("/").text)
    assert "示範診所的服藥時間表" in section and "已退回" in section and "退回" in section


def test_notice_goes_away_when_it_no_longer_applies(cfg, store, add_doc):
    """文件後來讀好(不再是讀不出來)、事項被取代或恢復,或是超過 30 天,就不再列。"""
    doc_id = add_doc(dict(BILL))              # 已存檔,不是讀不出來
    store.add_audit_event("reject_document", document_id=doc_id, actor="family@example.test", role="family")
    action_id = store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": SOON})
    store.set_action_status(action_id, "rejected", event="reject", actor="family@example.test", role="family")
    store.replace_actions(doc_id, [])         # 家人更正:舊的被取代
    old = add_doc(dict(BILL), action="failed")
    event = store.add_audit_event("reject_document", document_id=old, actor="family@example.test", role="family")
    with store._conn() as conn:
        conn.execute("UPDATE audit_events SET created_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=31)).isoformat(timespec="seconds"), event))
    section = _remind(_as(cfg, store).get("/").text)
    assert "請重新拍" not in section and "已退回" not in section


# ---- 2. 請家人看一下(Be My Eyes) ---------------------------------------------------

def test_share_button_carries_no_document_content(client, add_doc):
    doc_id = add_doc(dict(BILL))
    html = client.get(f"/doc/{doc_id}").text
    button = re.search(r"<button[^>]*data-share[^>]*>[^<]*</button>", html).group(0)
    assert 'data-share-text="請幫我看這份文件"' in button and " hidden" in button     # 有 JS 才露出來
    assert "請家人看一下" in button and "示範電力公司" not in button and "1,854" not in button
    assert "data-share-status" in html


def test_viewers_do_not_get_the_share_button(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    assert "請家人看一下" not in _as(cfg, store, None).get(f"/doc/{doc_id}").text


def test_share_script_falls_back_to_copying_and_ignores_cancel():
    from web.render import STATIC_DIR
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    block = script[script.index("data-share:"):script.index("上傳後的進度頁")]
    assert "navigator.share" in block and "clipboard.writeText" in block and "AbortError" in block
    assert "location.pathname" in block and "fetch(" not in block                # 只傳網址,不送任何東西到伺服器


# ---- 4. 期限還有幾天、等了幾天(doxo) --------------------------------------------------

@pytest.mark.parametrize("days, text, cls", [(-2, "逾期 2 天", "manual"), (0, "今天到期", "wait"), (1, "明天到期", "wait"),
                                             (3, "還有 3 天", "wait"), (4, "還有 4 天", ""), (30, "還有 30 天", "")])
def test_due_text(days, text, cls):
    assert due_text(TODAY + timedelta(days=days), TODAY) == (text, cls)
    assert due_text(None, TODAY) == ("", "")


def test_list_says_soon_only_within_three_days():
    rows = reminder_rows([{"id": 1, "result": dict(BILL)}],
                         [_deadline(1, (TODAY + timedelta(days=2)).isoformat()), _deadline(2, (TODAY + timedelta(days=9)).isoformat())],
                         TODAY)
    assert [(r["note"], r["note_class"]) for r in rows] == [("還有 2 天", "wait"), ("", "")]


def test_waiting_says_how_many_days():
    assert waiting_text({"created_at": (datetime.now() - timedelta(days=3)).isoformat()}, TODAY) == "等家人確認 3 天"
    assert waiting_text({"created_at": datetime.now().isoformat()}, TODAY) == "等待家人確認"
    assert waiting_text({}, TODAY) == "等待家人確認"


def test_confirm_card_says_how_long_it_has_waited(client, store, add_doc):
    action_id = store.add_action(add_doc(dict(BILL)), "calendar", "confirm", {"title": "繳電費", "date": SOON})
    with store._conn() as conn:
        conn.execute("UPDATE actions SET created_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"), action_id))
    assert "已等 2 天" in client.get("/confirm").text
    assert "等家人確認 2 天" in _remind(client.get("/").text)


def _answer(result, *raw):
    from web.render import action_view
    return answer_view(result, [action_view(a) for a in raw], verification_view(result), TODAY)


def test_deadline_box_says_how_long_is_left_while_the_reminder_is_live():
    bill = dict(BILL, fields={"due_date": (TODAY + timedelta(days=2)).isoformat(), "bill_kind": "電費"})
    live = {"id": 7, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "pending", "payload": {}}
    due = next(r for r in _answer(bill, live)["lines"] if r["key"] == "fields.due_date")
    assert (due["note"], due["note_class"]) == ("還有 2 天", "wait")
    # 已繳費改寫「已繳費」、取消提醒就不寫,免得已經繳的寫「逾期」
    for status, note in (("completed", "已繳費"), ("rejected", "")):
        due = next(r for r in _answer(bill, dict(live, status=status))["lines"] if r["key"] == "fields.due_date")
        assert (due["note"], due["note_class"]) == (note, "")


def test_result_page_shows_the_days_left(client, store, add_doc):
    late = dict(BILL, fields={"due_date": (TODAY - timedelta(days=3)).isoformat(), "bill_kind": "電費"})
    doc_id = add_doc(late)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": late["fields"]["due_date"]})
    assert '<span class="tile tile--manual">逾期 3 天</span>' in client.get(f"/doc/{doc_id}").text


# ---- 5. 防詐的一句:使用者 11/4 驗收時決定不放 -------------------------------------------

def test_no_fraud_line_on_bills_or_letters(client, add_doc):
    for result in (BILL, LETTER):
        html = client.get(f"/doc/{add_doc(dict(result))}").text
        assert "165" not in html and "不判斷真假" not in html


# ---- 6. 已完成與已取消的提醒全部查得到(doxo) --------------------------------------------

def test_all_page_lists_every_completed_and_cancelled_reminder(cfg, store, add_doc):
    old = store.add_action(add_doc(dict(BILL)), "calendar", "auto", {"title": "繳舊電費", "date": SOON})
    store.set_action_status(old, "completed", event="complete_reminder", actor="elder@example.test", role="elder")
    with store._conn() as conn:
        conn.execute("UPDATE audit_events SET created_at = ? WHERE action_id = ?",
                     ((datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"), old))
    gone = store.add_action(add_doc(dict(BILL)), "calendar", "auto", {"title": "繳水費", "date": SOON})
    store.set_action_status(gone, "rejected", event="cancel_reminder", actor="family@example.test", role="family")
    elder = _as(cfg, store)
    assert "繳舊電費" not in elder.get("/").text                                  # 首頁只放 7 天
    html = elder.get("/reminders").text
    done = html[html.index("<h2>已完成</h2>"):html.index("<h2>已取消的提醒</h2>")]
    assert "繳舊電費" in done and "媽媽" in done and 'name="back" value="all"' in done
    off = html[html.index("<h2>已取消的提醒</h2>"):]
    assert "繳水費" in off and "取消" in off and 'class="ritem ritem--off"' in off


def test_nothing_done_or_cancelled_shows_no_folds(client):
    html = client.get("/reminders").text
    assert "<h2>已完成</h2>" not in html and "<h2>已取消的提醒</h2>" not in html


# ---- 8. 拍下一張(生活放大鏡) ---------------------------------------------------------

def test_result_page_ends_with_take_the_next_photo(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    assert 'href="/">' in _as(cfg, store).get(f"/doc/{doc_id}").text.split('id="fields-title"')[1]
    assert "拍下一張" not in _as(cfg, store, None).get(f"/doc/{doc_id}").text


# ---- 3. 手機上原件和要核對的字放一起(Rossum、Paperless) -----------------------------

def test_phone_layout_shows_the_original_on_confirm_cards_and_first_on_forms():
    from web.render import STATIC_DIR
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".confirm__photo { display: none; }" not in css
    assert ".doc-grid--form .doc-side { order: -1; }" in css


# ---- 7. 分清楚 AI 讀的和家人改過的(Rossum) ---------------------------------------------

def test_corrected_fields_show_what_the_ai_read(cfg, store, client, add_doc):
    doc_id = add_doc(dict(BILL))
    correct_document(cfg, store, doc_id, {"amount": 1286.0}, original=None)
    html = client.get(f"/doc/{doc_id}").text
    fields = html[html.index('id="fields-title"'):]
    assert "家人改過，原本讀成 1,854 元" in fields
    assert fields.count("家人改過") == 1                                           # 沒改的欄位不標


def test_retyped_documents_do_not_compare_with_readings_of_another_type():
    from web.render import ai_reading
    doc = {"result": {"doc_type": "收據"}}
    bill_era = {"created_at": "2026-11-01T09:00:00", "before": {"doc_type": "帳單", "amount": 1}}
    assert ai_reading(doc, [bill_era], []) is None
    receipt = {"created_at": "2026-11-02T09:00:00", "before": {"doc_type": "收據", "amount": 2}}
    retype = [{"created_at": "2026-11-03T09:00:00"}]
    assert ai_reading(doc, [receipt], retype) is None                             # 改類型之後才算
    assert ai_reading(doc, [receipt], []) == receipt["before"]


# ---- 9. 更正完看下一份(Paperless) -----------------------------------------------------

def test_after_correcting_offers_the_next_review(client, store, review_doc):
    first = review_doc(dict(BILL), name="a.png")
    second = review_doc(dict(BILL), name="b.png")
    r = client.post(f"/doc/{first}/correct", data={"vendor": "示範電力公司", "date": "2026-09-20", "amount": "1854",
                                                   "fields.due_date": SOON, "fields.bill_kind": "電費"},
                    follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert f'href="/doc/{second}/correct"' in page and "還有 1 份待複核，看下一份" in page
    assert "看下一份" not in client.get(f"/doc/{first}").text                    # 只在剛存完時出現


# ---- 10. 文件櫃搜尋(Paperless) ---------------------------------------------------------

def test_search_needs_every_word_in_any_order(client, add_doc):
    bill = add_doc(dict(BILL))
    letter = add_doc(dict(LETTER))
    html = client.get("/cabinet", params={"q": "電力  示範"}).text
    assert f'href="/doc/{bill}"' in html and f'href="/doc/{letter}"' not in html and "找「電力  示範」" in html
    html = client.get("/cabinet", params={"q": "補繳"}).text                     # 公文主旨
    assert f'href="/doc/{letter}"' in html and f'href="/doc/{bill}"' not in html
    assert "沒有找到" in client.get("/cabinet", params={"q": "不存在的字"}).text


def test_search_is_width_and_case_insensitive_and_escaped(client, add_doc):
    doc_id = add_doc(dict(BILL, invoice_number="AB12345678"))
    assert f'href="/doc/{doc_id}"' in client.get("/cabinet", params={"q": "ａｂ１２３"}).text
    html = client.get("/cabinet", params={"q": "<script>"}).text
    assert "<script>" not in html.split('<main id="main"')[1] and "&lt;script&gt;" in html


def test_search_stays_inside_the_chosen_category(client, store, add_doc):
    bill = add_doc(dict(BILL))
    store.set_document_category(bill, "生活契約")
    html = client.get("/cabinet", params={"q": "示範", "cat": "財產資產"}).text
    assert f'href="/doc/{bill}"' not in html and 'name="cat" value="財產資產"' in html
