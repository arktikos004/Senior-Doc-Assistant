"""中斷恢復(FIN-HARDEN 第四輪):排隊、辨識、存檔三個階段重開,文件都追得到、行動不重複,畫面走「還沒讀」與「重新辨識」。

模擬重開:對同一個資料夾再 create_app 一次並跑開機流程(with FormClient(...) as client)。上一個 app 的背景執行緒當作跟著行程死掉:
停在當機的那一步(_freeze),之後什麼都不再做、不補救、還沒 commit 的交易也不會寫進去(daemon 執行緒,測試結束一起收掉)。
在測試自己的執行緒裡當機(直接呼叫更正、改類型、退回)用 SystemExit:程式裡的 except Exception 接不到它,一樣沒有補救。
模型都是假的,不連網,檔案都在 tmp_path,文件都是合成的。
"""
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import src.pipeline as pipeline_module
import web.app as web_app
from conftest import FormClient
from src.duplicates import file_sha256
from src.models import ExtractionResult
from src.pipeline import model_unavailable
from src.providers import MockAnalyzer
from src.record_log import load_records
from src.review import correct_document, reject_document, retype_document
from src.store import Store
from web.app import create_app
from web.render import CONFIRM_REJECT_FIELD

TODAY = date.today()
DUE = TODAY + timedelta(days=10)
UPLOAD_NAME = "20261001-101500-abcd1234.png"
PILL = "處理中</span>"        # 狀態膠囊(文件櫃、首頁與結果頁)


def _reading(doc_type: str = "帳單", confidence: float = 0.95) -> ExtractionResult:
    fields = {"due_date": DUE.isoformat(), "bill_kind": "電費"} if doc_type == "帳單" else {}
    return ExtractionResult(doc_type=doc_type, date=TODAY.isoformat(), vendor="合成電力公司", amount=1286.0,
                            confidence=confidence, plain_summary="這是合成的文件。", fields=fields,
                            raw={"fields": fields})


class BillReader(MockAnalyzer):
    """一律讀成一張帳單(信心高就自動存檔,低就轉人工);記下每次的類型提示與要不要只在本機。"""

    def __init__(self, cfg, confidence: float = 0.95):
        super().__init__(cfg)
        self.confidence, self.calls = confidence, []

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.calls.append((doc_type_hint, local_only))
        return _reading("帳單", self.confidence)


class DownReader(BillReader):
    """模型沒開(ollama 套件把連線失敗包成 ConnectionError)。"""

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        raise ConnectionError("Failed to connect to Ollama.")


def _freeze(*args, **kwargs):
    """背景執行緒停在這裡不再回來:當作行程在這一步死掉了。"""
    threading.Event().wait()


