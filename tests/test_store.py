"""SQLite 儲存層測試(隔離在 tmp_path,不碰真實 logs/)。"""
from pathlib import Path

import pytest

from src.config import AppConfig, PathsConfig
from src.pipeline import Pipeline
from src.providers import MockAnalyzer
from src.store import Store


def _record(name="a.png", action="archive", **result):
    return {
        "時間": "2026-10-01T10:00:00",
        "原始檔案": name,
        "動作": action,
        "原因": "測試",
        "目標路徑": f"/tmp/{name}",
        "AI辨識結果": {"doc_type": "發票", "amount": 100.0, "currency": "NTD", **result},
        "錯誤": None,
    }


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def test_add_and_get_document(store):
    doc_id = store.add_document(_record(vendor="全聯"))
    doc = store.get_document(doc_id)
    assert doc["source_file"] == "a.png"
    assert doc["vendor"] == "全聯"
    assert doc["result"]["amount"] == 100.0


def test_failed_record_without_result(store):
    rec = _record(action="failed")
    rec["AI辨識結果"] = None
    doc = store.get_document(store.add_document(rec))
    assert doc["action"] == "failed" and doc["result"] is None


def test_list_documents_filters(store):
    store.add_document(_record("a.png"))
    store.add_document(_record("b.png", action="review"))
    assert [d["source_file"] for d in store.list_documents(action="review")] == ["b.png"]


def test_import_records_is_idempotent(store):
    records = [_record("a.png"), _record("b.png")]
    assert store.import_records(records) == 2
    assert store.import_records(records) == 0
    assert len(store.list_documents()) == 2


def test_actions_roundtrip_and_validation(store):
    doc_id = store.add_document(_record())
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費"})
    assert store.list_actions(doc_id)[0]["payload"] == {"title": "繳電費"}
    store.set_action_status(action_id, "done")
    assert store.list_actions(status="done")[0]["id"] == action_id
    with pytest.raises(ValueError):
        store.add_action(doc_id, "calendar", "yolo", {})


def test_unverified_corrections_hidden_by_default(store):
    store.add_correction({"amount": 1}, {"amount": 754}, vendor_key="12345678", verified=False)
    store.add_correction({"amount": 1}, {"amount": 1854}, vendor_key="12345678", verified=True)
    got = store.list_corrections(vendor_key="12345678")
    assert [c["after"]["amount"] for c in got] == [1854]
    assert len(store.list_corrections(vendor_key="12345678", verified_only=False)) == 2


def test_pipeline_writes_document_row(tmp_path):
    paths = PathsConfig(
        inbox=tmp_path / "inbox", archive=tmp_path / "archive", review=tmp_path / "review",
        failed=tmp_path / "failed", logs=tmp_path / "logs",
    )
    cfg = AppConfig(paths=paths)
    cfg.ensure_dirs()
    (cfg.paths.inbox / "清晰發票.png").write_bytes(b"x")
    record = Pipeline(cfg, MockAnalyzer(cfg)).process_file(cfg.paths.inbox / "清晰發票.png")
    doc = Store(cfg.paths.db_path).get_document(record["文件ID"])
    assert doc["action"] == "archive" and doc["doc_type"] == "發票"
    assert cfg.paths.db_path == tmp_path / "logs" / "app.db"


def test_old_database_gets_hint_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # 模擬舊版:沒有 doc_type_hint 欄位
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                     " source_file TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, target_path TEXT,"
                     " doc_type TEXT, date TEXT, vendor TEXT, amount REAL, currency TEXT, result_json TEXT,"
                     " origin TEXT, error TEXT)")
    store = Store(db)
    rec = _record()
    rec["使用者提示"] = "藥袋"
    assert store.get_document(store.add_document(rec))["doc_type_hint"] == "藥袋"


# ---- 家人更正與退回(F7):更新同一筆文件、換掉行動 ------------------------------

