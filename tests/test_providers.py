"""provider 測試:Workers AI(MockTransport)、Ollama(假 client)、隱私分流。

不呼叫任何真實模型、不連網路:HTTP 一律走 httpx.MockTransport。
"""
import base64
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import src.providers as providers
from src.config import AppConfig, WorkersAIConfig
from src.models import ExtractionResult
from src.preprocess import prepare_image
from src.prompts import get_prompt
from src.providers import RoutingAnalyzer, create_analyzer
from src.providers.ollama import OllamaAnalyzer
from src.providers.workers_ai import WorkersAIAnalyzer, WorkersAIConfigError, WorkersAIError

MODEL = "@cf/google/gemma-4-26b-a4b-it"
FAKE_ACCOUNT = "acct-0000"
FAKE_TOKEN = "tok-SYNTHETIC-SECRET"

GOOD_REPLY = {
    "doc_type": "發票",
    "date": "2026-08-03",
    "vendor": "測試商店",
    "amount": 1854,
    "currency": "NTD",
    "invoice_number": "AB12345678",
    "confidence": 0.93,
    "notes": "",
}


@pytest.fixture
def image_file(tmp_path) -> Path:
    path = tmp_path / "合成發票.png"
    Image.new("RGB", (200, 120), "white").save(path)
    return path


@pytest.fixture
def cf_env(monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", FAKE_ACCOUNT)
    monkeypatch.setenv("CF_API_TOKEN", FAKE_TOKEN)


def _chat_completion(content) -> dict:
    """Workers AI REST 回應:外層 Cloudflare 信封,result 內為 chat.completion 格式。"""
    return {
        "result": {
            "id": "id-1",
            "object": "chat.completion",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "refusal": None},
                    "finish_reason": "stop",
                }
            ],
        },
        "success": True,
        "errors": [],
        "messages": [],
    }