class HeldReader(BillReader):
    """模型讀到一半就重開:卡在模型裡,再也不回來,所以什麼都不寫。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.reading = threading.Semaphore(0)

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.reading.release()
        _freeze()


@pytest.fixture
def held(cfg, monkeypatch):
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)     # 上傳後馬上回進度頁,不等背景
    return HeldReader(cfg)


def _restart(cfg, monkeypatch, reader) -> FormClient:
    """同一個資料夾再開一次(之後的請求照常等背景做完才回應);用 with 才會跑開機流程。"""
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 30.0)
    return FormClient(create_app(cfg, analyzer=reader))


def _upload(client, png_bytes, doc_type="帳單", pad=0, **data):
    return client.post("/upload", files={"file": ("photo.png", png_bytes(pad), "image/png")},
                       data={"doc_type": doc_type, **data}, follow_redirects=False)


def _live(store, doc_id):
    return [a for a in store.list_actions(document_id=doc_id) if not a["superseded"]]


def _files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


def _until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等不到背景工作")


def _assert_can_retry(client, doc):
    """長輩看到的:照片還在、不用重拍,可以按「重新辨識」。"""
    assert doc["action"] == "failed" and model_unavailable(doc["error"])
    html = client.get(f"/doc/{doc['id']}").text
    assert "系統暫時忙碌，這張還沒讀" in html and "照片已經收好了，不用重拍。" in html
    assert 'http-equiv="refresh"' not in html
    assert f'action="/doc/{doc["id"]}/retry"' in html and "重新拍一張" not in html
    assert client.get(f"/doc/{doc['id']}/file").status_code == 200


# ---- 排隊、辨識兩個階段 -----------------------------------------------------------------------

def test_restart_while_reading_and_queued_keeps_every_document(cfg, store, held, png_bytes, monkeypatch):
    """辨識中與排隊中各一份:重開後兩份都在、不自動重跑、不用重拍;重新辨識後更新同一筆,提醒各一筆。"""
    before = FormClient(create_app(cfg, analyzer=held))
    assert _upload(before, png_bytes).status_code == 303
    assert held.reading.acquire(timeout=10)                                 # 第一份正在讀
    assert _upload(before, png_bytes, pad=7).status_code == 303             # 第二份排隊中

    reader = BillReader(cfg)
    with _restart(cfg, monkeypatch, reader) as client:
        docs = store.list_documents()
        assert len(docs) == 2 and reader.calls == [] and store.list_actions() == []
        for doc in docs:
            assert Path(doc["target_path"]).parent == cfg.paths.uploads_path
            _assert_can_retry(client, doc)
        assert "還沒讀" in client.get("/cabinet").text and "還沒讀" in client.get("/").text
        for doc in docs:
            r = client.post(f"/doc/{doc['id']}/retry", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == f"/doc/{doc['id']}"
    assert store.count_documents() == 2 and _files(cfg.paths.uploads_path) == []
    for doc in docs:
        assert store.get_document(doc["id"])["action"] == "archive" and len(_live(store, doc["id"])) == 1


def test_progress_page_after_a_restart_points_to_the_cabinet(cfg, store, held, png_bytes, monkeypatch):
    before = FormClient(create_app(cfg, analyzer=held))
    where = _upload(before, png_bytes).headers["location"]
    assert held.reading.acquire(timeout=10)
    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        page = client.get(where)
        assert page.status_code == 404 and "找不到這次上傳的進度" in page.text and 'href="/cabinet"' in page.text
        assert "還沒讀" in page.text and "重新辨識" in page.text                   # 下一步說得清楚,不叫人重新上傳


def test_upload_choices_survive_the_restart(cfg, store, held, png_bytes, monkeypatch):
    """上傳時選的大類與文件名稱收件時就記下:重新辨識照舊只在本機,不會因為重開變成送雲端。"""
    before = FormClient(create_app(cfg, analyzer=held))
    _upload(before, png_bytes, doc_type="醫療收據", category="醫療與保險")
    assert held.reading.acquire(timeout=10)
    reader = BillReader(cfg)
    with _restart(cfg, monkeypatch, reader) as client:
        [doc] = store.list_documents()
        assert (doc["doc_label"], doc["category"], doc["doc_type_hint"]) == ("醫療收據", "醫療與保險", "收據")
        assert doc["file_sha256"] == file_sha256(Path(doc["target_path"]))
        assert "醫療與保險" in client.get(f"/doc/{doc['id']}").text
        client.post(f"/doc/{doc['id']}/retry", follow_redirects=False)
    assert reader.calls == [("收據", True)]


def test_a_document_being_read_shows_as_processing(cfg, store, held, png_bytes):
    """處理中:文件櫃、首頁與結果頁寫「處理中」;不算讀不出來、導覽數字不跳;不能重新辨識、更正、退回或改類型。"""
    client = FormClient(create_app(cfg, analyzer=held))
    _upload(client, png_bytes)
    assert held.reading.acquire(timeout=10)
    [doc] = store.list_documents()
    assert doc["action"] == "processing" and doc["error"] is None and doc["result"] is None
    assert store.count_documents(action="failed") == 0 and store.count_documents(action="review") == 0
    for url in ("/", "/cabinet"):
        assert PILL in client.get(url).text
    html = client.get(f"/doc/{doc['id']}").text
    assert PILL in html and "/retry" not in html and "照片已經收好了" in html
    # 還在讀不是系統忙碌:照實寫,這一頁自己重新整理,讀好就換成結果(沒有 JS 也一樣)
    assert "正在讀這張" in html and "系統暫時忙碌" not in html and '<meta http-equiv="refresh" content="5">' in html
    assert "這張讀不出來" not in html and "重新拍一張" not in html and "無法辨識" not in html
    assert 'class="nav-badge"' not in html
    assert client.post(f"/doc/{doc['id']}/retry", follow_redirects=False).status_code == 409
    assert client.get(f"/doc/{doc['id']}/correct").status_code == 409
    assert client.get(f"/doc/{doc['id']}/retype").status_code == 409
    assert client.post(f"/doc/{doc['id']}/reject", data={CONFIRM_REJECT_FIELD: "1"}).status_code == 409
    assert store.get_document(doc["id"])["action"] == "processing"


# ---- 存檔階段 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("confidence, folder, action", [(0.95, "archive", "archive"), (0.45, "review", "review")])
def test_restart_between_moving_the_file_and_the_commit(cfg, store, monkeypatch, png_bytes, confidence, folder, action):
    """檔案搬走了、交易還沒 commit:文件照樣是還沒讀,原件依雜湊找回來放回 uploads,待複核的辨識結果檔也不留。"""
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    crashed = threading.Event()

    def crash(self, *args, **kwargs):
        crashed.set()
        _freeze()

    with monkeypatch.context() as patch:
        patch.setattr(Store, "update_document", crash)
        _upload(FormClient(create_app(cfg, analyzer=BillReader(cfg, confidence))), png_bytes)
        assert crashed.wait(10)
    assert _files(getattr(cfg.paths, folder)) and store.list_actions() == []    # 檔案真的搬走了,行動沒有寫進去

    reader = BillReader(cfg, confidence)
    with _restart(cfg, monkeypatch, reader) as client:
        [doc] = store.list_documents()
        assert reader.calls == [] and Path(doc["target_path"]).parent == cfg.paths.uploads_path
        assert _files(cfg.paths.archive) == [] and _files(cfg.paths.review) == []
        _assert_can_retry(client, doc)
        assert client.post(f"/doc/{doc['id']}/retry", follow_redirects=False).status_code == 303
    [doc] = store.list_documents()
    assert doc["action"] == action and len(_live(store, doc["id"])) == 1 and Path(doc["target_path"]).is_file()


def test_restart_right_after_the_commit_keeps_the_finished_document(cfg, store, monkeypatch, png_bytes):
    """交易 commit 了、流水帳還沒寫:文件與提醒都完整,開機不動它;行動不會多一筆,也不能再重新辨識。"""
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    crashed = threading.Event()

    def crash(*args, **kwargs):
        crashed.set()
        _freeze()

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_module, "append_record", crash)
        _upload(FormClient(create_app(cfg, analyzer=BillReader(cfg))), png_bytes)
        assert crashed.wait(10)

    reader = BillReader(cfg)
    with _restart(cfg, monkeypatch, reader) as client:
        [doc] = store.list_documents()
        assert doc["action"] == "archive" and doc["error"] is None and reader.calls == []
        assert len(_live(store, doc["id"])) == 1 and Path(doc["target_path"]).is_file()
        assert client.post(f"/doc/{doc['id']}/retry", follow_redirects=False).status_code == 409


# ---- 重新辨識 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("with_hash", [True, False])
def test_restart_during_the_retry_move_puts_the_original_back(cfg, store, png_bytes, monkeypatch, with_hash):
    """原件剛搬回 uploads、資料庫還記著 failed/(舊版重新辨識的窗口):開機把原件搬回資料庫記的位置,不多建一筆。
    舊文件沒有雜湊的,認上傳時的檔名。"""
    kept = cfg.paths.uploads_path / UPLOAD_NAME
    kept.write_bytes(png_bytes())
    recorded = cfg.paths.failed / UPLOAD_NAME
    doc_id = store.add_document({
        "原始檔案": UPLOAD_NAME, "動作": "failed", "原因": "AI 辨識失敗", "目標路徑": str(recorded),
        "AI辨識結果": None, "錯誤": "ConnectionError: Failed to connect to Ollama.", "使用者提示": "帳單",
        "檔案雜湊": file_sha256(kept) if with_hash else None})
    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        doc = store.get_document(doc_id)
        assert store.count_documents() == 1 and Path(doc["target_path"]) == recorded and recorded.is_file()
        assert not kept.exists()
        _assert_can_retry(client, doc)
        assert client.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 303
    assert store.get_document(doc_id)["action"] == "archive" and len(_live(store, doc_id)) == 1


def test_restart_while_rereading_lets_the_family_retry_again(cfg, store, held, png_bytes, monkeypatch):
    """重新辨識讀到一半重開:一樣回到「還沒讀」,再按一次就好;誰按的只記成功的那一次,紀錄照舊是「讀不出來 → 已存檔」。"""
    first = FormClient(create_app(cfg, analyzer=DownReader(cfg)))
    _upload(first, png_bytes)
    doc_id = _until(lambda: [d["id"] for d in store.list_documents() if d["action"] == "failed"])[0]
    assert Path(store.get_document(doc_id)["target_path"]).parent == cfg.paths.failed

    second = FormClient(create_app(cfg, analyzer=held))
    assert second.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 303
    assert held.reading.acquire(timeout=10)
    doc = store.get_document(doc_id)
    assert doc["action"] == "processing" and Path(doc["target_path"]).parent == cfg.paths.uploads_path
    assert PILL in second.get(f"/doc/{doc_id}").text
    again = second.post(f"/doc/{doc_id}/retry", follow_redirects=False)         # 連按兩下
    assert again.status_code == 409 and "這份文件不用重新辨識" in again.text

    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        _assert_can_retry(client, store.get_document(doc_id))
        assert store.list_audit_events(document_id=doc_id) == []
        assert client.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 303
    assert store.count_documents() == 1 and len(_live(store, doc_id)) == 1
    [event] = store.list_audit_events(document_id=doc_id)
    assert (event["event"], event["detail"]) == ("retry_read", {"from": "failed", "to": "archive"})


def test_reread_counts_deadlines_from_the_upload_day(cfg, store, png_bytes, monkeypatch):
    """重開後隔幾天才重新辨識:「收到後 N 日內」還是從上傳那天算,不會因為中斷而把期限往後推。"""
    original = cfg.paths.failed / UPLOAD_NAME
    original.write_bytes(png_bytes())
    doc_id = store.add_document({"時間": "2026-01-02T08:00:00", "原始檔案": UPLOAD_NAME, "動作": "failed",
                                 "目標路徑": str(original), "錯誤": "ConnectionError: Failed to connect"})
    seen = []
    monkeypatch.setattr(pipeline_module, "plan_actions",
                        lambda result, decision, cfg, received_on: seen.append(received_on) or [])
    with FormClient(create_app(cfg, analyzer=BillReader(cfg))) as client:
        assert client.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 303
    assert seen == [date(2026, 1, 2)]


# ---- 改類型、更正、退回:搬了檔、資料庫還沒寫就重開 ---------------------------------------------------

class TypedReader(MockAnalyzer):
    """第一次讀成收據(自動存檔),之後照家人選的類型讀。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = 0

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.calls += 1
        return _reading("收據" if self.calls == 1 else doc_type_hint)