def test_update_document_keeps_upload_facts(store):
    rec = _record(action="review", vendor="範例商店")
    rec["使用者提示"] = "發票"
    doc_id = store.add_document(rec)
    store.update_document(doc_id, {
        "時間": "2026-10-02T09:00:00", "原始檔案": "改過的名字.png", "動作": "archive", "原因": "家人更正後存檔",
        "目標路徑": "/tmp/archive/b.png", "AI辨識結果": {"doc_type": "發票", "amount": 120.0, "vendor": "改正商店"},
        "錯誤": None, "來源": "web_correct",
    })
    doc = store.get_document(doc_id)
    assert (doc["action"], doc["reason"], doc["target_path"]) == ("archive", "家人更正後存檔", "/tmp/archive/b.png")
    assert doc["vendor"] == "改正商店" and doc["amount"] == 120.0 and doc["result"]["vendor"] == "改正商店"
    # 上傳時間、原始檔名、來源與類型提示是上傳時的事實,不跟著更正改
    assert (doc["created_at"], doc["source_file"]) == ("2026-10-01T10:00:00", "a.png")
    assert (doc["origin"], doc["doc_type_hint"]) == ("pipeline", "發票")
    assert len(store.list_documents()) == 1


def test_update_unknown_document_raises(store):
    with pytest.raises(LookupError):
        store.update_document(999, _record())


def test_replace_actions_supersedes_every_old_action(store):
    doc_id, other = store.add_document(_record()), store.add_document(_record("b.png"))
    pending = store.add_action(doc_id, "calendar", "auto", {"date": "2026-10-20"})
    done = store.add_action(doc_id, "medication_schedule", "confirm", {}, status="done")
    cancelled = store.add_action(doc_id, "calendar", "auto", {}, status="rejected")
    untouched = store.add_action(other, "calendar", "auto", {})
    (new,) = store.replace_actions(doc_id, [{"kind": "calendar", "tier": "auto", "payload": {"date": "2026-10-25"}}])
    by_id = {a["id"]: a for a in store.list_actions()}
    for old in (pending, done, cancelled):
        assert (by_id[old]["status"], by_id[old]["superseded"]) == ("rejected", True)
    assert (by_id[new]["status"], by_id[new]["superseded"]) == ("pending", False)
    assert by_id[new]["payload"] == {"date": "2026-10-25"}
    assert (by_id[untouched]["status"], by_id[untouched]["superseded"]) == ("pending", False)   # 別份文件不受影響


def test_superseded_action_keeps_its_status(store):
    # 被更正取代的舊行動不能再改狀態:舊頁面上的「恢復提醒」若和更正同時到,不能讓舊期限又生效(SEC-01)
    doc_id = store.add_document(_record())
    old = store.add_action(doc_id, "calendar", "auto", {"date": "2026-10-20"})
    (new,) = store.replace_actions(doc_id, [{"kind": "calendar", "tier": "auto", "payload": {"date": "2026-10-25"}}])

    store.set_action_status(old, "pending")
    store.set_action_status(new, "done")

    by_id = {a["id"]: a for a in store.list_actions()}
    assert (by_id[old]["status"], by_id[old]["superseded"]) == ("rejected", True)
    assert by_id[new]["status"] == "done"   # 沒被取代的照常改


def test_replace_actions_is_all_or_nothing(store):
    doc_id = store.add_document(_record())
    old = store.add_action(doc_id, "calendar", "auto", {})
    with pytest.raises(ValueError):
        store.replace_actions(doc_id, [{"kind": "calendar", "tier": "auto", "payload": {}},
                                       {"kind": "payment", "tier": "yolo", "payload": {}}])
    assert [(a["id"], a["status"], a["superseded"]) for a in store.list_actions()] == [(old, "pending", False)]


def test_count_and_list_all_documents(store):
    for i in range(3):
        store.add_document(_record(f"{i}.png", action="review"))
    store.add_document(_record("x.png"))
    assert store.count_documents(action="review") == 3 and store.count_documents() == 4
    assert len(store.list_documents(action="review", limit=None)) == 3
    assert len(store.list_documents(limit=2)) == 2


def test_corrections_can_be_listed_per_document(store):
    doc_id, other = store.add_document(_record()), store.add_document(_record("b.png"))
    store.add_correction({"amount": 1}, {"amount": 2}, document_id=doc_id, verified=False)
    store.add_correction({"amount": 3}, {"amount": 4}, document_id=other, verified=True)
    assert [c["after"] for c in store.list_corrections(document_id=doc_id, verified_only=False)] == [{"amount": 2}]
    assert store.list_corrections(document_id=doc_id) == []


