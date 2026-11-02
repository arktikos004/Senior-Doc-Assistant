"""模型沒回應(服務沒開、逾時)時不叫長輩重拍:結果頁寫「系統暫時忙碌」,按「重新辨識」更新同一份文件。

照片本身讀不出來的仍是「重拍」。模型用 MockAnalyzer 模擬「連不上 → 恢復」;不連網,檔案都在 tmp_path。
"""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import FormClient
from src.pipeline import model_unavailable
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app


class FlakyAnalyzer(MockAnalyzer):
    """down 時像 Ollama 沒開(ollama 套件把連線失敗包成 ConnectionError);恢復後照 MockAnalyzer 讀。"""

    def __init__(self, cfg, error=None):
        super().__init__(cfg)
        self.error = error or ConnectionError("Failed to connect to Ollama. Please check that Ollama is running.")
        self.down = True

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        if self.down:
            raise self.error
        return super().analyze(file_path, doc_type_hint, local_only=local_only)


@pytest.fixture
def flaky(cfg):
    return FlakyAnalyzer(cfg)


def _upload(client, png_bytes, doc_type="帳單"):
    r = client.post("/upload", files={"file": ("photo.png", png_bytes(), "image/png")}, data={"doc_type": doc_type},
                    follow_redirects=False)
    assert r.status_code == 303
    return int(re.fullmatch(r"/doc/(\d+)", r.headers["location"]).group(1))


@pytest.mark.parametrize("error, busy", [
    ("ConnectionError: Failed to connect to Ollama.", True),
    ("ReadTimeout: timed out", True),
    ("ResponseError: model runner has unexpectedly stopped", True),
    ("WorkersAIError: Workers AI 請求失敗(HTTP 503)", True),
    ("UnidentifiedImageError: cannot identify image file", False),
    ("ValueError: 模型回覆不是合法 JSON", False),
    (None, False),
])
def test_only_model_side_failures_count_as_busy(error, busy):
    assert model_unavailable(error) is busy


def test_busy_failure_says_the_photo_is_kept_and_offers_a_retry(cfg, store, flaky, png_bytes):
    client = FormClient(create_app(cfg, analyzer=flaky))
    doc_id = _upload(client, png_bytes)
    html = client.get(f"/doc/{doc_id}").text
    assert "系統暫時忙碌，這張還沒讀" in html and "照片已經收好了，不用重拍。" in html
    assert "<h1>還沒讀的文件</h1>" in html and "無法辨識" not in html
    assert f'action="/doc/{doc_id}/retry"' in html and "重新拍一張" not in html
    assert "還沒讀" in client.get("/cabinet").text or "還沒讀" in client.get("/").text

    flaky.down = False
    before = store.count_documents()
    r = client.post(f"/doc/{doc_id}/retry", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/doc/{doc_id}"     # 同一份文件,編號不變
    doc = store.get_document(doc_id)
    assert doc["action"] in ("archive", "review") and doc["error"] is None and doc["doc_type_hint"] == "帳單"
    assert store.count_documents() == before
    assert not any(cfg.paths.failed.iterdir())                                     # 原件從 failed/ 搬走了
    [event] = store.list_audit_events(document_id=doc_id)
    assert (event["event"], event["actor"]) == ("retry_read", "local")
    assert "重新辨識" in client.get(f"/doc/{doc_id}").text                          # 操作紀錄看得到

    again = client.post(f"/doc/{doc_id}/retry", follow_redirects=False)            # 已經讀好了:不再做
    assert again.status_code == 409 and "這份文件不用重新辨識" in again.text


def test_retry_that_fails_again_stays_busy(cfg, store, flaky, png_bytes):
    client = FormClient(create_app(cfg, analyzer=flaky))
    doc_id = _upload(client, png_bytes)
    r = client.post(f"/doc/{doc_id}/retry", follow_redirects=False)
    assert r.headers["location"] == f"/doc/{doc_id}"
    assert store.get_document(doc_id)["action"] == "failed" and "系統暫時忙碌" in client.get(f"/doc/{doc_id}").text
    assert len(list(cfg.paths.failed.iterdir())) == 1                               # 原件還在,可以再試


def test_unreadable_photo_still_asks_for_a_new_photo(cfg, store, png_bytes):
    reader = FlakyAnalyzer(cfg, error=ValueError("模型回覆不是合法 JSON"))
    client = FormClient(create_app(cfg, analyzer=reader))
    doc_id = _upload(client, png_bytes)
    html = client.get(f"/doc/{doc_id}").text
    assert "這張讀不出來" in html and "重新拍一張" in html and "/retry" not in html
    assert client.post(f"/doc/{doc_id}/retry", follow_redirects=False).status_code == 409
    assert store.get_document(doc_id)["action"] == "failed"


def test_viewers_cannot_retry(cfg, store, flaky, png_bytes):
    doc_id = _upload(FormClient(create_app(cfg, analyzer=flaky)), png_bytes)

    class Stranger:
        def resolve(self, request):
            return Identity("stranger@example.test", False)

    viewer = TestClient(create_app(cfg, analyzer=flaky, identity_resolver=Stranger()))
    viewer.get("/")
    html = viewer.get(f"/doc/{doc_id}").text
    assert "系統暫時忙碌" in html and "/retry" not in html
    r = viewer.post(f"/doc/{doc_id}/retry", data={CSRF_FIELD: viewer.cookies[CSRF_COOKIE]}, follow_redirects=False)
    assert r.status_code == 403 and store.get_document(doc_id)["action"] == "failed"


def test_retry_keeps_the_upload_choices(cfg, store, flaky, png_bytes):
    """重新辨識照上傳時選的大類與文件名稱(敏感大類一樣只在本機)。"""
    client = FormClient(create_app(cfg, analyzer=flaky))
    r = client.post("/upload", files={"file": ("p.png", png_bytes(), "image/png")},
                    data={"category": "醫療與保險", "doc_type": "醫療收據"}, follow_redirects=False)
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    seen = []
    flaky.down = False
    original = flaky.analyze

    def spy(file_path, doc_type_hint=None, *, local_only=False):
        seen.append((doc_type_hint, local_only))
        return original(file_path, doc_type_hint, local_only=local_only)

    flaky.analyze = spy
    client.post(f"/doc/{doc_id}/retry", follow_redirects=False)
    doc = store.get_document(doc_id)
    assert seen == [("收據", True)] and doc["doc_label"] == "醫療收據" and doc["category"] == "醫療與保險"