def test_restart_while_retyping_keeps_the_old_reading(cfg, store, held, png_bytes, monkeypatch):
    """改類型重讀到一半重開:文件與提醒都還是原本的(先讀完才動文件),之後照樣能改。"""
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 30.0)
    reader = TypedReader(cfg)
    doc_id = int(_upload(FormClient(create_app(cfg, analyzer=reader)), png_bytes, "收據").headers["location"]
                 .rsplit("/", 1)[1])
    before = store.get_document(doc_id)
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    busy = FormClient(create_app(cfg, analyzer=held))
    assert busy.post(f"/doc/{doc_id}/retype", data={"doc_type": "帳單"}, follow_redirects=False).status_code == 303
    assert held.reading.acquire(timeout=10)
    with _restart(cfg, monkeypatch, reader) as client:
        assert store.get_document(doc_id) == before and store.list_actions() == []
        assert client.post(f"/doc/{doc_id}/retype", data={"doc_type": "帳單"}, follow_redirects=False).status_code == 303
    doc = store.get_document(doc_id)
    assert doc["doc_type"] == "帳單" and len(_live(store, doc_id)) == 1 and Path(doc["target_path"]).is_file()


def _crash_on(monkeypatch, name):
    def crash(self, *args, **kwargs):
        raise SystemExit
    monkeypatch.setattr(Store, name, crash)


