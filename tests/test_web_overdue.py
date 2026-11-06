"""待辦不會消失(FIN-HARDEN 第一輪):逾期的提醒留到完成或取消、首頁超過 6 筆有「查看全部」、舊文件的提醒資料完整、
完成多久都能從結果頁改回。身分與文件都是合成的,不呼叫模型。"""
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from samples import BILL, LETTER
from src.models import ExtractionResult
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import HOME_REMINDERS, reminder_rows

TODAY = date.today()
SOON = (TODAY + timedelta(days=10)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
LAST_MONTH = (TODAY - timedelta(days=40)).isoformat()
FAILED_CHECK = {"QR 總計額": {"status": "fail", "fields": ["amount"]}}


def _doc(doc_id, result=BILL, action="archive"):
    return {"id": doc_id, "action": action, "result": dict(result)}


def _deadline(action_id, doc_id=1, day=SOON, tier="auto", status="pending", title="繳電費", superseded=0):
    return {"id": action_id, "document_id": doc_id, "kind": "calendar", "tier": tier, "status": status,
            "superseded": superseded, "payload": {"title": title, "date": day}}


def _remind_section(html):
    """「要記得的事」這一段,不含標題旁的「說明」(裡面提到「已繳費」「已辦理」)。"""
    end = html.index('id="done-title"') if 'id="done-title"' in html else html.index('id="recent-title"')
    return re.sub(r'<details class="note">.*?</details>', "", html[html.index('id="remind-title"'):end], flags=re.S)


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


# ---- 按鈕與入口 ---------------------------------------------------------------

def test_waiting_deadline_has_no_button_that_would_fail(client, store, add_doc):
    """還在等家人確認的期限不能標成完成(POST 會回 409),所以這一列不放「已繳費」,點進去到「家人確認」。"""
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": SOON})
    [row] = reminder_rows([_doc(doc_id)], store.list_actions(), TODAY)
    assert row["href"] == f"/confirm?item={action_id}" and not row.get("done")
    assert f'action="/reminder/{action_id}/done"' not in client.get("/").text


def test_deadline_with_unreadable_date_is_still_listed():
    """期限提醒的日期壞了也不能靜默消失:不寫日期,排在其他期限後面,還是有入口。"""
    rows = reminder_rows([_doc(1)], [_deadline(1, day="不是日期", title="日期壞掉的"), _deadline(2)], TODAY)
    assert [r["title"] for r in rows] == ["繳電費", "日期壞掉的"]
    assert rows[1]["when"] == "" and rows[1]["href"] == "/doc/1" and rows[1]["done"]["id"] == 1


# ---- 核對不符 -----------------------------------------------------------------

def test_rejected_document_no_longer_shows_as_mismatch():
    """家人已退回重拍(讀值留著當紀錄)就是處理過了;還在待複核的才繼續列。"""
    mismatch = dict(BILL, verification=FAILED_CHECK)
    rows = reminder_rows([_doc(1, mismatch, "review"), _doc(2, mismatch, "failed")], [], TODAY)
    assert [r["href"] for r in rows] == ["/doc/1"]


# ---- 超過 30 份文件 ------------------------------------------------------------

def test_reminders_on_old_documents_keep_vendor_amount_and_wording(client, store, add_doc):
    """首頁不能只查最近幾份文件:舊文件的提醒要有商家、金額,帳單的按鈕說「已繳費」,不是「已辦理」。"""
    old = add_doc(dict(BILL))
    store.add_action(old, "calendar", "auto", {"title": "繳電費", "date": SOON})
    for _ in range(31):
        add_doc(dict(LETTER))
    section = _remind_section(client.get("/").text)
    assert "示範電力公司" in section and "1,854 元" in section and "已繳費" in section and "已辦理" not in section


def test_mismatch_on_old_document_is_still_listed(client, add_doc):
    old = add_doc(dict(BILL, verification=FAILED_CHECK), action="review")
    for _ in range(31):
        add_doc(dict(LETTER))
    assert f'href="/doc/{old}"' in _remind_section(client.get("/").text)


# ---- 已完成不被其他操作紀錄擠掉 ----------------------------------------------------

def test_completed_survives_many_other_audit_events(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    store.set_action_status(action_id, "completed", event="complete_reminder", actor="elder@example.test", role="elder")
    for _ in range(250):
        store.add_audit_event("category", document_id=doc_id, actor="family@example.test", role="family")
    html = client.get("/").text
    assert 'id="done-title"' in html and "繳電費" in html[html.index('id="done-title"'):]


def test_audit_events_can_be_filtered_by_event(store, add_doc):
    doc_id, other = add_doc(dict(BILL)), add_doc(dict(BILL))
    store.add_audit_event("category", document_id=doc_id)
    store.add_audit_event("complete_reminder", document_id=doc_id)
    store.add_audit_event("complete_reminder", document_id=other)
    assert [e["document_id"] for e in store.list_audit_events(event="complete_reminder")] == [other, doc_id]
    assert [e["event"] for e in store.list_audit_events(document_id=doc_id, event="category")] == ["category"]


# ---- 逾期:留到完成或取消 ----------------------------------------------------------

def test_yesterday_is_overdue_and_today_is_not():
    rows = reminder_rows([_doc(1)], [_deadline(1, day=SOON, title="還早"), _deadline(2, day=TODAY.isoformat(), title="今天"),
                                     _deadline(3, day=YESTERDAY, title="昨天")], TODAY)
    assert [(r["title"], r["note"], r["note_class"]) for r in rows] == [
        ("昨天", "逾期 1 天", "manual"), ("今天", "今天到期", "wait"), ("還早", "", "")]


def test_only_overdue_rows_can_be_dismissed_from_the_list():
    rows = reminder_rows([_doc(1)], [_deadline(1, day=YESTERDAY), _deadline(2, day=TODAY.isoformat()), _deadline(3)], TODAY)
    assert rows[0]["dismiss"] == {"id": 1, "decision": "cancel", "label": "取消提醒"}
    assert not rows[1].get("dismiss") and not rows[2].get("dismiss")


@pytest.mark.parametrize("gone", [{"status": "completed"}, {"status": "rejected"},
                                  {"status": "rejected", "superseded": 1}, {"superseded": 1}])
def test_overdue_reminder_leaves_only_when_finished(gone):
    """已完成、已取消、被取代的不再列出,不管期限過了多久。"""
    assert reminder_rows([_doc(1)], [_deadline(1, day=YESTERDAY, **gone)], TODAY) == []


def test_overdue_waiting_for_family_goes_to_confirm_without_buttons():
    [row] = reminder_rows([_doc(1)], [_deadline(1, day=YESTERDAY, tier="confirm")], TODAY)
    assert row["href"] == "/confirm?item=1" and row["note"] == "逾期 1 天" and row["group"] == "overdue"
    assert row["sub"] == "示範電力公司，等待家人確認" and not row.get("done") and not row.get("dismiss")


def test_overdue_confirmed_by_family_can_be_marked_but_not_dismissed_from_the_list():
    """家人確認過的只能在結果頁退回(要先問一次),清單上不放一鍵取消。"""
    [row] = reminder_rows([_doc(1)], [_deadline(1, day=YESTERDAY, tier="confirm", status="done")], TODAY)
    assert row["done"]["label"] == "已繳費" and not row.get("dismiss") and row["href"] == "/doc/1"


def test_list_order_is_overdue_then_soon_then_family_help_then_the_rest():
    docs = [_doc(1), _doc(2, dict(BILL, verification=FAILED_CHECK), "review")]
    actions = [_deadline(1, day=SOON, title="還早"),
               {"id": 2, "document_id": 1, "kind": "medication_schedule", "tier": "confirm", "status": "done",
                "superseded": 0, "payload": {"title": "已確認的服藥表", "slots": []}},
               {"id": 3, "document_id": 1, "kind": "medication_schedule", "tier": "confirm", "status": "pending",
                "superseded": 0, "payload": {"title": "等確認的服藥表", "slots": []}},
               _deadline(4, day=LAST_MONTH, title="上個月"), _deadline(5, day=YESTERDAY, title="昨天"),
               _deadline(6, day=TODAY.isoformat(), title="今天")]
    rows = reminder_rows(docs, actions, TODAY)
    assert [r["title"] for r in rows] == ["昨天", "上個月", "今天", "等確認的服藥表", "帳單", "還早", "已確認的服藥表"]
    assert [r["group"] for r in rows] == ["overdue", "overdue", "soon", "family", "family", "other", "other"]
    assert rows[4]["note"] == "核對不符"


class OldBillAnalyzer(MockAnalyzer):
    """拍了一張舊帳單:開單日與繳費期限都已經過了。"""

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        return ExtractionResult.from_dict({
            "doc_type": "帳單", "date": (TODAY - timedelta(days=60)).isoformat(), "vendor": "示範電力公司",
            "amount": 1854.0, "currency": "NTD", "confidence": 0.95, "plain_summary": "舊帳單",
            "fields": {"due_date": LAST_MONTH, "bill_kind": "電費"}})


def test_bill_already_past_due_when_uploaded_is_overdue_and_can_be_dismissed(cfg, store, form_client, png_bytes):
    client = form_client(create_app(cfg, analyzer=OldBillAnalyzer(cfg)))
    r = client.post("/upload", files={"file": ("old.png", png_bytes(), "image/png")}, data={"doc_type": "帳單"},
                    follow_redirects=False)
    doc_id = int(re.fullmatch(r"/doc/(\d+)", r.headers["location"]).group(1))
    [action] = store.list_actions(document_id=doc_id)
    assert (action["kind"], action["tier"], action["status"]) == ("calendar", "auto", "pending")
    section = _remind_section(client.get("/").text)
    assert "逾期 40 天" in section and "取消提醒" in section

    r = client.post(f"/reminder/{action['id']}", data={"decision": "cancel"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{doc_id}?msg=")     # 結果頁可以馬上恢復
    assert "恢復提醒" in client.get(r.headers["location"]).text
    assert "繳電費" not in _remind_section(client.get("/").text)
    client.post(f"/reminder/{action['id']}", data={"decision": "restore"})
    assert "逾期 40 天" in _remind_section(client.get("/").text)


def test_undo_puts_a_completed_reminder_back_as_overdue(cfg, store, add_doc):
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": YESTERDAY})
    elder = _as(cfg, store)
    assert "逾期 1 天" in _remind_section(elder.get("/").text)
    _post(elder, f"/reminder/{action_id}/done", decision="complete")
    assert "逾期 1 天" not in _remind_section(elder.get("/").text)
    _post(elder, f"/reminder/{action_id}/done", decision="undo")
    assert "逾期 1 天" in _remind_section(elder.get("/").text)


def test_confirm_card_says_overdue(client, store, add_doc):
    doc_id = add_doc(dict(BILL))
    store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": YESTERDAY})
    assert "逾期 1 天" in client.get("/confirm").text
    store.add_action(doc_id, "calendar", "confirm", {"title": "繳水費", "date": SOON})
    assert client.get("/confirm").text.count("逾期") == 1


# ---- 首頁超過 6 筆:總數與查看全部 --------------------------------------------------

def _mixed_todo(store, add_doc):
    """9 筆混合待辦:逾期 1、等家人確認 2、期限 4、核對不符 1、已確認服藥表 1。回傳每筆的入口。"""
    entries = []
    overdue = add_doc(dict(BILL))
    store.add_action(overdue, "calendar", "auto", {"title": "過期的", "date": YESTERDAY})
    entries.append(f"/doc/{overdue}")
    for name in ("等確認甲", "等確認乙"):
        store.add_action(add_doc(dict(BILL)), "calendar", "confirm", {"title": name, "date": SOON})
    entries.append("/confirm")
    for days in (0, 3, 6, 9):
        doc_id = add_doc(dict(BILL))
        store.add_action(doc_id, "calendar", "auto", {"title": f"{days} 天後", "date": (TODAY + timedelta(days=days)).isoformat()})
        entries.append(f"/doc/{doc_id}")
    mismatch = add_doc(dict(BILL, verification=FAILED_CHECK), action="review")
    entries.append(f"/doc/{mismatch}")
    meds = add_doc({"doc_type": "藥袋"})
    store.add_action(meds, "medication_schedule", "confirm", {"title": "服藥表", "slots": []}, status="done")
    entries.append(f"/doc/{meds}")
    return entries


def test_home_shows_six_with_the_total_and_a_link_to_all(client, store, add_doc):
    _mixed_todo(store, add_doc)
    section = _remind_section(client.get("/").text)
    assert HOME_REMINDERS == 6 and section.count('class="ritem"') == 6
    assert "共 9 筆" in section and 'href="/reminders"' in section and "查看全部" in section


def test_all_reminders_page_has_an_entry_for_every_row(client, store, add_doc):
    entries = _mixed_todo(store, add_doc)
    html = client.get("/reminders").text
    assert html.count('class="ritem"') == 9 and "共 9 筆" in html
    for href in entries:
        assert f'href="{href}"' in html, href


def test_six_or_fewer_have_no_link_to_all(client, store, add_doc):
    for days in range(HOME_REMINDERS):
        store.add_action(add_doc(dict(BILL)), "calendar", "auto",
                         {"title": "繳電費", "date": (TODAY + timedelta(days=days)).isoformat()})
    section = _remind_section(client.get("/").text)
    assert "共 6 筆" in section and 'href="/reminders"' not in section


def test_nothing_to_remember_shows_no_total(client):
    section = _remind_section(client.get("/").text)
    assert "還沒有要記得的事" in section and "共 0 筆" not in section
    assert "還沒有要記得的事" in client.get("/reminders").text


def test_marking_from_the_all_page_returns_there(cfg, store, add_doc):
    action_id = store.add_action(add_doc(dict(BILL)), "calendar", "auto", {"title": "繳電費", "date": SOON})
    elder = _as(cfg, store)
    assert 'name="back" value="all"' in elder.get("/reminders").text
    r = _post(elder, f"/reminder/{action_id}/done", decision="complete", back="all")
    assert r.status_code == 303 and r.headers["location"].startswith("/reminders?msg=")
    assert "已記下" in elder.get(r.headers["location"]).text


def test_viewers_see_all_reminders_without_buttons(cfg, store, add_doc):
    store.add_action(add_doc(dict(BILL)), "calendar", "auto", {"title": "繳電費", "date": YESTERDAY})
    html = _as(cfg, store, role=None).get("/reminders").text
    assert "繳電費" in html and "已繳費" not in html and "取消提醒" not in html


# ---- 完成多久都能從結果頁改回 -------------------------------------------------------

def _completed_long_ago(store, add_doc, result=BILL, tier="auto", status="pending", days=30):
    doc_id = add_doc(dict(result))
    action_id = store.add_action(doc_id, "calendar", tier, {"title": "繳電費", "date": SOON}, status=status)
    store.set_action_status(action_id, "completed", event="complete_reminder", actor="elder@example.test", role="elder")
    with store._conn() as conn:
        conn.execute("UPDATE audit_events SET created_at = ?",
                     ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"),))
    return doc_id, action_id


def test_result_page_can_undo_long_after_it_left_the_home_page(cfg, store, add_doc):
    doc_id, action_id = _completed_long_ago(store, add_doc)
    family = _as(cfg, store, "family", "女兒")
    assert 'id="done-title"' not in family.get("/").text                     # 首頁只放最近 7 天
    page = family.get(f"/doc/{doc_id}").text
    assert f'action="/reminder/{action_id}/done"' in page and "改回未繳" in page

    r = _post(family, f"/reminder/{action_id}/done", decision="undo", back="doc")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{doc_id}?msg=")
    assert store.list_actions(document_id=doc_id)[0]["status"] == "pending"
    event = store.list_audit_events(document_id=doc_id)[0]
    assert (event["event"], event["actor"], event["role"]) == ("undo_complete", "family@example.test", "family")
    assert "繳電費" in _remind_section(family.get("/").text)                  # 回到要記得的事


def test_letters_say_not_done_yet_and_confirmed_reminders_go_back_to_confirmed(cfg, store, add_doc):
    doc_id, action_id = _completed_long_ago(store, add_doc, LETTER, "confirm", "done")
    elder = _as(cfg, store)
    assert "改回未辦" in elder.get(f"/doc/{doc_id}").text
    _post(elder, f"/reminder/{action_id}/done", decision="undo", back="doc")
    assert store.list_actions(document_id=doc_id)[0]["status"] == "done"


def test_viewers_cannot_undo(cfg, store, add_doc):
    doc_id, action_id = _completed_long_ago(store, add_doc)
    viewer = _as(cfg, store, role=None)
    page = viewer.get(f"/doc/{doc_id}").text
    assert f'action="/reminder/{action_id}/done"' not in page and "改回未繳" not in page
    assert _post(viewer, f"/reminder/{action_id}/done", decision="undo", back="doc").status_code == 403


def test_back_is_a_closed_choice_not_a_url(cfg, store, add_doc):
    """back 只認固定的值,帶外部網址也只會回首頁(不做開放轉址)。"""
    _, action_id = _completed_long_ago(store, add_doc)
    elder = _as(cfg, store)
    r = _post(elder, f"/reminder/{action_id}/done", decision="undo", back="https://evil.example.test/")
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
