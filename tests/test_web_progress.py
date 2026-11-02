"""上傳後的背景辨識與進度頁(UX-3):每一步真的做完才亮、一次只讀一張、只有上傳的人看得到進度。

模型用 MockAnalyzer 加上「等測試放行」模擬正在讀;不連網、不呼叫真模型,檔案都在 tmp_path。
"""
import io
import re
import threading
import time

import pytest
from PIL import Image

import src.pipeline as pipeline_module
import web.app as web_app
from conftest import FormClient
from samples import plain_pdf
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import create_app
from web.jobs import JOB_TTL, JobBoard
from web.render import progress_view


class GatedAnalyzer(MockAnalyzer):
    """辨識要等測試放行(模擬模型正在讀);記下同時在讀的最多張數。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.go = threading.Event()
        self.entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.running = self.most = 0

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
        self.entered.release()
        try:
            assert self.go.wait(10), "測試沒有放行"
            return super().analyze(file_path, doc_type_hint, local_only=local_only)
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def gated(cfg, monkeypatch):
    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    reader = GatedAnalyzer(cfg)
    yield reader
    reader.go.set()   # 測試失敗也放行,背景執行緒才會結束


def _png(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _upload(client, content=None, name="photo.png", doc_type="不確定"):
    kind = "application/pdf" if name.endswith(".pdf") else "image/png"
    return client.post("/upload", files={"file": (name, content or _png(), kind)}, data={"doc_type": doc_type},
                       follow_redirects=False)


def _status(client, where):
    r = client.get(where + "/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    return r.json()


def _states(client, where):
    return {s["id"]: s["state"] for s in _status(client, where)["steps"]}


def _until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等不到背景辨識")


def _until_done(client, where):
    return _until(lambda: _status(client, where)["next"])


def test_each_step_lights_up_only_after_the_server_has_done_it(cfg, gated, monkeypatch):
    checking = threading.Event()
    original = pipeline_module.verify_decide_plan

    def held(*args, **kwargs):            # 核對要等測試放行
        assert checking.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "verify_decide_plan", held)
    client = FormClient(create_app(cfg, analyzer=gated))
    r = _upload(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/upload/progress/")
    where = r.headers["location"]

    page = client.get(where)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "正在讀這張文件" in page.text and "展示模式：使用模擬讀值" in page.text
    assert '<noscript><meta http-equiv="refresh" content="3"></noscript>' in page.text   # 沒有 JS 也會更新
    assert f'data-job-status="{where}/status"' in page.text

    assert gated.entered.acquire(timeout=10)                                    # 模型正在讀
    assert _states(client, where) == {"received": "done", "read": "now", "checked": "todo", "decided": "todo"}
    gated.go.set()
    _until(lambda: _states(client, where)["read"] == "done")                    # 讀完了,核對還沒做完
    status = _status(client, where)
    assert {s["id"]: s["state"] for s in status["steps"]} == {
        "received": "done", "read": "done", "checked": "now", "decided": "todo"}
    assert status["say"] == "現在：程式核對" and status["next"] is None
    read = next(s for s in status["steps"] if s["id"] == "read")
    assert re.fullmatch(r"(不到 0\.1|\d+(\.\d)?) 秒", read["time"]) and read["state_text"] == "（做完了）"

    checking.set()
    doc = _until_done(client, where)
    assert re.fullmatch(r"/doc/\d+", doc)
    assert client.get(where, follow_redirects=False).headers["location"] == doc   # 沒有 JS 的重新整理也到結果頁


def test_uploads_are_read_one_at_a_time_in_order(cfg, gated):
    """只有一張 GPU,第二張等第一張讀完;進度頁寫前面還有幾份。"""
    client = FormClient(create_app(cfg, analyzer=gated))
    first = _upload(client).headers["location"]
    assert gated.entered.acquire(timeout=10)
    second = _upload(client).headers["location"]
    status = _status(client, second)
    read = next(s for s in status["steps"] if s["id"] == "read")
    assert (read["state"], read["hint"]) == ("now", "前面還有 1 份，輪到這份就開始")
    assert status["say"] == "排隊中，前面還有 1 份"
    assert "前面還有 1 份" in client.get(second).text
    assert not gated.entered.acquire(timeout=0.3)                               # 第二張還沒送進模型

    gated.go.set()
    a, b = _until_done(client, first), _until_done(client, second)
    assert gated.most == 1
    assert int(a.rsplit("/", 1)[1]) < int(b.rsplit("/", 1)[1])                  # 照上傳順序


def test_quick_results_skip_the_progress_page(client):
    """模擬讀值一下就做完:直接到結果頁(展示模式與既有流程不變)。"""
    r = _upload(client)
    assert r.status_code == 303 and re.fullmatch(r"/doc/\d+", r.headers["location"])


class ByHeader:
    """每個請求看 x-test-user 決定是誰(同一個 app 兩個家人)。"""

    def resolve(self, request):
        return Identity(request.headers.get("x-test-user"), False)


def test_only_the_uploader_can_see_the_progress(cfg, store, gated):
    for email in ("a@example.test", "b@example.test"):
        store.set_member(email, "family")
        store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)
    app = create_app(cfg, analyzer=gated, identity_resolver=ByHeader())
    a = FormClient(app, headers={"x-test-user": "a@example.test"})
    b = FormClient(app, headers={"x-test-user": "b@example.test"})
    where = _upload(a).headers["location"]
    assert a.get(where).status_code == 200 and a.get(where + "/photo").status_code == 200
    for path in (where, where + "/status", where + "/photo"):
        assert b.get(path, follow_redirects=False).status_code == 404
    assert "找不到這次上傳的進度" in b.get(where).text
    gated.go.set()
    _until_done(a, where)


@pytest.mark.parametrize("token", ["x" * 32, "short"])
def test_unknown_progress_says_where_to_look(client, token):
    r = client.get(f"/upload/progress/{token}")
    assert r.status_code == 404 and "找不到這次上傳的進度" in r.text and 'href="/cabinet"' in r.text
    assert client.get(f"/upload/progress/{token}/status").status_code == 404


def test_progress_shows_a_small_copy_of_the_photo_until_done(cfg, gated):
    client = FormClient(create_app(cfg, analyzer=gated))
    where = _upload(client, _png((2000, 1500)), doc_type="帳單").headers["location"]
    assert "剛上傳的照片：帳單" in client.get(where).text
    photo = client.get(where + "/photo")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "private, no-store"
    with Image.open(io.BytesIO(photo.content)) as im:
        assert im.format == "JPEG" and im.width <= 240 and im.height <= 320
    gated.go.set()
    _until_done(client, where)
    assert client.get(where + "/photo").status_code == 404                     # 做完就不留縮圖


def test_pdf_progress_has_no_photo(cfg, gated):
    client = FormClient(create_app(cfg, analyzer=gated))
    where = _upload(client, plain_pdf(), name="bill.pdf").headers["location"]
    page = client.get(where).text
    assert "正在讀這份文件" in page and "收到檔案" in page and "剛上傳的 PDF 檔" in page
    assert f'src="{where}/photo"' not in page and client.get(where + "/photo").status_code == 404
    gated.go.set()
    _until_done(client, where)


def test_a_crash_while_processing_shows_an_error_page(cfg, monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("C:\\secret\\path disk full")

    monkeypatch.setattr(web_app.Pipeline, "process_file", boom)
    client = FormClient(create_app(cfg, analyzer=MockAnalyzer(cfg)))
    r = _upload(client)                                                         # 很快就失敗:直接回錯誤頁
    assert r.status_code == 500 and "這份文件處理到一半出了問題" in r.text and "secret" not in r.text

    monkeypatch.setattr(web_app, "QUICK_RESULT_SECONDS", 0)
    where = _upload(client).headers["location"]
    if where.startswith("/upload/progress/"):                                   # 已經轉到進度頁:之後問也是錯誤頁
        _until(lambda: client.get(where + "/status").status_code == 500)
        page = client.get(where)
        assert page.status_code == 500 and "這份文件處理到一半出了問題" in page.text


def test_job_board_times_each_step_and_forgets_old_jobs():
    now = [100.0]
    board = JobBoard(clock=lambda: now[0])
    release = threading.Event()

    def work(report):
        now[0] += 2.5
        report("read")
        now[0] += 0.5
        report("checked")
        assert release.wait(5)
        now[0] += 0.25
        return 7

    job = board.submit(work, owner="me", provider="mock", is_pdf=False, label="", photo=b"jpg", received=0.3)
    _until(lambda: "checked" in board.snapshot(job)["seconds"])
    assert board.snapshot(job)["doc_id"] is None
    release.set()
    assert board.wait(job, 5)
    snap = board.snapshot(job)
    assert snap["seconds"] == {"received": 0.3, "read": 2.5, "checked": 0.5, "decided": 0.25}
    assert snap["doc_id"] == 7 and job.photo is None
    assert board.get(job.token, "someone else") is None and board.get(job.token, "me") is job
    now[0] += JOB_TTL + 1
    assert board.get(job.token, "me") is None


def test_progress_view_wording():
    base = {"seconds": {"received": 0.31}, "queued": False, "ahead": 0, "elapsed": 1.5}
    v = progress_view(base, token="t", provider="ollama", is_pdf=False, label="", has_photo=True)
    received, read = v["steps"][0], v["steps"][1]
    assert (received["state"], received["time"]) == ("done", "0.3 秒")
    assert (read["state"], read["time"], read["hint"]) == ("now", "", "Gemma 4 在這台電腦讀字，只照抄、不換算")
    assert v["photo_text"] == "剛上傳的照片" and v["say"] == "現在：AI 照抄文字"
    slow = progress_view(dict(base, elapsed=12.7), token="t", provider="ollama", is_pdf=False, label="", has_photo=True)
    assert slow["steps"][1]["time"] == "已 12 秒"                                # 模型剛載入比較久:看得到還在讀
    done = progress_view(dict(base, seconds={"received": 0.3, "read": 12.04}), token="t", provider="workers_ai",
                         is_pdf=False, label="", has_photo=False)
    assert done["steps"][1]["time"] == "12 秒" and done["steps"][1]["hint"] == "Gemma 4 讀字，只照抄、不換算"
    instant = progress_view(dict(base, seconds={"received": 0.04}), token="t", provider="mock", is_pdf=True,
                            label="帳單", has_photo=False)
    assert instant["steps"][0]["time"] == "不到 0.1 秒" and instant["photo_text"] == "剛上傳的 PDF 檔：帳單"
