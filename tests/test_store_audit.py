"""家庭角色的儲存契約測試:家庭成員(members)與操作紀錄(audit_events)。隔離在 tmp_path,不碰真實 logs/。

只測儲存層「存與查」;誰能做什麼(長輩/家人的權限)由 web 層決定,另有測試。
"""
import sqlite3
from pathlib import Path

import pytest

from src.store import MEMBER_CHANGE_PREFIX, MEMBER_REMOVED, Store

FAMILY = "family@example.com"
ELDER = "elder@example.com"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def _doc(store, name="a.png"):
    return store.add_document({"原始檔案": name, "動作": "archive",
                               "AI辨識結果": {"doc_type": "藥袋", "vendor": "範例診所"}})


# ---- 行動狀態 + 操作紀錄 ------------------------------------------------------------------

def test_set_action_status_records_who_and_from_to(store):
    doc_id = _doc(store)
    action_id = store.add_action(doc_id, "medication_schedule", "confirm", {})
    assert store.set_action_status(action_id, "done", event="confirm", actor=FAMILY, role="family") is True
    [event] = store.list_audit_events(document_id=doc_id)
    assert (event["event"], event["action_id"], event["actor"], event["role"]) == ("confirm", action_id, FAMILY, "family")
    assert event["detail"] == {"from": "pending", "to": "done"}


def test_set_action_status_without_event_keeps_old_behaviour(store):
    action_id = store.add_action(_doc(store), "calendar", "auto", {})
    assert store.set_action_status(action_id, "rejected") is True
    assert store.list_actions()[0]["status"] == "rejected"
    assert store.list_audit_events() == []                     # 沒給 event 不記(舊的呼叫方式照舊)


def test_same_status_twice_is_not_a_change_and_not_recorded(store):
    action_id = store.add_action(_doc(store), "calendar", "auto", {})
    assert store.set_action_status(action_id, "rejected", event="cancel_reminder", actor=ELDER, role="elder")
    assert store.set_action_status(action_id, "rejected", event="cancel_reminder", actor=ELDER, role="elder") is False
    assert len(store.list_audit_events()) == 1                 # 連按兩次「取消提醒」只記一筆


def test_superseded_action_is_not_changed_or_recorded(store):
    doc_id = _doc(store)
    old = store.add_action(doc_id, "calendar", "auto", {})
    store.replace_actions(doc_id, [])                         # 家人更正或退回:舊行動被取代
    assert store.set_action_status(old, "pending", event="restore_reminder", actor=ELDER, role="elder") is False
    assert store.list_actions()[0]["status"] == "rejected"
    assert store.list_audit_events() == []


def test_unknown_action_returns_false(store):
    assert store.set_action_status(999, "done", event="confirm", actor=FAMILY, role="family") is False


# ---- 更正 + 操作紀錄 -----------------------------------------------------------------------

def test_add_correction_records_who_corrected_and_links_the_correction(store):
    doc_id = _doc(store)
    correction_id = store.add_correction({"amount": 1268}, {"amount": 1286}, document_id=doc_id,
                                         actor=FAMILY, role="family")
    [event] = store.list_audit_events(document_id=doc_id)
    assert (event["event"], event["actor"], event["role"]) == ("correct", FAMILY, "family")
    assert event["detail"] == {"correction_id": correction_id}


def test_correction_without_document_has_no_audit_event(store):
    store.add_correction({"amount": 1}, {"amount": 2})
    assert store.list_audit_events() == []


# ---- 其他操作與查詢 -------------------------------------------------------------------------

def test_reject_document_event_and_history_newest_first(store):
    doc_id, other = _doc(store), _doc(store, "b.png")
    action_id = store.add_action(doc_id, "calendar", "auto", {})
    store.set_action_status(action_id, "rejected", event="cancel_reminder", actor=ELDER, role="elder")
    store.add_audit_event("reject_document", document_id=doc_id, actor=FAMILY, role="family")
    store.add_audit_event("reject_document", document_id=other)          # 本機直接開:沒有登入者與角色
    assert [e["event"] for e in store.list_audit_events(document_id=doc_id)] == ["reject_document", "cancel_reminder"]
    assert len(store.list_audit_events()) == 3
    assert store.list_audit_events(document_id=other)[0]["actor"] is None


