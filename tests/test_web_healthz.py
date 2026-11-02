"""/healthz(W2-B)的測試:給開機腳本與監看用。Ollama 檢查一律注入 httpx.MockTransport,不連網。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from src.providers import MockAnalyzer
from web.app import APP_VERSION, create_app


def get_health(cfg, transport: httpx.BaseTransport) -> httpx.Response:
    return TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), ollama_transport=transport)).get("/healthz")


def ollama_up(seen: list, loaded: tuple[str, ...] = ("gemma4:12b",)) -> httpx.MockTransport:
    """假的 Ollama:/api/ps 列出已載入顯示記憶體的模型。"""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"models": [{"name": name, "model": name} for name in loaded]})
    return httpx.MockTransport(handler)


def refused(request):
    raise httpx.ConnectError("connection refused", request=request)


def timed_out(request):
    raise httpx.ReadTimeout("timed out", request=request)


def server_error(request):
    return httpx.Response(500)


def test_mock_mode_answers_200_without_touching_ollama(cfg):
    seen = []
    r = get_health(cfg, ollama_up(seen))
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": APP_VERSION, "provider": "mock", "models": {}, "ollama": "unused",
                        "model_loaded": None, "database": "ok"}
    assert seen == []
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers          # 監看腳本每分鐘來一次,不必發 CSRF cookie


def test_reports_reachable_ollama_and_local_model(cfg):
    cfg.provider = "ollama"
    seen = []
    r = get_health(cfg, ollama_up(seen))
    assert r.json() == {"status": "ok", "version": APP_VERSION, "provider": "ollama",
                        "models": {"local": "gemma4:12b"}, "ollama": "reachable", "model_loaded": True,
                        "database": "ok"}
    assert [str(q.url) for q in seen] == ["http://localhost:11434/api/ps"]
    assert seen[0].extensions["timeout"]["connect"] <= 3      # 短逾時:監看不會被卡住的 Ollama 拖住


@pytest.mark.parametrize("handler", [refused, timed_out, server_error])
def test_still_answers_when_ollama_is_down(cfg, handler):
    cfg.provider = "ollama"
    r = get_health(cfg, httpx.MockTransport(handler))
    assert r.status_code == 200                               # 網頁本身還活著,監看腳本不必重啟它
    assert r.json()["ollama"] == "unreachable" and r.json()["status"] == "degraded"


def test_cloud_mode_lists_both_models_and_never_calls_the_cloud(cfg):
    """雲端模式下藥袋與未指定類型仍在本機辨識,所以兩個模型都列、也檢查 Ollama;不打 Workers AI。"""
    cfg.provider = "workers_ai"
    seen = []
    r = get_health(cfg, ollama_up(seen))
    assert r.json()["models"] == {"cloud": "@cf/google/gemma-4-26b-a4b-it", "local": "gemma4:12b"}
    assert r.json()["ollama"] == "reachable"
    assert {q.url.host for q in seen} == {"localhost"}


def test_never_reveals_paths_hosts_or_keys(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct-must-not-leak")
    monkeypatch.setenv("CF_API_TOKEN", "token-must-not-leak")
    cfg.provider = "workers_ai"
    cfg.ollama.host = "http://gpu-box.internal:11434/"
    seen = []
    r = get_health(cfg, ollama_up(seen))
    assert str(seen[0].url) == "http://gpu-box.internal:11434/api/ps"   # 檢查的是設定的主機
    assert set(r.json()) == {"status", "version", "provider", "models", "ollama", "model_loaded", "database"}
    for secret in (str(tmp_path), "must-not-leak", "gpu-box", "11434", "CF_"):
        assert secret not in r.text, secret


def test_says_whether_the_model_is_already_loaded(cfg):
    """展示前看一眼:model_loaded 是 true,第一張文件就不必等模型載入。沒寫標籤的名稱當作 :latest。"""
    cfg.provider = "ollama"
    assert get_health(cfg, ollama_up([], loaded=())).json()["model_loaded"] is False
    assert get_health(cfg, ollama_up([], loaded=("other:7b",))).json()["model_loaded"] is False
    cfg.ollama.model = "gemma4"
    body = get_health(cfg, ollama_up([], loaded=("gemma4:latest",))).json()
    assert body["model_loaded"] is True and body["status"] == "ok"            # 沒載入不算異常,只是第一張比較慢


def test_unreadable_ollama_reply_still_counts_as_reachable(cfg):
    cfg.provider = "ollama"
    body = get_health(cfg, httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))).json()
    assert (body["ollama"], body["model_loaded"], body["status"]) == ("reachable", None, "ok")


def test_reports_a_database_that_cannot_be_written(cfg, monkeypatch):
    monkeypatch.setattr("src.store.Store.writable", lambda self: False)
    r = get_health(cfg, ollama_up([]))
    assert r.status_code == 200 and r.json()["database"] == "error" and r.json()["status"] == "degraded"


def test_store_writable_check_changes_nothing_and_notices_a_lock(tmp_path):
    import sqlite3

    from src.store import Store
    store = Store(tmp_path / "app.db")
    before = (tmp_path / "app.db").read_bytes()
    assert store.writable() is True and (tmp_path / "app.db").read_bytes() == before
    holder = sqlite3.connect(tmp_path / "app.db")
    holder.execute("BEGIN EXCLUSIVE")                                          # 別人正鎖著寫
    try:
        assert store.writable() is False
    finally:
        holder.rollback()
        holder.close()