def test_restart_after_the_retype_moved_the_original(cfg, store, png_bytes, monkeypatch):
    """改類型時原件已經改名搬到帳單的資料夾、交易還沒寫完就重開:讀值與提醒都是原本的,原件搬回資料庫記的位置。"""
    reader = TypedReader(cfg)
    client = FormClient(create_app(cfg, analyzer=reader))
    doc_id = int(_upload(client, png_bytes, "收據").headers["location"].rsplit("/", 1)[1])
    before = store.get_document(doc_id)
    original = Path(before["target_path"])
    with monkeypatch.context() as patch:
        _crash_on(patch, "replace_actions")
        with pytest.raises(SystemExit):
            retype_document(cfg, store, reader, doc_id, "帳單", original)
    assert not original.exists() and _files(cfg.paths.archive / "帳單")              # 檔案真的搬走了
    with _restart(cfg, monkeypatch, reader) as client:
        assert store.get_document(doc_id) == before and original.is_file()
        assert _files(cfg.paths.archive / "帳單") == [] and store.list_actions() == []
        assert client.get(f"/doc/{doc_id}/file").status_code == 200
        assert store.list_audit_events(document_id=doc_id) == []


def test_restart_after_a_correction_or_reject_moved_the_original(cfg, store, review_doc, monkeypatch):
    """更正(待複核 → 存檔)或退回(待複核 → 讀不出來)搬了原件、交易沒寫完就重開:文件照舊待複核,原件搬回 review/。"""
    bill = _reading().to_dict()
    for act in ("correct", "reject"):
        doc_id = review_doc(bill, name=f"20261001-10150{len(act)}-abcd1234.png")
        original = Path(store.get_document(doc_id)["target_path"])
        store.set_file_hash(doc_id, file_sha256(original))      # 正式資料都有(收件時算,舊的開機時補)
        store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": DUE.isoformat()})
        before = store.get_document(doc_id)
        with monkeypatch.context() as patch:
            _crash_on(patch, "update_document")
            with pytest.raises(SystemExit):
                if act == "correct":
                    correct_document(cfg, store, doc_id, {"amount": 1300.0}, original)
                else:
                    reject_document(cfg, store, doc_id, original)
        assert not original.exists()
        with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
            assert store.get_document(doc_id) == before and original.is_file()
            assert [a["status"] for a in _live(store, doc_id)] == ["pending"]
            assert client.get(f"/doc/{doc_id}/file").status_code == 200
    assert _files(cfg.paths.archive) == [] and _files(cfg.paths.failed) == []