def test_old_database_gets_superseded_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # 模擬舊版:actions 沒有 superseded 欄位,已有一筆行動
        conn.execute("CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,"
                     " created_at TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT NOT NULL,"
                     " status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT NOT NULL)")
        conn.execute("INSERT INTO actions (document_id, created_at, kind, tier, payload_json)"
                     " VALUES (1, '2026-10-01T10:00:00', 'calendar', 'auto', '{}')")
    store = Store(db)
    assert store.list_actions()[0]["superseded"] is False
    store.replace_actions(1, [])
    assert store.list_actions()[0]["superseded"] is True


# ---- 文件四大類與全家共用的設定(10/2 契約;文件櫃與設定頁共用) --------------------

def test_document_category_is_stored_and_filterable(store):
    rec = _record()
    rec["類別"] = "財產資產"
    with_cat = store.add_document(rec)
    without = store.add_document(_record("b.png"))
    assert store.get_document(with_cat)["category"] == "財產資產"
    assert store.get_document(without)["category"] is None           # 沒給就是 None(舊資料也是)
    assert [d["id"] for d in store.list_documents(category="財產資產")] == [with_cat]


def test_family_can_assign_a_category(store):
    doc_id = store.add_document(_record())
    store.set_document_category(doc_id, "生活契約")
    assert store.get_document(doc_id)["category"] == "生活契約"
    store.set_document_category(doc_id, "未分類")
    with pytest.raises(ValueError):
        store.set_document_category(doc_id, "亂填的類別")
    with pytest.raises(LookupError):
        store.set_document_category(999, "生活契約")


def test_update_document_keeps_category(store):
    rec = _record()
    rec["類別"] = "醫療與保險"
    doc_id = store.add_document(rec)
    store.update_document(doc_id, _record(action="review"))            # 家人更正後:類別不被洗掉
    assert store.get_document(doc_id)["category"] == "醫療與保險"


def test_old_database_gets_category_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # 模擬 10/2 以前的版本:沒有 category 欄位
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                     " source_file TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, target_path TEXT,"
                     " doc_type TEXT, date TEXT, vendor TEXT, amount REAL, currency TEXT, result_json TEXT,"
                     " origin TEXT, error TEXT, doc_type_hint TEXT)")
    store = Store(db)
    rec = _record()
    rec["類別"] = "生活契約"
    assert store.get_document(store.add_document(rec))["category"] == "生活契約"


def test_document_label_is_stored_and_survives_updates(store):
    """家人上傳時選的文件名稱(上傳選項清單,10/3)寫進 doc_label;家人更正或退回後不被洗掉,沒選是 None。"""
    rec = _record(action="review")
    rec["使用者選的文件"] = "保單"
    doc_id = store.add_document(rec)
    assert store.get_document(doc_id)["doc_label"] == "保單"
    store.update_document(doc_id, _record(action="archive"))
    assert store.get_document(doc_id)["doc_label"] == "保單"
    assert store.get_document(store.add_document(_record("b.png")))["doc_label"] is None


def test_old_database_gets_doc_label_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # 模擬 10/3 以前的版本:沒有 doc_label 欄位,已有一份文件
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                     " source_file TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, target_path TEXT,"
                     " doc_type TEXT, date TEXT, vendor TEXT, amount REAL, currency TEXT, result_json TEXT,"
                     " origin TEXT, error TEXT, doc_type_hint TEXT, category TEXT)")
        conn.execute("INSERT INTO documents (created_at, source_file, action, category)"
                     " VALUES ('2026-10-02T10:00:00', 'old.png', 'archive', '生活契約')")
    store = Store(db)
    assert store.get_document(1)["doc_label"] is None                  # 舊文件:沒選過名稱
    rec = _record()
    rec["使用者選的文件"] = "稅單"
    assert store.get_document(store.add_document(rec))["doc_label"] == "稅單"


def test_settings_change_only_when_value_changes_and_are_logged(store):
    assert store.get_settings() == {}
    assert store.set_setting("auto_threshold", "0.85", actor="family@example.com") is True
    assert store.set_setting("auto_threshold", "0.85") is False         # 沒變就不寫、不留紀錄
    assert store.set_setting("auto_threshold", "0.90") is True
    assert store.get_settings() == {"auto_threshold": "0.90"}
    changes = store.list_setting_changes()
    assert [(c["old_value"], c["new_value"]) for c in changes] == [("0.85", "0.90"), (None, "0.85")]
    assert changes[1]["actor"] == "family@example.com" and changes[0]["actor"] is None
    assert len(store.list_setting_changes(limit=1)) == 1