def _workers(cfg: AppConfig, handler) -> tuple[WorkersAIAnalyzer, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    client = httpx.Client(transport=httpx.MockTransport(record))
    return WorkersAIAnalyzer(cfg, client=client), seen


def _user_text(request: httpx.Request) -> str:
    body = json.loads(request.content)
    parts = body["messages"][0]["content"]
    return next(p["text"] for p in parts if p["type"] == "text")


# --- Workers AI ---


def test_workers_ai_request_and_result(cf_env, image_file):
    cfg = AppConfig()
    analyzer, seen = _workers(
        cfg, lambda req, n: httpx.Response(200, json=_chat_completion(json.dumps(GOOD_REPLY)))
    )

    result = analyzer.analyze(image_file, doc_type_hint="發票")

    assert isinstance(result, ExtractionResult)
    assert result.doc_type == "發票"
    assert result.date == "2026-08-03"
    assert result.amount == 1854.0
    assert result.invoice_number == "AB12345678"
    assert result.source_model == f"workers_ai:{MODEL}"

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.host == "api.cloudflare.com"
    assert req.url.path == f"/client/v4/accounts/{FAKE_ACCOUNT}/ai/run/{MODEL}"
    assert req.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert req.extensions["timeout"]["read"] == cfg.workers_ai.timeout

    body = json.loads(req.content)
    prompt, schema = get_prompt("發票")
    parts = body["messages"][0]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    prepared = prepare_image(image_file)
    assert image_part["image_url"]["url"] == (
        f"data:{prepared.mime_type};base64," + base64.b64encode(prepared.data).decode("ascii")
    )
    assert _user_text(req) == prompt
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["temperature"] == 0
    assert body["stream"] is False


def test_workers_ai_uses_configured_model(cf_env, image_file):
    cfg = AppConfig(workers_ai=WorkersAIConfig(model="@cf/google/other-model"))
    analyzer, seen = _workers(
        cfg, lambda req, n: httpx.Response(200, json=_chat_completion(json.dumps(GOOD_REPLY)))
    )

    result = analyzer.analyze(image_file, doc_type_hint="收據")

    assert seen[0].url.path.endswith("/ai/run/@cf/google/other-model")
    assert result.source_model == "workers_ai:@cf/google/other-model"


def test_workers_ai_accepts_fenced_json_and_legacy_response_shape(cf_env, image_file):
    fenced = "```json\n" + json.dumps(GOOD_REPLY, ensure_ascii=False) + "\n```"
    analyzer, _ = _workers(AppConfig(), lambda req, n: httpx.Response(200, json=_chat_completion(fenced)))
    assert analyzer.analyze(image_file, "發票").vendor == "測試商店"

    # JSON Mode 文件描述的舊格式:{"result": {"response": {...}}}
    legacy = {"result": {"response": GOOD_REPLY}, "success": True, "errors": [], "messages": []}
    analyzer, _ = _workers(AppConfig(), lambda req, n: httpx.Response(200, json=legacy))
    assert analyzer.analyze(image_file, "發票").amount == 1854.0


def test_workers_ai_retries_once_when_reply_is_not_json(cf_env, image_file):
    replies = ["抱歉，我無法確定這張文件的內容。", json.dumps(GOOD_REPLY)]
    analyzer, seen = _workers(
        AppConfig(), lambda req, n: httpx.Response(200, json=_chat_completion(replies[n - 1]))
    )

    result = analyzer.analyze(image_file, doc_type_hint="發票")

    assert result.doc_type == "發票"
    assert len(seen) == 2
    prompt, _ = get_prompt("發票")
    assert _user_text(seen[0]) == prompt
    second = _user_text(seen[1])
    assert second.startswith(prompt) and len(second) > len(prompt)
    assert "只輸出" in second[len(prompt):] and "JSON" in second[len(prompt):]


def test_workers_ai_raises_after_two_non_json_replies(cf_env, image_file):
    analyzer, seen = _workers(
        AppConfig(), lambda req, n: httpx.Response(200, json=_chat_completion("這不是 JSON"))
    )

    with pytest.raises(ValueError, match="JSON"):
        analyzer.analyze(image_file, doc_type_hint="發票")
    assert len(seen) == 2


def test_workers_ai_non_object_json_counts_as_failure(cf_env, image_file):
    replies = ['["發票"]', json.dumps(GOOD_REPLY)]
    analyzer, seen = _workers(
        AppConfig(), lambda req, n: httpx.Response(200, json=_chat_completion(replies[n - 1]))
    )

    assert analyzer.analyze(image_file, "發票").doc_type == "發票"
    assert len(seen) == 2


@pytest.mark.parametrize("missing", ["CF_ACCOUNT_ID", "CF_API_TOKEN"])
def test_workers_ai_missing_env_names_the_variable(monkeypatch, image_file, missing):
    monkeypatch.setenv("CF_ACCOUNT_ID", FAKE_ACCOUNT)
    monkeypatch.setenv("CF_API_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv(missing)
    analyzer, seen = _workers(AppConfig(), lambda req, n: pytest.fail("缺設定時不應發出請求"))

    with pytest.raises(WorkersAIConfigError) as exc_info:
        analyzer.analyze(image_file, doc_type_hint="發票")

    message = str(exc_info.value)
    assert missing in message
    assert FAKE_TOKEN not in message
    assert seen == []


def test_workers_ai_missing_env_uses_configured_names(monkeypatch, image_file):
    monkeypatch.delenv("MY_CF_ACCOUNT", raising=False)
    monkeypatch.delenv("MY_CF_TOKEN", raising=False)
    cfg = AppConfig(workers_ai=WorkersAIConfig(account_id_env="MY_CF_ACCOUNT", api_token_env="MY_CF_TOKEN"))
    analyzer, _ = _workers(cfg, lambda req, n: pytest.fail("缺設定時不應發出請求"))

    with pytest.raises(WorkersAIConfigError) as exc_info:
        analyzer.analyze(image_file)

    assert "MY_CF_ACCOUNT" in str(exc_info.value)
    assert "MY_CF_TOKEN" in str(exc_info.value)


def test_workers_ai_http_error_is_clear_and_hides_token(cf_env, image_file):
    envelope = {
        "result": None,
        "success": False,
        "errors": [{"code": 10000, "message": "Authentication error"}],
        "messages": [],
    }
    analyzer, seen = _workers(AppConfig(), lambda req, n: httpx.Response(401, json=envelope))

    with pytest.raises(WorkersAIError) as exc_info:
        analyzer.analyze(image_file, doc_type_hint="發票")

    message = str(exc_info.value)
    assert "401" in message and "Authentication error" in message
    assert FAKE_TOKEN not in message
    assert len(seen) == 1  # HTTP 錯誤不是 JSON 解析問題,不重試


# --- Ollama(假 client,不需 Ollama 服務) ---


class FakeOllamaClient:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=self.replies[len(self.calls) - 1]))


def test_ollama_request_and_source_model(image_file):
    cfg = AppConfig()
    client = FakeOllamaClient([json.dumps(GOOD_REPLY)])

    result = OllamaAnalyzer(cfg, client=client).analyze(image_file, doc_type_hint="發票")

    assert result.source_model == f"ollama:{cfg.ollama.model}"
    call = client.calls[0]
    prompt, schema = get_prompt("發票")
    assert call["model"] == cfg.ollama.model
    assert call["format"] == schema
    assert call["options"]["temperature"] == 0
    assert call["messages"][0]["content"] == prompt
    assert call["messages"][0]["images"] == [prepare_image(image_file).data]


