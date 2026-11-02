"""刪除單份文件(只有家人):照片、讀值、提醒、更正、操作紀錄與 records.jsonl 那幾行一起刪,不能復原;
誰刪的記在設定變更紀錄(只記編號)。資料都是合成的,放在 tmp_path。"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from samples import BILL
from src.providers import MockAnalyzer
from src.record_log import RECORDS_FILENAME, append_record
from src.store import DOCUMENT_DELETED
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import CONFIRM_DELETE_FIELD


def _seed(cfg, store, review_doc):
    doc_id = review_doc(dict(BILL))
    path = Path(store.get_document(doc_id)["target_path"])
    sidecar = path.with_suffix(path.suffix + ".ai.json")
    sidecar.write_text("{}", encoding="utf-8")
    append_record(cfg.paths.logs, {"原始檔案": "20261001-101500-abcd1234.png", "文件ID": doc_id, "AI辨識結果": {"amount": 1854}})
    append_record(cfg.paths.logs, {"原始檔案": "20261002-101500-ffff0000.png", "文件ID": doc_id + 100})
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15"})
    return doc_id, path, sidecar


def test_family_deletes_a_document_after_confirming(client, cfg, store, review_doc):
    doc_id, path, sidecar = _seed(cfg, store, review_doc)
    asked = client.post(f"/doc/{doc_id}/delete", data={}, follow_redirects=False)
    assert asked.status_code == 400 and "確定要刪除這份文件嗎？" in asked.text     # 沒有確認欄位:先問
    assert store.get_document(doc_id) is not None and path.exists()

    r = client.post(f"/doc/{doc_id}/delete", data={CONFIRM_DELETE_FIELD: "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/cabinet")
    assert store.get_document(doc_id) is None and not path.exists() and not sidecar.exists()
    assert store.list_actions() == []
    lines = (cfg.paths.logs / RECORDS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["文件ID"] for line in lines] == [doc_id + 100]      # 別份文件的紀錄留著
    assert client.get(f"/doc/{doc_id}").status_code == 404
    [change] = [c for c in store.list_setting_changes() if c["key"] == DOCUMENT_DELETED]
    assert (change["old_value"], change["actor"]) == (str(doc_id), "local")
    assert f"刪除一份文件（第 {doc_id} 號）" in client.get("/settings").text


def _as(cfg, store, role):
    email = f"{role}@example.test"
    store.set_member(email, role)
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)   # 已看過資料使用說明

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))
    c.get("/")
    return c


def test_elder_cannot_delete_and_does_not_see_the_button(cfg, store, review_doc):
    doc_id, path, _ = _seed(cfg, store, review_doc)
    elder = _as(cfg, store, "elder")
    assert "刪除這份文件" not in elder.get(f"/doc/{doc_id}").text
    r = elder.post(f"/doc/{doc_id}/delete", data={CSRF_FIELD: elder.cookies[CSRF_COOKIE], CONFIRM_DELETE_FIELD: "1"},
                   follow_redirects=False)
    assert r.status_code == 403 and store.get_document(doc_id) is not None and path.exists()


def test_family_sees_the_delete_button_with_a_confirm_dialog(cfg, store, review_doc):
    doc_id, _, _ = _seed(cfg, store, review_doc)
    html = _as(cfg, store, "family").get(f"/doc/{doc_id}").text
    assert "刪除這份文件" in html and 'data-confirm-title="確定要刪除這份文件嗎？"' in html


def test_deleting_a_missing_document_is_404(client):
    r = client.post("/doc/999/delete", data={CONFIRM_DELETE_FIELD: "1"}, follow_redirects=False)
    assert r.status_code == 404
