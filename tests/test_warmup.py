"""tools/warmup.py:展示前先把模型載入顯示記憶體。Ollama 一律注入 httpx.MockTransport,不連網。"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import warmup  # noqa: E402


def test_loads_the_configured_model_without_sending_any_document(capsys):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"model": "gemma4:12b", "done": True, "done_reason": "load"})

    assert warmup.main(["--keep-alive", "3h"], transport=httpx.MockTransport(handler)) == 0
    [request] = seen
    assert request.url.path == "/api/generate"
    assert json.loads(request.content) == {"model": "gemma4:12b", "keep_alive": "3h"}   # 沒有提示詞、沒有影像
    assert "已載入 gemma4:12b" in capsys.readouterr().out


def test_without_keep_alive_ollama_decides(capsys):
    seen = []
    warmup.main([], transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})))
    assert json.loads(seen[0].content) == {"model": "gemma4:12b"}       # 照 OLLAMA_KEEP_ALIVE


def test_reports_when_ollama_is_not_running(capsys):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert warmup.main([], transport=httpx.MockTransport(refuse)) == 1
    out = capsys.readouterr().out
    assert "沒有載入 gemma4:12b" in out and "ConnectError" in out
    assert warmup.main([], transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))) == 1