def test_ollama_turns_off_thinking_on_every_call(image_file):
    # Gemma 4 預設先「思考」:輸出額度用完時 content 是空字串(S0-3 實測 22 次全空),重試那次也要關
    client = FakeOllamaClient(["{截斷的輸出", json.dumps(GOOD_REPLY)])

    OllamaAnalyzer(AppConfig(), client=client).analyze(image_file, doc_type_hint="發票")

    assert [call.get("think") for call in client.calls] == [False, False]


def test_ollama_sets_context_length_on_every_call(image_file):
    # 手機照片(約 300 萬像素)加通用版提示詞約 3,900 個 token:用 Ollama 預設的 4096,回答寫到一半就被截斷
    # (10/5 實測 done_reason=length)。每次呼叫都帶設定的 num_ctx,重試那次也要
    cfg = AppConfig()
    client = FakeOllamaClient(["{截斷的輸出", json.dumps(GOOD_REPLY)])

    OllamaAnalyzer(cfg, client=client).analyze(image_file)

    assert [call["options"].get("num_ctx") for call in client.calls] == [cfg.ollama.num_ctx] * 2


def test_ollama_retries_once_when_reply_is_not_json(image_file):
    client = FakeOllamaClient(["{截斷的輸出", json.dumps(GOOD_REPLY)])

    result = OllamaAnalyzer(AppConfig(), client=client).analyze(image_file, doc_type_hint="發票")

    assert result.amount == 1854.0
    assert len(client.calls) == 2
    prompt, _ = get_prompt("發票")
    second = client.calls[1]["messages"][0]["content"]
    assert second.startswith(prompt) and "只輸出" in second[len(prompt):]


def test_ollama_accepts_fenced_json(image_file):
    # 與 Workers AI 共用同一份寬鬆解析:```json 圍欄不算失敗,不必重試
    fenced = "```json\n" + json.dumps(GOOD_REPLY, ensure_ascii=False) + "\n```"
    client = FakeOllamaClient([fenced])

    result = OllamaAnalyzer(AppConfig(), client=client).analyze(image_file, doc_type_hint="發票")

    assert result.vendor == "測試商店"
    assert len(client.calls) == 1


def test_ollama_raises_after_two_non_json_replies(image_file):
    client = FakeOllamaClient(["不是 JSON", "還是不是"])

    with pytest.raises(ValueError, match="JSON"):
        OllamaAnalyzer(AppConfig(), client=client).analyze(image_file)
    assert len(client.calls) == 2


# --- 隱私分流 ---


class FakeAnalyzer:
    def __init__(self, name: str, doc_type: str = "發票"):
        self.name = name
        self.doc_type = doc_type
        self.hints: list[str | None] = []

    def analyze(self, file_path, doc_type_hint=None):
        self.hints.append(doc_type_hint)
        return ExtractionResult(doc_type=self.doc_type, confidence=0.9, source_model=self.name)


def _routing(cfg=None, cloud_doc_type="發票"):
    local, cloud = FakeAnalyzer("local"), FakeAnalyzer("cloud", cloud_doc_type)
    return RoutingAnalyzer(local, cloud, cfg or AppConfig(provider="workers_ai")), local, cloud


def test_routing_sends_non_sensitive_hint_to_cloud(tmp_path):
    router, local, cloud = _routing()

    result = router.analyze(tmp_path / "x.png", doc_type_hint="發票")

    assert result.source_model == "cloud"
    assert cloud.hints == ["發票"] and local.hints == []


@pytest.mark.parametrize("hint", [None, "藥袋", "", "   ", "處方箋", "其他"])
def test_routing_keeps_sensitive_missing_or_unknown_hint_local(tmp_path, hint):
    router, local, cloud = _routing()

    result = router.analyze(tmp_path / "x.png", doc_type_hint=hint)

    assert result.source_model == "local"
    assert cloud.hints == [] and len(local.hints) == 1


def test_routing_passes_normalized_hint_downstream(tmp_path):
    router, local, cloud = _routing()

    router.analyze(tmp_path / "x.png", doc_type_hint="  ")
    router.analyze(tmp_path / "y.png", doc_type_hint=" 帳單 ")

    assert local.hints == [None]  # 空白提示等同沒有提示,get_prompt 不會收到空白字串
    assert cloud.hints == ["帳單"]


@pytest.mark.parametrize("hint", ["公文", "帳單", "發票", "收據", None])
def test_routing_local_only_keeps_every_hint_local_and_passes_it_on(tmp_path, hint):
    """使用者選了敏感大類(Pipeline 傳 local_only=True):不論類型都在本機,類型提示照樣交給本機挑提示詞。"""
    router, local, cloud = _routing()

    result = router.analyze(tmp_path / "x.png", doc_type_hint=hint, local_only=True)

    assert result.source_model == "local"
    assert local.hints == [hint] and cloud.hints == []
    assert router.use_cloud(hint, local_only=True) is False


