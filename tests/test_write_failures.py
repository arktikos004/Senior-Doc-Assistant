"""寫入一致性(FIN-HARDEN 第四輪):搬檔、資料庫、records.jsonl 任何一步失敗,都不會文件有了、提醒漏了,也不會多一筆。

故障注入:monkeypatch 讓某一步丟例外(資料庫忙碌用 sqlite3.OperationalError,寫檔失敗用 OSError)。資料庫的寫入在同一個交易:
失敗就整個撤回,剛搬好的原件搬回原位;records.jsonl 是衍生紀錄,交易成功後才寫,寫不進去只記 log。
模型都是假的,不連網,檔案都在 tmp_path,文件都是合成的。
"""
import logging
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import src.pipeline as pipeline_module
import src.store as store_module
import web.app as web_app
from conftest import FormClient
from src.duplicates import file_sha256
from src.models import ExtractionResult
from src.pipeline import Pipeline, model_unavailable
from src.providers import MockAnalyzer
from src.record_log import load_records
from src.review import correct_document, reject_document, retype_document
from src.store import Store
from web.app import create_app
from web.render import CONFIRM_DELETE_FIELD

TODAY = date.today()
DUE = TODAY + timedelta(days=10)


def _reading(doc_type: str = "帳單", confidence: float = 0.95) -> ExtractionResult:
    fields = {"due_date": DUE.isoformat(), "bill_kind": "電費"} if doc_type == "帳單" else {}
    return ExtractionResult(doc_type=doc_type, date=TODAY.isoformat(), vendor="合成電力公司", amount=1286.0,
                            confidence=confidence, plain_summary="這是合成的文件。", fields=fields,
                            raw={"fields": fields})


class BillReader(MockAnalyzer):
    def __init__(self, cfg, doc_type="帳單"):
        super().__init__(cfg)
        self.doc_type = doc_type

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        return _reading(doc_type_hint or self.doc_type)