# ---- 開機恢復的邊界 -----------------------------------------------------------------------------

def test_original_that_cannot_be_found_asks_for_a_new_photo(cfg, store, monkeypatch):
    """原件真的不見了就照實說:不寫「照片已經收好了」,也沒有「重新辨識」。"""
    doc_id = store.add_document({"原始檔案": UPLOAD_NAME, "動作": "processing",
                                 "目標路徑": str(cfg.paths.uploads_path / UPLOAD_NAME)})
    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        doc = store.get_document(doc_id)
        assert doc["action"] == "failed" and doc["error"] and not model_unavailable(doc["error"])
        html = client.get(f"/doc/{doc_id}").text
        assert "這張讀不出來" in html and "重新拍一張" in html and "/retry" not in html


def test_boot_leaves_finished_documents_alone(cfg, store, png_bytes, monkeypatch):
    results = {"doc_type": "收據", "date": TODAY.isoformat(), "amount": 100.0}
    keep = cfg.paths.archive / "收據" / "kept.png"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(png_bytes())
    rows = [
        store.add_document({"原始檔案": "a.png", "動作": "archive", "目標路徑": str(keep), "AI辨識結果": results,
                            "檔案雜湊": file_sha256(keep)}),
        store.add_document({"原始檔案": "b.png", "動作": "review", "目標路徑": str(cfg.paths.review / "gone.png"),
                            "AI辨識結果": results}),
        store.add_document({"原始檔案": "c.png", "動作": "failed", "目標路徑": str(cfg.paths.failed / "gone.png"),
                            "錯誤": "ValueError: 模型回覆不是合法 JSON"}),
    ]
    store.add_action(rows[0], "calendar", "auto", {"title": "繳費期限", "date": DUE.isoformat()})
    before = [store.get_document(i) for i in rows], store.list_actions()
    with _restart(cfg, monkeypatch, BillReader(cfg)):
        pass
    assert ([store.get_document(i) for i in rows], store.list_actions()) == before