def test_sensitive_category_stays_local_with_the_type_prompt(cf_env, image_file):
    """驗收:醫療與保險 + 公文在雲端模式也只在本機辨識,而且用公文的提示詞;生活契約 + 帳單照常走雲端。

    兩端都是真的 provider(Ollama 用假 client、Workers AI 用 MockTransport),不連網。
    """
    cfg = AppConfig(provider="workers_ai")
    ollama = FakeOllamaClient([json.dumps(GOOD_REPLY)])
    workers, seen = _workers(cfg, lambda req, n: httpx.Response(200, json=_chat_completion(json.dumps(GOOD_REPLY))))
    router = RoutingAnalyzer(OllamaAnalyzer(cfg, client=ollama), workers, cfg)

    local = router.analyze(image_file, doc_type_hint="公文", local_only=True)
    assert seen == [] and len(ollama.calls) == 1                 # 一個位元組都沒送雲端
    prompt, schema = get_prompt("公文")
    assert ollama.calls[0]["messages"][0]["content"] == prompt and ollama.calls[0]["format"] == schema
    assert local.source_model == f"ollama:{cfg.ollama.model}"

    cloud = router.analyze(image_file, doc_type_hint="帳單")      # 生活契約不是敏感大類:local_only=False
    assert len(seen) == 1 and len(ollama.calls) == 1
    assert _user_text(seen[0]) == get_prompt("帳單")[0]
    assert cloud.source_model == f"workers_ai:{cfg.workers_ai.model}"


def test_routing_respects_configured_local_only_types(tmp_path):
    router, local, cloud = _routing(AppConfig(provider="workers_ai", local_only_doc_types=("藥袋", "公文")))

    router.analyze(tmp_path / "x.png", doc_type_hint="公文")
    router.analyze(tmp_path / "y.png", doc_type_hint="帳單")

    assert local.hints == ["公文"]
    assert cloud.hints == ["帳單"]


def test_routing_flags_cloud_result_that_is_local_only_type(tmp_path, caplog):
    router, _, _ = _routing(cloud_doc_type="藥袋")

    with caplog.at_level(logging.WARNING, logger="src.providers.routing"):
        result = router.analyze(tmp_path / "x.png", doc_type_hint="收據")

    assert "藥袋" in result.notes and "雲端" in result.notes
    assert any(r.levelno == logging.WARNING and "藥袋" in r.getMessage() for r in caplog.records)


def test_routing_does_not_flag_matching_cloud_result(tmp_path, caplog):
    router, _, _ = _routing(cloud_doc_type="發票")

    with caplog.at_level(logging.WARNING, logger="src.providers.routing"):
        result = router.analyze(tmp_path / "x.png", doc_type_hint="發票")

    assert result.notes == ""
    assert not caplog.records


def test_routing_builds_analyzers_lazily(tmp_path):
    built: list[str] = []
    local, cloud = FakeAnalyzer("local"), FakeAnalyzer("cloud")

    def make_local():
        built.append("local")
        return local

    router = RoutingAnalyzer(make_local, cloud, AppConfig(provider="workers_ai"))
    router.analyze(tmp_path / "x.png", doc_type_hint="發票")
    assert built == []  # 只用到雲端時,本機 provider 從未建立

    router.analyze(tmp_path / "x.png", doc_type_hint="藥袋")
    router.analyze(tmp_path / "x.png")
    assert built == ["local"]  # 建立一次後重複使用


def test_create_analyzer_workers_ai_returns_router_without_building_ollama(monkeypatch):
    built: list[AppConfig] = []
    monkeypatch.setattr(providers, "OllamaAnalyzer", lambda cfg: built.append(cfg))

    analyzer = create_analyzer(AppConfig(provider="workers_ai"))

    assert isinstance(analyzer, RoutingAnalyzer)
    assert built == []


def test_create_analyzer_router_routes_medication_bag_to_ollama(monkeypatch, tmp_path):
    local = FakeAnalyzer("ollama-fake", "藥袋")
    monkeypatch.setattr(providers, "OllamaAnalyzer", lambda cfg: local)

    result = create_analyzer(AppConfig(provider="workers_ai")).analyze(tmp_path / "x.png", "藥袋")

    assert result.source_model == "ollama-fake"
    assert local.hints == ["藥袋"]


def test_create_analyzer_mock_flag_wins():
    assert type(create_analyzer(AppConfig(provider="workers_ai"), mock=True)).__name__ == "MockAnalyzer"