@pytest.mark.parametrize("kwargs", [{"event": "pay"}, {"event": "confirm", "role": "admin"}])
def test_unknown_event_or_role_is_rejected(store, kwargs):
    with pytest.raises(ValueError):
        store.add_audit_event(document_id=_doc(store), **kwargs)
    assert store.list_audit_events() == []


def test_unknown_role_rolls_back_the_status_change(store):
    action_id = store.add_action(_doc(store), "calendar", "auto", {})
    with pytest.raises(ValueError):
        store.set_action_status(action_id, "rejected", event="cancel_reminder", role="admin")
    assert store.list_actions()[0]["status"] == "pending"     # 同一個交易:紀錄寫不進去,狀態也不改


# ---- 家庭成員 -------------------------------------------------------------------------------

def test_members_are_case_insensitive_and_changes_are_logged(store):
    assert store.set_member(" Family@Example.com ", "family", "大兒子", actor=FAMILY) is True
    assert store.set_member(ELDER, "elder", "媽媽", actor=FAMILY) is True
    assert store.get_member("FAMILY@example.com")["role"] == "family"
    assert [m["email"] for m in store.list_members()] == [FAMILY, ELDER]          # 家人在前
    assert store.set_member(FAMILY, "family", "大兒子") is False                  # 沒變不寫
    assert store.set_member(FAMILY, "elder", "大兒子", actor=FAMILY) is True
    changes = store.list_setting_changes()
    assert (changes[0]["key"], changes[0]["old_value"], changes[0]["new_value"], changes[0]["actor"]) == (
        MEMBER_CHANGE_PREFIX + FAMILY, "family", "elder", FAMILY)


def test_remove_member(store):
    store.set_member(ELDER, "elder")
    assert store.remove_member(ELDER.upper(), actor=FAMILY) is True
    assert store.get_member(ELDER) is None and store.remove_member(ELDER) is False
    assert store.list_setting_changes()[0]["new_value"] == MEMBER_REMOVED


@pytest.mark.parametrize("email, role", [("", "family"), ("   ", "elder"), (FAMILY, "admin")])
def test_set_member_rejects_blank_email_or_unknown_role(store, email, role):
    with pytest.raises(ValueError):
        store.set_member(email, role)
    assert store.list_members() == []


def test_get_member_of_nobody(store):
    assert store.get_member(None) is None and store.get_member("") is None


def test_purge_all_keeps_members(store):
    store.set_member(ELDER, "elder")
    doc_id = _doc(store)
    store.add_audit_event("reject_document", document_id=doc_id)
    assert store.purge_all()["audit_events"] == 1
    assert store.get_member(ELDER)["role"] == "elder"        # 家庭成員和設定一樣保留


# ---- 舊資料庫 -------------------------------------------------------------------------------

def test_existing_database_gets_the_new_tables(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:                     # 10/5 以前的資料庫:沒有 members、audit_events
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                     "source_file TEXT NOT NULL, action TEXT NOT NULL)")
    store = Store(path)
    store.set_member(ELDER, "elder")
    assert store.list_members()[0]["email"] == ELDER and store.list_audit_events() == []


# ---- 刪除單份文件 ---------------------------------------------------------------------------

def test_delete_document_removes_only_that_document(store):
    from src.store import DOCUMENT_DELETED
    keep, gone = _doc(store, "keep.png"), _doc(store, "gone.png")
    for doc_id in (keep, gone):
        action_id = store.add_action(doc_id, "medication_schedule", "confirm", {})
        store.set_action_status(action_id, "done", event="confirm", actor=FAMILY, role="family")
        store.add_correction({"before": 1}, {"after": 2}, document_id=doc_id, doc_type="藥袋", verified=True,
                             source="web_correct", actor=FAMILY, role="family")
    assert store.delete_document(gone, actor=FAMILY) is True
    assert store.get_document(gone) is None and store.get_document(keep) is not None
    assert {a["document_id"] for a in store.list_actions()} == {keep}
    assert {c["document_id"] for c in store.list_corrections()} == {keep}
    assert {e["document_id"] for e in store.list_audit_events()} == {keep}
    [change] = [c for c in store.list_setting_changes() if c["key"] == DOCUMENT_DELETED]
    assert (change["old_value"], change["actor"]) == (str(gone), FAMILY)     # 只記編號,不記內容


def test_delete_unknown_document_changes_nothing(store):
    from src.store import DOCUMENT_DELETED
    assert store.delete_document(999, actor=FAMILY) is False
    assert not [c for c in store.list_setting_changes() if c["key"] == DOCUMENT_DELETED]