def _busy(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


def _busy_once(original):
    """第一次呼叫時資料庫忙碌,之後照常(一下子被鎖住,不是整個壞掉)。"""
    calls = []

    def wrapper(*args, **kwargs):
        if not calls:
            calls.append(None)
            _busy()
        return original(*args, **kwargs)
    return wrapper


def _disk_full(*args, **kwargs):
    raise OSError(28, "No space left on device")


def _upload(client, png_bytes, doc_type="帳單"):
    return client.post("/upload", files={"file": ("photo.png", png_bytes(), "image/png")}, data={"doc_type": doc_type},
                       follow_redirects=False)


def _live(store, doc_id):
    return [a for a in store.list_actions(document_id=doc_id) if not a["superseded"]]


def _files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


def _until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("等不到背景工作")


# ---- 交易本身 -----------------------------------------------------------------------------------

def _bill_record(name="a.png"):
    return {"原始檔案": name, "動作": "archive", "目標路徑": None, "AI辨識結果": _reading().to_dict()}


def test_transaction_writes_everything_or_nothing(store):
    with pytest.raises(sqlite3.OperationalError):
        with store.transaction():
            doc_id = store.add_document(_bill_record())
            store.add_action(doc_id, "calendar", "auto", {"title": "繳電費"})
            _busy()
    assert store.list_documents() == [] and store.list_actions() == []
    with store.transaction():
        doc_id = store.add_document(_bill_record())
        store.add_action(doc_id, "calendar", "auto", {"title": "繳電費"})
    assert [d["id"] for d in store.list_documents()] == [doc_id] and len(store.list_actions()) == 1


def test_transaction_is_undone_when_the_service_dies(store):
    """服務被關掉(BaseException,例如 KeyboardInterrupt、SystemExit)也一樣撤回。"""
    with pytest.raises(SystemExit):
        with store.transaction():
            store.add_document(_bill_record())
            raise SystemExit
    assert store.list_documents() == []


def test_transaction_belongs_to_one_thread(store):
    """交易只在呼叫的執行緒:別的執行緒照常讀寫,看不到還沒 commit 的那一筆;巢狀的併進外層。"""
    seen = []
    with store.transaction():
        store.add_document(_bill_record("mine.png"))
        with store.transaction():
            store.add_document(_bill_record("nested.png"))
        worker = threading.Thread(target=lambda: seen.append(store.list_documents()))
        worker.start()
        worker.join(10)
    assert seen == [[]] and len(store.list_documents()) == 2


def test_conditional_update_only_touches_a_document_in_that_state(store):
    doc_id = store.add_document({"原始檔案": "a.png", "動作": "failed", "錯誤": "ConnectionError: down"})
    with pytest.raises(LookupError):
        store.update_document(doc_id, _bill_record(), expect="processing")
    assert store.get_document(doc_id)["action"] == "failed"


# ---- 網頁上傳:收件 → 背景辨識 → 存檔 ---------------------------------------------------------------

@pytest.fixture
def bills(cfg):
    return BillReader(cfg)


def test_jsonl_failure_does_not_fail_the_document(cfg, store, bills, png_bytes, monkeypatch, caplog):
    """records.jsonl 寫不進去(磁碟滿、檔案被開著):文件與提醒照樣存好、照樣打開結果頁,只記一行 log(不寫檔名)。"""
    monkeypatch.setattr(pipeline_module, "append_record", _disk_full)
    with caplog.at_level(logging.WARNING):
        r = _upload(FormClient(create_app(cfg, analyzer=bills)), png_bytes)
    [doc] = store.list_documents()
    assert r.headers["location"] == f"/doc/{doc['id']}" and doc["action"] == "archive"
    assert len(_live(store, doc["id"])) == 1 and load_records(cfg.paths.logs) == []
    assert f"文件 {doc['id']}" in caplog.text and doc["source_file"] not in caplog.text


def test_upload_that_cannot_be_recorded_is_not_kept(cfg, store, bills, png_bytes, monkeypatch):
    """收件時資料庫就寫不進去:照實說沒存成、請重新上傳;收下的檔不留(重新上傳不會多一份沒有紀錄的檔)。"""
    client = FormClient(create_app(cfg, analyzer=bills))
    with monkeypatch.context() as patch:
        patch.setattr(Store, "add_document", _busy)
        r = _upload(client, png_bytes)
    assert r.status_code == 500 and "這份沒有存成文件" in r.text and "再上傳一次" in r.text
    assert store.list_documents() == [] and all(_files(folder) == [] for folder in (
        cfg.paths.uploads_path, cfg.paths.archive, cfg.paths.review, cfg.paths.failed))   # 沒有沒紀錄的複本
    assert _upload(client, png_bytes).status_code == 303
    assert store.count_documents() == 1


@pytest.mark.parametrize("target, name", [
    (Store, "update_document"), (Store, "set_document_category"), (Store, "replace_actions"),
    (store_module, "_insert_action"),
])
def test_save_failure_leaves_no_document_without_its_reminder(cfg, store, bills, png_bytes, monkeypatch, target, name):
    """讀完寫進資料庫的任何一步失敗:整個撤回,不會文件存好了、提醒卻沒寫;原件搬回 uploads,文件改成「還沒讀」。
    錯誤頁連回這份文件、不叫人重新上傳(會多一份);之後重新辨識照常,提醒只有一筆。"""
    client = FormClient(create_app(cfg, analyzer=bills))
    with monkeypatch.context() as patch:
        patch.setattr(target, name, _busy_once(getattr(target, name)))
        r = _upload(client, png_bytes)
    [doc] = store.list_documents()
    assert r.status_code == 500 and "這份文件處理到一半出了問題" in r.text
    assert f'href="/doc/{doc["id"]}"' in r.text and "不用重新上傳" in r.text
    assert doc["action"] == "failed" and model_unavailable(doc["error"]) and store.list_actions() == []
    assert Path(doc["target_path"]).parent == cfg.paths.uploads_path and Path(doc["target_path"]).is_file()
    assert _files(cfg.paths.archive) == [] and _files(cfg.paths.review) == []
    assert load_records(cfg.paths.logs) == []

    assert client.post(f"/doc/{doc['id']}/retry", follow_redirects=False).status_code == 303
    assert store.get_document(doc["id"])["action"] == "archive" and len(_live(store, doc["id"])) == 1
    assert store.count_documents() == 1 and len(load_records(cfg.paths.logs)) == 1


def test_database_down_while_saving_is_fixed_at_the_next_boot(cfg, store, bills, png_bytes, monkeypatch):
    """資料庫一直寫不進去(連「改成還沒讀」也寫不了):文件留在處理中,下次開機恢復改成「還沒讀」。"""
    client = FormClient(create_app(cfg, analyzer=bills))
    with monkeypatch.context() as patch:
        patch.setattr(Store, "update_document", _busy)
        assert _upload(client, png_bytes).status_code == 500
    [doc] = store.list_documents()
    assert doc["action"] == "processing" and Path(doc["target_path"]).is_file()
    with FormClient(create_app(cfg, analyzer=bills)):
        pass
    assert model_unavailable(store.get_document(doc["id"])["error"])


def test_retry_that_cannot_be_recorded_puts_the_original_back(cfg, store, png_bytes, monkeypatch):
    original = cfg.paths.failed / "20261001-101500-abcd1234.png"
    original.write_bytes(png_bytes())
    doc_id = store.add_document({"原始檔案": original.name, "動作": "failed", "目標路徑": str(original),
                                 "錯誤": "ConnectionError: Failed to connect to Ollama."})
    client = FormClient(create_app(cfg, analyzer=BillReader(cfg)))
    with monkeypatch.context() as patch:
        patch.setattr(Store, "update_document", _busy)
        r = client.post(f"/doc/{doc_id}/retry", follow_redirects=False)
    assert r.status_code == 503 and "這次沒有開始重新辨識" in r.text and "照片還在" in r.text
    doc = store.get_document(doc_id)
    assert doc["action"] == "failed" and Path(doc["target_path"]) == original and original.is_file()
    assert _files(cfg.paths.uploads_path) == []
    assert client.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 303
    assert store.get_document(doc_id)["action"] == "archive" and len(_live(store, doc_id)) == 1


def test_document_deleted_while_being_read_does_not_come_back(cfg, store, png_bytes, monkeypatch):
    """讀的時候家人刪掉了這份:讀完不寫回、不留檔,重開也不會冒出來。"""
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    go, entered = threading.Event(), threading.Event()

    class Gated(BillReader):
        def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
            entered.set()
            assert go.wait(10)
            return super().analyze(file_path, doc_type_hint, local_only=local_only)

    client = FormClient(create_app(cfg, analyzer=Gated(cfg)))
    where = _upload(client, png_bytes).headers["location"]
    assert entered.wait(10)
    [doc] = store.list_documents()
    assert client.post(f"/doc/{doc['id']}/delete", data={CONFIRM_DELETE_FIELD: "1"},
                       follow_redirects=False).status_code == 303
    go.set()
    _until(lambda: client.get(where + "/status").status_code == 500)            # 背景做完了:沒有寫回
    assert store.list_documents() == [] and store.list_actions() == []
    assert all(_files(folder) == [] for folder in (cfg.paths.uploads_path, cfg.paths.archive, cfg.paths.review,
                                                    cfg.paths.failed))
    with FormClient(create_app(cfg, analyzer=BillReader(cfg))):
        pass
    assert store.list_documents() == []


# ---- 命令列、資料夾監控(沒有先收件的舊入口) ------------------------------------------------------

@pytest.mark.parametrize("name", ["add_document", "add_action"])
def test_cli_save_failure_keeps_the_file_in_the_inbox(cfg, store, monkeypatch, png_bytes, name):
    """舊入口一次寫文件與行動:任何一步失敗就整個撤回,原件留在收件匣,再處理一次只會有一筆。"""
    path = cfg.paths.inbox / "bill.png"
    path.write_bytes(png_bytes())
    pipeline = Pipeline(cfg, BillReader(cfg), store)
    with monkeypatch.context() as patch:
        patch.setattr(Store, name, _busy)
        with pytest.raises(sqlite3.OperationalError):
            pipeline.process_file(path, "帳單")
    assert store.list_documents() == [] and store.list_actions() == [] and path.is_file()
    assert _files(cfg.paths.archive) == [] and load_records(cfg.paths.logs) == []
    record = pipeline.process_file(path, "帳單")
    assert store.count_documents() == 1 and len(_live(store, record["文件ID"])) == 1
    assert [line["文件ID"] for line in load_records(cfg.paths.logs)] == [record["文件ID"]]


# ---- 家人更正、改類型、退回(src/review.py) ---------------------------------------------------------

@pytest.fixture
def waiting_bill(cfg, store, review_doc):
    """一份待複核的帳單,原件在 review/,有一個等家人確認的期限提醒。"""
    doc_id = review_doc(_reading(confidence=0.5).to_dict())
    original = Path(store.get_document(doc_id)["target_path"])
    store.set_file_hash(doc_id, file_sha256(original))
    store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": DUE.isoformat()})
    return doc_id, original


def _unchanged(store, doc_id, before, original):
    assert store.get_document(doc_id) == before and original.is_file()
    assert [(a["status"], a["superseded"]) for a in store.list_actions(document_id=doc_id)] == [("pending", False)]


@pytest.mark.parametrize("name", ["update_document", "replace_actions", "add_correction"])
def test_correction_that_cannot_be_saved_changes_nothing(cfg, store, waiting_bill, monkeypatch, name):
    doc_id, original = waiting_bill
    before = store.get_document(doc_id)
    with monkeypatch.context() as patch:
        patch.setattr(Store, name, _busy)
        with pytest.raises(sqlite3.OperationalError):
            correct_document(cfg, store, doc_id, {"amount": 1300.0}, original)
    _unchanged(store, doc_id, before, original)
    assert store.list_corrections(verified_only=False) == [] and _files(cfg.paths.archive) == []
    assert correct_document(cfg, store, doc_id, {"amount": 1300.0}, original).action == "archive"
    assert len(_live(store, doc_id)) == 1 and len(store.list_corrections(verified_only=False)) == 1


@pytest.mark.parametrize("name", ["update_document", "set_document_category", "replace_actions", "add_audit_event"])
def test_retype_that_cannot_be_saved_changes_nothing(cfg, store, waiting_bill, monkeypatch, name):
    doc_id, original = waiting_bill
    before = store.get_document(doc_id)
    reader = BillReader(cfg, "收據")
    with monkeypatch.context() as patch:
        patch.setattr(Store, name, _busy)
        with pytest.raises(sqlite3.OperationalError):
            retype_document(cfg, store, reader, doc_id, "收據", original)
    _unchanged(store, doc_id, before, original)
    assert store.list_audit_events(document_id=doc_id) == [] and _files(cfg.paths.archive) == []
    assert retype_document(cfg, store, reader, doc_id, "收據", original) is not None
    assert store.get_document(doc_id)["doc_type"] == "收據" and _live(store, doc_id) == []


@pytest.mark.parametrize("name", ["update_document", "replace_actions"])
def test_reject_that_cannot_be_saved_changes_nothing(cfg, store, waiting_bill, monkeypatch, name):
    doc_id, original = waiting_bill
    before = store.get_document(doc_id)
    with monkeypatch.context() as patch:
        patch.setattr(Store, name, _busy)
        with pytest.raises(sqlite3.OperationalError):
            reject_document(cfg, store, doc_id, original)
    _unchanged(store, doc_id, before, original)
    assert _files(cfg.paths.failed) == [] and store.list_audit_events(document_id=doc_id) == []
    reject_document(cfg, store, doc_id, original)
    assert store.get_document(doc_id)["action"] == "failed" and _live(store, doc_id) == []


def test_correction_still_counts_when_the_jsonl_cannot_be_written(cfg, store, waiting_bill, monkeypatch, caplog):
    doc_id, original = waiting_bill
    monkeypatch.setattr(pipeline_module, "append_record", _disk_full)
    with caplog.at_level(logging.WARNING):
        assert correct_document(cfg, store, doc_id, {"amount": 1300.0}, original).action == "archive"
    assert store.get_document(doc_id)["amount"] == 1300.0 and len(_live(store, doc_id)) == 1
    assert f"文件 {doc_id}" in caplog.text
