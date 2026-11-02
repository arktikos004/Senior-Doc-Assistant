"""「繳好了/辦好了」:長輩或家人把生效中的期限提醒標成完成,移到首頁「已完成」(7 天內),可以改回。
只是記下來讓家人知道:不付款、不查核;操作紀錄記下是誰、什麼時候。身分與文件都是合成的。"""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from samples import BILL, LETTER
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import completed_rows

DUE = (date.today() + timedelta(days=10)).isoformat()


def _reminder(store, add_doc, result=BILL, title="繳電費", tier="auto", status="pending"):
    doc_id = add_doc(dict(result))
    action_id = store.add_action(doc_id, "calendar", tier, {"title": title, "date": DUE}, status=status)
    return doc_id, action_id


def _member(cfg, store, role="elder", name="媽媽"):
    email = f"{role}@example.test"
    store.set_member(email, role, name)
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))
    c.get("/")
    return c


def _post(c, action_id, decision):
    return c.post(f"/reminder/{action_id}/done", data={CSRF_FIELD: c.cookies[CSRF_COOKIE], "decision": decision},
                  follow_redirects=False)


def test_bills_say_paid_and_letters_say_done(client, store, add_doc):
    _reminder(store, add_doc)
    _reminder(store, add_doc, LETTER, "公文期限：補件")
    html = client.get("/").text
    assert "繳好了" in html and "辦好了" in html


def test_elder_marks_a_bill_paid_and_the_family_sees_who_and_when(cfg, store, add_doc):
    doc_id, action_id = _reminder(store, add_doc)
    elder = _member(cfg, store)
    r = _post(elder, action_id, "complete")
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    assert [a["status"] for a in store.list_actions(document_id=doc_id)] == ["completed"]
    [event] = store.list_audit_events(document_id=doc_id)
    assert (event["event"], event["actor"], event["role"]) == ("complete_reminder", "elder@example.test", "elder")

    home = _member(cfg, store, "family", "女兒").get("/").text
    done = home[home.index('id="done-title"'):]
    assert "繳電費" in done and "媽媽" in done and "按了「繳好了」" in done and "還沒繳" in done
    upcoming = home[home.index('id="remind-title"'):home.index('id="done-title"')]
    assert "繳電費" not in upcoming
    page = elder.get(f"/doc/{doc_id}").text
    assert "已完成" in page and "取消提醒" not in page


def test_undo_puts_it_back_on_the_list(cfg, store, add_doc):
    doc_id, action_id = _reminder(store, add_doc)
    elder = _member(cfg, store)
    _post(elder, action_id, "complete")
    assert _post(elder, action_id, "undo").status_code == 303
    assert store.list_actions(document_id=doc_id)[0]["status"] == "pending"      # 自動提醒回到生效
    events = [e["event"] for e in store.list_audit_events(document_id=doc_id)]
    assert events == ["undo_complete", "complete_reminder"]
    home = elder.get("/").text
    assert "繳好了" in home[home.index('id="remind-title"'):]


def test_pressing_twice_is_harmless(cfg, store, add_doc):
    doc_id, action_id = _reminder(store, add_doc)
    elder = _member(cfg, store)
    _post(elder, action_id, "complete")
    assert _post(elder, action_id, "complete").status_code == 303
    assert len(store.list_audit_events(document_id=doc_id)) == 1


def test_waiting_medication_and_unknown_actions_cannot_be_marked(cfg, store, add_doc):
    elder = _member(cfg, store)
    _, waiting = _reminder(store, add_doc, tier="confirm", status="pending")           # 還在等家人確認
    assert _post(elder, waiting, "complete").status_code == 409
    doc_id = add_doc(dict(BILL))
    meds = store.add_action(doc_id, "medication_schedule", "confirm", {"items": []}, status="done")
    assert _post(elder, meds, "complete").status_code == 409
    assert _post(elder, 9999, "complete").status_code == 404
    assert _post(elder, waiting, "pay-now").status_code == 400


def test_family_confirmed_reminder_can_be_marked_and_undone(cfg, store, add_doc):
    doc_id, action_id = _reminder(store, add_doc, tier="confirm", status="done")
    elder = _member(cfg, store)
    _post(elder, action_id, "complete")
    _post(elder, action_id, "undo")
    assert store.list_actions(document_id=doc_id)[0]["status"] == "done"            # 家人確認過的回到「已確認」


def test_viewers_cannot_mark_anything(cfg, store, add_doc):
    _, action_id = _reminder(store, add_doc)

    class Viewer:
        def resolve(self, request):
            return Identity("stranger@example.test", False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Viewer()))
    c.get("/")
    assert _post(c, action_id, "complete").status_code == 403


def test_completed_list_keeps_seven_days():
    today = date(2026, 11, 7)
    docs = [{"id": 1, "result": dict(BILL)}]
    actions = [{"id": 5, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "completed",
                "superseded": 0, "payload": {"title": "繳電費", "date": "2026-11-20"}}]

    def event(days_ago):
        when = datetime(2026, 11, 7, 9, 30) - timedelta(days=days_ago)
        return [{"event": "complete_reminder", "action_id": 5, "document_id": 1, "actor": "elder@example.test",
                 "role": "elder", "created_at": when.isoformat(timespec="seconds"), "detail": {}}]

    names = {"elder@example.test": "媽媽"}
    [row] = completed_rows(docs, actions, event(6), today, names)
    assert (row["title"], row["who"], row["when"]) == ("繳電費", "媽媽", "11/1")
    assert completed_rows(docs, actions, event(8), today, names) == []