def test_untracked_files_are_left_alone(cfg, store, png_bytes, monkeypatch):
    """沒有文件指到的上傳檔(例如家人刪文件時剛好刪不掉的那一份)不會自己變回文件,也不會被刪。"""
    stray = cfg.paths.uploads_path / UPLOAD_NAME
    stray.write_bytes(png_bytes())
    with _restart(cfg, monkeypatch, BillReader(cfg)):
        pass
    assert store.list_documents() == [] and stray.is_file()


def test_boot_skips_jobs_still_running_in_this_process(cfg, store, held, png_bytes):
    """同一個行程再跑一次開機流程(測試或重新載入):還在排隊或辨識的不算中斷。"""
    client = FormClient(create_app(cfg, analyzer=held))
    _upload(client, png_bytes)
    assert held.reading.acquire(timeout=10)
    with client:
        [doc] = store.list_documents()
        assert doc["action"] == "processing"


def test_a_recovery_error_does_not_stop_the_service(cfg, monkeypatch):
    """開機恢復出錯只記 log:服務要開得起來,家人才看得到文件。"""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(web_app, "recover_documents", boom)
    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        assert client.get("/").status_code == 200


def test_old_database_still_boots(cfg, png_bytes, monkeypatch):
    """舊版資料庫(沒有後來補的欄位):照舊補欄位,開機恢復也不出錯;processing 只是 action 的新值,不用改表。"""
    with sqlite3.connect(cfg.paths.db_path) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                     "source_file TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, target_path TEXT, doc_type TEXT, "
                     "date TEXT, vendor TEXT, amount REAL, currency TEXT, result_json TEXT, origin TEXT, error TEXT)")
        conn.execute("INSERT INTO documents (created_at, source_file, action, target_path) VALUES "
                     "('2026-01-02T08:00:00', 'old.png', 'failed', ?)", (str(cfg.paths.failed / "old.png"),))
    (cfg.paths.failed / "old.png").write_bytes(png_bytes())
    with _restart(cfg, monkeypatch, BillReader(cfg)) as client:
        assert client.get("/doc/1").status_code == 200
    assert Store(cfg.paths.db_path).get_document(1)["action"] == "failed"


# ---- 第一次讀完的紀錄 ---------------------------------------------------------------------------

def test_first_read_keeps_one_time_and_the_document_id_in_the_jsonl(cfg, store, png_bytes):
    """收件時先建一筆,讀完更新同一筆:上傳時間不變;records.jsonl 那一行和資料庫同一個時間(匯入舊紀錄靠它認),也帶文件編號。"""
    client = FormClient(create_app(cfg, analyzer=BillReader(cfg)))
    r = _upload(client, png_bytes)
    assert r.status_code == 303
    [doc] = store.list_documents()
    assert r.headers["location"] == f"/doc/{doc['id']}"
    [line] = load_records(cfg.paths.logs)
    assert line["時間"] == doc["created_at"] and line["文件ID"] == doc["id"] and line["動作"] == "archive"
    assert store.import_records(load_records(cfg.paths.logs)) == 0
    assert store.list_audit_events(document_id=doc["id"]) == []                # 第一次讀完不算重新辨識
