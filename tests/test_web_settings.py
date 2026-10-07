"""系統設定頁(SET)的網頁測試:選單、目前生效的設定(current_cfg)、設定頁、匯出與刪除全部資料。

資料夾隔離在 tmp_path;Ollama 檢查注入 httpx.MockTransport,雲端金鑰是假的環境變數,
analyzer 一律是 MockAnalyzer(不連網、不呼叫模型)。
"""
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import web.app as web_app
from src import review
from src.providers import MockAnalyzer
from src.settings import PROVIDER, THRESHOLD
from src.store import Store
from test_web_correct import browser_form
from web.app import CSRF_FIELD, create_app


@pytest.fixture
def cloud_keys(monkeypatch):
    """這台電腦設好了雲端模型的帳號與金鑰(假的值)。"""
    monkeypatch.setenv("CF_ACCOUNT_ID", "synthetic-account")
    monkeypatch.setenv("CF_API_TOKEN", "synthetic-token")


@pytest.fixture
def no_cloud_keys(monkeypatch):
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)


def ollama(status: int = 200) -> httpx.MockTransport:
    """假的 Ollama:/api/version 回 status;連線失敗用 ollama_down。"""
    return httpx.MockTransport(lambda request: httpx.Response(status, json={"version": "0.22.0"}))


def ollama_down() -> httpx.MockTransport:
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    return httpx.MockTransport(refuse)


@pytest.fixture
def local_client(cfg, form_client):
    """本機模式(ollama)的 app:Ollama 檢查注入假的、連得上;上傳用 MockAnalyzer。"""
    cfg.provider = "ollama"
    return form_client(create_app(cfg, analyzer=MockAnalyzer(cfg), ollama_transport=ollama()))


def upload(client, png: bytes, name: str = "photo.png"):
    return client.post("/upload", files={"file": (name, png, "image/png")}, data={"doc_type": "不確定"},
                       follow_redirects=False)


# ---- 選單 --------------------------------------------------------------------------

_SIDE_NAV = re.compile(r'<nav class="side__nav"[^>]*>(.*?)</nav>', re.S)
_MENU_NAV = re.compile(r'<nav class="menu__panel"[^>]*>(.*?)</nav>', re.S)


@pytest.mark.parametrize("url", ["/", "/confirm", "/review", "/doc/999"])
def test_menu_has_cabinet_under_home_and_settings_last(client, url):
    html = client.get(url).text
    for nav in (_SIDE_NAV, _MENU_NAV):                         # 電腦的左側選單與手機的「選單」
        links = re.findall(r'href="([^"]+)"', nav.search(html).group(1))
        assert links == ["/", "/cabinet", "/confirm", "/review", "/settings"], url
    assert html.count("<span>文件櫃</span>") == html.count("<span>設定</span>") == 2


def test_html_carries_the_font_size_hook(client):
    """字級偏好寫在 <html data-font>(app.js 依瀏覽器存的值改寫);沒有 JS 時是標準。"""
    assert '<html lang="zh-Hant-TW" data-font="standard">' in client.get("/").text


# ---- 目前生效的設定:設定頁存的值馬上生效 ------------------------------------------------

def test_upload_uses_the_saved_threshold(client, store, png_bytes):
    store.set_setting(THRESHOLD, "0.95")
    r = upload(client, png_bytes())
    doc = store.get_document(int(r.headers["location"].rsplit("/", 1)[1]))
    assert doc["action"] == "review" and "門檻 0.95" in doc["reason"]


def test_switching_mode_rebuilds_the_analyzer(cfg, store, form_client, monkeypatch, cloud_keys, png_bytes):
    """沒注入 analyzer 時依目前的辨識模式建立;模式改了,下一份上傳就用新的(同一個模式不重建)。"""
    cfg.provider = "ollama"
    built = []

    def fake_create_analyzer(current):
        built.append((current.provider, current.local_only_doc_types))
        return MockAnalyzer(current)

    monkeypatch.setattr(web_app, "create_analyzer", fake_create_analyzer)
    client = form_client(create_app(cfg))
    assert built == []                                         # 開頁面不建立,第一次上傳才建立
    upload(client, png_bytes(), "a.png")
    upload(client, png_bytes(), "b.png")
    assert built == [("ollama", ("藥袋",))]
    store.set_setting(PROVIDER, "workers_ai")
    upload(client, png_bytes(), "c.png")
    assert built == [("ollama", ("藥袋",)), ("workers_ai", ("藥袋",))]


def test_concurrent_uploads_build_the_analyzer_once(cfg, form_client, monkeypatch, png_bytes):
    """兩份上傳同時進來、還沒有 analyzer:「比較 → 建立 → 寫入」整段鎖住,同一個模式只建一次,兩份用同一個(SEC-08)。
    沒有鎖時兩邊都看到「還沒建」,各建一個;鍵與 analyzer 分兩次寫,切換模式的那一瞬間也可能對不上。"""
    cfg.provider = "ollama"
    built, used, gate = [], [], threading.Barrier(2)

    def slow_create(current):
        built.append(current.provider)
        try:
            gate.wait(timeout=1.0)      # 沒有鎖:兩份上傳都進得來;有鎖:另一份在外面等,這邊等不到就往下走
        except threading.BrokenBarrierError:
            pass
        return MockAnalyzer(current)

    class RecordingPipeline:            # 只看上傳拿到哪一個 analyzer,不真的跑辨識
        def __init__(self, current, analyzer, store):
            used.append(analyzer)

        def process_file(self, path, hint, category=None, label=None):
            return {"文件ID": 1}

    monkeypatch.setattr(web_app, "create_analyzer", slow_create)
    monkeypatch.setattr(web_app, "Pipeline", RecordingPipeline)
    clients = [form_client(create_app(cfg))]
    clients.append(form_client(clients[0].app))
    assert all(c.get("/").status_code == 200 for c in clients) and built == []   # 開頁面不建立
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = list(pool.map(lambda pair: upload(pair[0], png_bytes(), pair[1]), zip(clients, ["a.png", "b.png"])))
    assert [r.status_code for r in sent] == [303, 303]
    assert built == ["ollama"] and len(used) == 2 and used[0] is used[1]


def test_injected_analyzer_is_always_used(cfg, store, form_client, monkeypatch, cloud_keys, png_bytes):
    cfg.provider = "ollama"
    monkeypatch.setattr(web_app, "create_analyzer", lambda current: pytest.fail("不該另外建立 analyzer"))
    client = form_client(create_app(cfg, analyzer=MockAnalyzer(cfg)))
    store.set_setting(PROVIDER, "workers_ai")
    assert upload(client, png_bytes()).status_code == 303


def test_privacy_notes_follow_the_current_mode(local_client, store, cloud_keys):
    """安心說明與上傳區的藥袋提示說的是目前的辨識模式,不是 config.yaml 寫的(原則 7)。"""
    html = local_client.get("/").text
    assert "文件都在這台電腦上辨識，不會送到雲端。" in html and "兩類只在這台電腦處理" not in html
    store.set_setting(PROVIDER, "workers_ai")
    html = local_client.get("/").text
    assert "其他文件會交給雲端模型讀取" in html and "醫療與保險、身分證明兩類只在這台電腦處理" in html
    assert "不會送到雲端。照片" not in html


def test_healthz_reports_the_current_mode(local_client, store, cloud_keys):
    assert local_client.get("/healthz").json()["provider"] == "ollama"
    store.set_setting(PROVIDER, "workers_ai")
    body = local_client.get("/healthz").json()
    assert body["provider"] == "workers_ai" and set(body["models"]) == {"cloud", "local"}


# ---- 設定頁:系統狀態(唯讀) -----------------------------------------------------------

def _text(html: str) -> str:
    """頁面主要內容的純文字(去掉標籤、合併空白),比對畫面上的句子用。"""
    main = re.search(r"<main.*?</main>", html, re.S).group(0)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", main))


def test_settings_page_is_in_the_menu_and_has_four_parts(client):
    r = client.get("/settings")
    assert r.headers["cache-control"] == "no-store" and "default-src 'self'" in r.headers["content-security-policy"]
    html = r.text
    assert '<a class="side__link" href="/settings" aria-current="page">' in html
    assert "<title>設定 - 看有 高齡家庭文書輔助</title>" in html
    for title in ("系統狀態", "這台裝置", "系統設定", "資料管理"):
        assert f">{title} <span class=\"tag\">" in html or f">{title}</h2>" in html, title


def test_status_in_local_mode(local_client):
    text = _text(local_client.get("/settings").text)
    assert "辨識模式 本機（這台電腦） 模型連得上" in text
    assert "使用的模型 Gemma 4 12B（本機）" in text
    assert "只在本機處理 藥袋、醫療與保險、身分證明，以及沒選類型的文件" in text
    assert "自動存檔門檻 驗證信心 80% 以上才自動存檔，其餘交給家人複核" in text
    assert f"版本 {web_app.APP_VERSION}" in text


@pytest.mark.parametrize("transport", [ollama(500), ollama_down()])
def test_status_says_when_the_local_model_is_unreachable(cfg, form_client, transport):
    cfg.provider = "ollama"
    html = form_client(create_app(cfg, analyzer=MockAnalyzer(cfg), ollama_transport=transport)).get("/settings").text
    assert "模型連不上" in html and "pill--manual" in html and "模型連得上" not in html


def test_status_in_cloud_mode_lists_both_models(local_client, store, cloud_keys):
    store.set_setting(PROVIDER, "workers_ai")
    store.set_setting(THRESHOLD, "0.90")
    text = _text(local_client.get("/settings").text)
    assert "辨識模式 雲端備援（敏感文件仍在這台電腦） 本機模型連得上" in text
    assert "使用的模型 Gemma 4 26B（雲端）、Gemma 4 12B（本機）" in text
    assert "驗證信心 90% 以上才自動存檔" in text


def test_status_in_demo_mode_never_probes_ollama(cfg, form_client):
    def fail(request):
        pytest.fail("展示模式不該連 Ollama")
    html = form_client(create_app(cfg, analyzer=MockAnalyzer(cfg),
                                  ollama_transport=httpx.MockTransport(fail))).get("/settings").text
    text = _text(html)
    assert "辨識模式 展示模式（使用模擬讀值）" in text and "模型連" not in text
    assert "只在本機處理 全部文件（展示模式不會把文件送出這台電腦）" in text


# ---- 設定頁:這台裝置的偏好 ---------------------------------------------------------------

_PREF_INPUT = re.compile(r'<input class="chip__input" type="radio" name="pref-(\w+)" value="(\w+)" '
                         r'data-pref="\1" disabled( checked)?>')


def test_device_preferences_are_disabled_defaults_without_js(client):
    """沒有 JS 時顯示預設、選項不可用(app.js 才打開並勾上這台裝置存的值)。"""
    html = client.get("/settings").text
    found = _PREF_INPUT.findall(html)
    assert [(name, value) for name, value, _ in found] == [
        ("font", "standard"), ("font", "large"), ("font", "xlarge"), ("rate", "slow"), ("rate", "standard")]
    assert [(name, value) for name, value, checked in found if checked] == [("font", "standard"), ("rate", "standard")]
    assert "<noscript>" in html and "改了馬上生效，不影響家裡其他人的手機。" in html


def test_font_and_rate_values_match_css_js_and_speech():
    """選項的值、app.css 的放大規則、app.js 的可選值、speech.js 的語速要對得上。"""
    from web.render import DEVICE_PREFS, STATIC_DIR
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    speech = (STATIC_DIR / "speech.js").read_text(encoding="utf-8")
    prefs = {name: [value for value, _ in options] for name, _, options, _ in DEVICE_PREFS}
    assert 'html[data-font="large"] { font-size: 112.5%; }' in css
    assert 'html[data-font="xlarge"] { font-size: 125%; }' in css
    for name, values in prefs.items():
        assert re.search(rf'{name}: {{ values: \[{", ".join(f"{v!r}" for v in values)}\]'.replace("'", '"'), js), name
    assert 'getAttribute("data-rate") === "slow" ? 0.8 : 0.95' in speech
    assert "localStorage" in js and "try {" in js                  # 讀不到儲存空間也不會壞


# ---- 設定頁:系統設定的表單 -----------------------------------------------------------------

_PROVIDER_INPUT = re.compile(r'<input class="chip__input" type="radio" name="provider" value="(\w+)"([^>]*)>')


def test_threshold_choices_mark_the_default(client):
    html = client.get("/settings").text
    options = re.findall(r'<option value="([\d.]+)"( selected)?>([^<]+)</option>', html)
    assert options == [("0.80", " selected", "80%（預設）"), ("0.85", "", "85%"), ("0.90", "", "90%"), ("0.95", "", "95%")]
    assert "只能調得更嚴，不能低於 80%" in html


def test_demo_mode_only_shows_the_mode(client):
    html = client.get("/settings").text
    assert not _PROVIDER_INPUT.search(html) and "data-save-confirm" not in html
    assert "展示模式<span class=\"muted\">（使用模擬讀值，不能切換）</span>" in html


def test_cloud_option_is_disabled_without_keys(local_client, no_cloud_keys):
    html = local_client.get("/settings").text
    inputs = dict(_PROVIDER_INPUT.findall(html))
    assert inputs["ollama"].strip() == "checked"
    assert "disabled" in inputs["workers_ai"]
    assert "這台電腦還沒有設定雲端模型的帳號與金鑰，所以不能選" in html


def test_switching_to_cloud_asks_first(local_client, cloud_keys):
    """從本機切到雲端要先問(確認框的屬性放在藏著的那顆「儲存設定」,app.js 依選項換過去)。"""
    html = local_client.get("/settings").text
    inputs = dict(_PROVIDER_INPUT.findall(html))
    assert "disabled" not in inputs["workers_ai"] and "data-needs-confirm" in inputs["workers_ai"]
    assert "data-needs-confirm" not in inputs["ollama"]
    button = re.search(r"<button[^>]*data-save-confirm[^>]*>", html).group(0)
    assert " hidden disabled" in button
    assert 'data-confirm-title="確定要開啟雲端備援嗎？"' in button and 'data-confirm-ok="確定開啟"' in button
    assert 'data-confirm="開啟後，藥袋、醫療與保險、身分證明，以及沒選類型的文件仍只在這台電腦處理；' in button
    assert 'data-confirm-field="confirm_cloud"' in button      # 按了「確定開啟」,app.js 才補上伺服器要看的確認欄位
    assert "data-confirm" not in re.search(r"<button[^>]*data-save>", html).group(0)


def test_already_in_cloud_mode_needs_no_confirmation(local_client, store, cloud_keys):
    store.set_setting(PROVIDER, "workers_ai")
    inputs = dict(_PROVIDER_INPUT.findall(local_client.get("/settings").text))
    assert "checked" in inputs["workers_ai"] and "data-needs-confirm" not in inputs["workers_ai"]


def test_lock_line_says_medication_bags_stay_local(client):
    assert "藥袋、醫療與保險、身分證明一律只在這台電腦處理，這一項不能關。" in client.get("/settings").text


# ---- 設定頁:存檔 --------------------------------------------------------------------------

def save(client, **form):
    return client.post("/settings", data=form, follow_redirects=False)


def test_saving_a_stricter_threshold_takes_effect_and_is_logged(client, store, flash_text):
    r = save(client, auto_threshold="0.85")
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?msg=%E5%B7%B2%E5%84%B2%E5%AD%98%E8%A8%AD%E5%AE%9A"   # 已儲存設定
    assert store.get_settings() == {THRESHOLD: "0.85"}
    html = client.get(r.headers["location"]).text
    assert flash_text(html) == "已儲存設定"
    assert "驗證信心 85% 以上才自動存檔" in _text(html)
    assert re.search(r'<li>\d+月\d+日 \d\d:\d\d 自動存檔門檻改成 85%</li>', html)
    assert '<option value="0.85" selected>85%</option>' in html


@pytest.mark.parametrize("bad", ["0.79", "0.5", "0", "1", "abc"])
def test_threshold_below_080_is_refused(client, store, bad):
    r = save(client, auto_threshold=bad)
    assert r.status_code == 400
    assert "自動存檔門檻" in r.text and 'role="alert"' in r.text and 'aria-invalid="true"' in r.text
    assert '<option value="0.80" selected>' in r.text           # 選單回到目前的值
    assert store.get_settings() == {} and store.list_setting_changes() == []


def test_nothing_changed_is_said_and_not_logged(client, store, flash_text):
    r = save(client, auto_threshold="0.80")
    assert r.headers["location"].endswith("?msg=" + "%E8%A8%AD%E5%AE%9A%E6%B2%92%E6%9C%89%E8%AE%8A%E5%8B%95")
    assert flash_text(client.get(r.headers["location"]).text) == "設定沒有變動"
    assert store.list_setting_changes() == []


def test_cloud_mode_needs_keys(local_client, store, no_cloud_keys):
    r = save(local_client, provider="workers_ai", auto_threshold="0.90")
    assert r.status_code == 400 and "這台電腦還沒有設定雲端模型的帳號與金鑰，不能開啟雲端備援。" in r.text
    assert store.get_settings() == {}                          # 一欄有錯,整份不存(門檻也沒存)
    inputs = dict(_PROVIDER_INPUT.findall(r.text))
    assert '<option value="0.90" selected>' in r.text           # 剛剛的選擇留著,改好再送
    assert "checked" in inputs["ollama"]
    assert "確定要開啟雲端備援嗎？</h1>" not in r.text             # 不能開的不必問要不要開


def test_switching_to_cloud_with_keys(local_client, store, cloud_keys):
    """確認過(有 JS 時 app.js 在確認框按「確定開啟」後補上 confirm_cloud)才切到雲端;切回本機不必確認。"""
    r = save(local_client, provider="workers_ai", auto_threshold="0.80", confirm_cloud="1")
    assert r.status_code == 303 and store.get_settings() == {PROVIDER: "workers_ai"}
    html = local_client.get("/settings").text
    assert "辨識模式改成「可以用雲端備援」" in html
    assert "其他文件會交給雲端模型讀取" in html                     # 安心說明跟著改
    r = save(local_client, provider="ollama", auto_threshold="0.80")    # 切回本機不必確認
    assert r.status_code == 303 and store.get_settings() == {PROVIDER: "ollama"}
    assert "辨識模式改成「只用這台電腦」" in local_client.get("/settings").text


def test_switching_to_cloud_without_confirmation_asks_on_a_page(local_client, store, cloud_keys):
    """沒有 JS(或 app.js 沒跑起來)就沒有確認框:伺服器沒收到確認欄位不會切換(SEC-02),回一頁確認頁再問一次,
    字和確認框一樣;按「確定開啟」才生效,「先不要」回設定頁。門檻和辨識模式同一張表單,也等確認後一起存。"""
    r = save(local_client, provider="workers_ai", auto_threshold="0.90")
    assert r.status_code == 400 and store.get_settings() == {} and store.list_setting_changes() == []
    assert "<h1>確定要開啟雲端備援嗎？</h1>" in r.text
    assert "開啟後，藥袋、醫療與保險、身分證明，以及沒選類型的文件仍只在這台電腦處理；其他文件會交給雲端模型讀取。" in r.text
    assert '<a class="btn btn--secondary btn--lg" href="/settings">先不要</a>' in r.text
    assert "確定開啟</button>" in r.text and "取消" not in _text(r.text)
    assert r.text.index(">先不要</a>") < r.text.index("確定開啟</button>")      # 不做的那一顆在前面
    form = browser_form(r.text, "/settings")
    assert {k: v for k, v in form.items() if k != CSRF_FIELD} == {
        "provider": "workers_ai", "auto_threshold": "0.90", "confirm_cloud": "1"}
    r = local_client.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and store.get_settings() == {PROVIDER: "workers_ai", THRESHOLD: "0.90"}


@pytest.mark.parametrize("value", ["", "0", "yes", "on"])
def test_cloud_confirmation_field_must_be_the_expected_value(local_client, store, cloud_keys, value):
    r = save(local_client, provider="workers_ai", confirm_cloud=value)
    assert r.status_code == 400 and "<h1>確定要開啟雲端備援嗎？</h1>" in r.text and store.get_settings() == {}


def test_only_switching_to_cloud_needs_confirmation(local_client, store, cloud_keys):
    """只改門檻、或已經在雲端備援時再存一次,都不問。"""
    assert save(local_client, provider="ollama", auto_threshold="0.85").status_code == 303
    store.set_setting(PROVIDER, "workers_ai")
    assert save(local_client, provider="workers_ai", auto_threshold="0.90").status_code == 303
    assert store.get_settings() == {PROVIDER: "workers_ai", THRESHOLD: "0.90"}


def test_demo_mode_cannot_switch(client, store, cloud_keys):
    r = save(client, provider="ollama", auto_threshold="0.80")
    assert r.status_code == 400 and "展示模式不能切換辨識模式。" in r.text
    assert store.get_settings() == {}


def test_unknown_fields_are_ignored(local_client, store, cloud_keys):
    """表單值只是資料:藥袋本機限定、分級之類的欄位塞進來也不看。"""
    save(local_client, auto_threshold="0.90", local_only_doc_types="", tier="auto")
    assert store.get_settings() == {THRESHOLD: "0.90"}
    assert "藥袋" in local_client.get("/settings").text


def test_who_changed_it_is_recorded_but_not_trusted(client, store):
    """Access 登入的 email 只用來記錄(不當授權);不像 email 的標頭值當作沒有。"""
    client.post("/settings", data={"auto_threshold": "0.85"},
                headers={"Cf-Access-Authenticated-User-Email": "family@example.com"})
    client.post("/settings", data={"auto_threshold": "0.90"},
                headers={"Cf-Access-Authenticated-User-Email": "<script>alert(1)</script> x"})
    client.post("/settings", data={"auto_threshold": "0.95"})
    actors = [c["actor"] for c in store.list_setting_changes()]
    assert actors == [None, None, "family@example.com"]
    html = client.get("/settings").text
    assert "自動存檔門檻改成 85%<span class=\"log__who\">（family@example.com）</span>" in html
    assert "<script>alert" not in html


def test_change_log_lists_the_latest_ten(client, store):
    for i in range(12):
        store.set_setting(THRESHOLD, f"0.{80 + i}")
    html = client.get("/settings").text
    assert html.count("自動存檔門檻改成") == 10 and "改成 91%" in html and "改成 81%" not in html


def test_empty_change_log(client):
    assert "還沒有改過設定。" in client.get("/settings").text


# ---- 資料管理:匯出 ------------------------------------------------------------------------

@pytest.fixture
def filled(cfg, store, add_doc, png_bytes):
    """一份有原件、提醒與更正的帳單,加一筆設定變更;回傳原件路徑。"""
    from samples import BILL, REMINDER
    original = cfg.paths.archive / "帳單" / "2099-10" / "bill.png"
    original.parent.mkdir(parents=True)
    original.write_bytes(png_bytes())
    doc_id = add_doc(BILL, target=original)
    store.add_action(doc_id, "calendar", "auto", REMINDER)
    store.add_correction({"amount": 1800}, {"amount": 1854}, document_id=doc_id)
    store.set_setting(THRESHOLD, "0.85", actor="family@example.com")
    return original


def test_export_downloads_all_records_without_images(client, filled, png_bytes):
    import json
    r = client.get("/settings/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"                    # 內容有個資:瀏覽器與代理都不存
    assert r.headers["x-content-type-options"] == "nosniff"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="records-') and ".json" in disposition
    assert "filename*=UTF-8''%E7%9C%8B%E6%9C%89" in disposition       # 看有紀錄-….json
    assert "set-cookie" not in r.headers
    data = json.loads(r.text)
    assert {"documents", "actions", "corrections", "setting_changes"} <= set(data)
    assert data["documents"][0]["result"]["vendor"] == "示範電力公司"
    assert data["documents"][0]["target_path"] == "archive/帳單/2099-10/bill.png"
    assert data["actions"][0]["payload"]["title"] == "繳電費"
    assert data["corrections"][0]["after"] == {"amount": 1854}
    assert data["setting_changes"][0]["actor"] == "family@example.com"
    assert "IHDR" not in r.text and str(filled.parent.parent.parent) not in r.text   # 不含影像與完整路徑


def test_export_link_and_delete_button_on_the_page(client):
    html = client.get("/settings").text
    assert '<a class="btn btn--secondary btn--lg" href="/settings/export" download>' in html
    button = re.search(r'<button class="btn btn--danger btn--lg"[^>]*>', html).group(0)
    assert 'data-confirm-title="確定要刪除全部資料嗎？"' in button and 'data-confirm-ok="確定刪除"' in button
    assert "無法復原" in button and 'data-confirm-field="understood"' in button   # 確認後 app.js 補上的欄位
    # 沒有 JS 就沒有確認框:要先勾「我知道刪除後無法復原」才送得出去
    assert re.search(r'<noscript><label class="check-line"><input type="checkbox" name="understood" value="1" '
                     r'required>', html)


# ---- 資料管理:刪除全部資料 --------------------------------------------------------------------

def delete_all(client, **kwargs):
    """按「刪除全部資料」而且確認過了:有 JS 時 app.js 在確認框按「確定刪除」後補上 understood,
    沒有 JS 時是表單裡必勾的「我知道刪除後無法復原」。"""
    return client.post("/settings/delete", data={"understood": "1"}, **kwargs)


def test_delete_without_confirmation_asks_on_a_page(cfg, client, store, filled):
    """伺服器沒收到確認欄位就不刪(SEC-02;原本只有瀏覽器檢查必勾):回一頁確認頁,字和確認框一樣,
    按「確定刪除」才刪,「先不要」回設定頁。"""
    records = cfg.paths.logs / "records.jsonl"
    records.write_text("{}\n", encoding="utf-8")
    r = client.post("/settings/delete")
    assert r.status_code == 400 and "<h1>確定要刪除全部資料嗎？</h1>" in r.text
    assert "所有文件的照片、讀值、提醒與更正紀錄都會刪除，無法復原；設定與設定變更紀錄會保留。" in r.text
    assert '<a class="btn btn--secondary btn--lg" href="/settings">先不要</a>' in r.text
    assert "確定刪除</button>" in r.text and "取消" not in _text(r.text)
    assert len(store.list_documents()) == 1 and filled.exists() and records.exists()
    assert not _purge_logged(store)
    form = browser_form(r.text, "/settings/delete")
    assert {k: v for k, v in form.items() if k != CSRF_FIELD} == {"understood": "1"}
    r = client.post("/settings/delete", data=form, follow_redirects=False)
    assert r.status_code == 303 and store.list_documents() == [] and not filled.exists() and not records.exists()


def test_delete_all_data(cfg, client, store, filled, tmp_path, flash_text):
    outside = tmp_path / "keep-me.png"
    outside.write_bytes(b"synthetic")
    inbox_file = cfg.paths.inbox / "scan.png"
    inbox_file.write_bytes(b"synthetic")
    review_file = cfg.paths.review / "r.png"
    review_file.write_bytes(b"synthetic")
    records = cfg.paths.logs / "records.jsonl"
    records.write_text('{"AI辨識結果": {"vendor": "示範電力公司"}}\n', encoding="utf-8")   # 處理紀錄也含讀值
    r = delete_all(client, headers={"Cf-Access-Authenticated-User-Email": "family@example.com"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    assert not records.exists()                                 # 確認框說「讀值都會刪除」,這份也要刪
    assert flash_text(client.get(r.headers["location"]).text) == "已刪除全部資料"
    assert store.list_documents() == [] and store.list_actions() == []
    assert store.list_corrections(verified_only=False) == []
    assert not filled.exists() and not review_file.exists()
    assert outside.exists() and inbox_file.exists()             # 四個資料夾以外的一律不碰
    for folder in (cfg.paths.archive, cfg.paths.review, cfg.paths.failed, cfg.paths.uploads_path):
        assert folder.is_dir()                                  # 資料夾本身留著
    assert store.get_settings()[THRESHOLD] == "0.85"            # 設定與設定紀錄保留,刪除本身也記一筆
    latest = store.list_setting_changes()[0]
    assert latest["key"] == "purged" and latest["actor"] == "family@example.com"
    assert "刪除全部資料<span class=\"log__who\">（family@example.com）</span>" in client.get("/settings").text


def test_delete_says_when_some_files_are_stuck(client, cfg, store, filled, monkeypatch, flash_text):
    monkeypatch.setattr(web_app, "delete_data_files", lambda c: (0, 1))
    r = delete_all(client, follow_redirects=False)
    assert flash_text(client.get(r.headers["location"]).text) == "資料已刪除，但有些照片檔刪不掉，請管理者檢查資料夾。"


def _refuse(*args, **kwargs):
    raise PermissionError(13, "synthetic: file is in use")


def _purge_logged(store) -> bool:
    changes = store.list_setting_changes()
    return bool(changes) and changes[0]["key"] == "purged"


def test_delete_goes_on_when_the_processing_log_is_stuck(client, cfg, store, filled, monkeypatch, caplog):
    """records.jsonl 被別的程式開著(Windows 不能刪開著的檔):原本是「系統出了點問題」,資料庫清空了、
    原件與含讀值的處理紀錄還在,也沒記一筆(SEC-04)。現在其餘步驟照做、刪除照樣記一筆,
    畫面照實說哪些已刪、哪一樣刪不掉,下一步是回設定頁再按一次。"""
    monkeypatch.setattr(web_app, "clear_records", _refuse)
    r = delete_all(client)
    assert r.status_code == 500 and "<h1>有些資料還沒刪掉</h1>" in r.text and "系統出了點問題" not in r.text
    assert "文件、提醒、更正紀錄與照片都已經刪除，但處理紀錄檔（裡面有讀值）刪不掉" in r.text
    assert "再按一次「刪除全部資料」" in r.text and 'href="/settings"' in r.text and "回設定</a>" in r.text
    assert store.list_documents() == [] and not filled.exists()     # 資料庫與原件照樣刪掉
    assert _purge_logged(store)
    assert "PermissionError" in caplog.text and "synthetic" not in caplog.text   # 紀錄只寫例外類型,不寫訊息原文


def test_delete_says_when_both_photos_and_the_processing_log_are_stuck(client, store, filled, monkeypatch):
    monkeypatch.setattr(web_app, "clear_records", _refuse)
    monkeypatch.setattr(web_app, "delete_data_files", lambda c: (0, 2))
    r = delete_all(client)
    assert r.status_code == 500 and "<h1>有些資料還沒刪掉</h1>" in r.text
    assert "文件、提醒與更正紀錄已經刪除，但處理紀錄檔（裡面有讀值）和有些照片檔刪不掉" in r.text
    assert "照片都已經刪除" not in r.text                         # 沒刪掉的不寫成刪了
    assert store.list_documents() == [] and _purge_logged(store)


def test_delete_survives_an_error_while_removing_photos(client, cfg, store, filled, monkeypatch, flash_text):
    """刪照片檔整個出錯(例如資料夾讀不了):當成有照片檔刪不掉,處理紀錄照樣清、刪除照樣記一筆。"""
    records = cfg.paths.logs / "records.jsonl"
    records.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "delete_data_files", _refuse)
    r = delete_all(client, follow_redirects=False)
    assert flash_text(client.get(r.headers["location"]).text) == "資料已刪除，但有些照片檔刪不掉，請管理者檢查資料夾。"
    assert store.list_documents() == [] and not records.exists() and _purge_logged(store)


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 不能刪開著的檔")
def test_delete_with_the_processing_log_really_open(client, cfg, store, filled, flash_text):
    records = cfg.paths.logs / "records.jsonl"
    records.write_text('{"AI辨識結果": {"vendor": "示範電力公司"}}\n', encoding="utf-8")
    with records.open("rb"):
        r = delete_all(client)
    assert r.status_code == 500 and "處理紀錄檔（裡面有讀值）刪不掉" in r.text
    assert records.exists() and not filled.exists() and _purge_logged(store)
    again = delete_all(client, follow_redirects=False)     # 關掉那個程式後再按一次就補刪
    assert flash_text(client.get(again.headers["location"]).text) == "已刪除全部資料" and not records.exists()


def test_delete_stops_when_the_database_cannot_be_cleared(client, cfg, store, filled, monkeypatch, caplog):
    """資料庫刪不了(例如正被占用):後面都不做——先刪照片的話,畫面上會留著指不到原件的文件。
    照實說什麼都沒刪、可以再試;不是「系統出了點問題」,也不記成刪過。"""
    records = cfg.paths.logs / "records.jsonl"
    records.write_text("{}\n", encoding="utf-8")

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "purge_all", locked)
    r = delete_all(client)
    assert r.status_code == 503 and "資料沒有刪除" in r.text and "系統出了點問題" not in r.text
    assert "文件、照片與紀錄都還在" in r.text and 'href="/settings"' in r.text and "回設定</a>" in r.text
    assert len(store.list_documents()) == 1 and filled.exists() and records.exists()
    assert not _purge_logged(store)
    assert "OperationalError" in caplog.text


def test_delete_waits_for_a_correction_in_progress(cfg, app, form_client, store, filled, monkeypatch):
    """更正做到一半時按「刪除全部資料」:等它做完才刪(和改文件的路由同一把鎖,SEC-01)。
    不等的話,更正會在刪完之後把讀值寫回更正紀錄與 records.jsonl,畫面卻說已經全部刪除。"""
    doc_id = store.list_documents()[0]["id"]
    correcting, deleting = form_client(app), form_client(app)
    form = browser_form(correcting.get(f"/doc/{doc_id}/correct").text, f"/doc/{doc_id}/correct")
    form.update({"amount": "1900", CSRF_FIELD: correcting.csrf_token()})
    inside, purged, real = threading.Event(), threading.Event(), review.verify_decide_plan

    def gated(*args, **kwargs):
        inside.set()                    # 更正已經讀完文件,正在重新核對
        purged.wait(timeout=1.0)        # 沒有鎖:刪除在這時候做完;有鎖:刪除進不來,等不到就往下走
        return real(*args, **kwargs)

    monkeypatch.setattr(review, "verify_decide_plan", gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(correcting.post, f"/doc/{doc_id}/correct", data=form, follow_redirects=False)
        assert inside.wait(timeout=10)
        r = delete_all(deleting, follow_redirects=False)
        purged.set()
        assert saving.result(timeout=10).status_code == 303 and r.status_code == 303
    assert store.list_documents() == [] and store.list_actions() == []
    assert store.list_corrections(verified_only=False) == []
    assert not (cfg.paths.logs / "records.jsonl").exists()
    assert [p for p in cfg.paths.archive.rglob("*") if p.is_file()] == []


def test_delete_needs_post(client, store, filled):
    assert client.get("/settings/delete").status_code == 405
    assert len(store.list_documents()) == 1


# ---- CSRF:兩個新的 POST 都要帶 token ---------------------------------------------------------------

@pytest.mark.parametrize("url, data", [("/settings", {"auto_threshold": "0.95"}), ("/settings/delete", {})])
@pytest.mark.parametrize("problem", ["missing", "mismatch"])
def test_settings_posts_need_the_csrf_token(app, store, filled, form_client, url, data, problem):
    from fastapi.testclient import TestClient
    from web.app import CSRF_FIELD
    other_browser = form_client(app).csrf_token()
    client = TestClient(app)
    client.get("/")
    if problem == "mismatch":
        data = {**data, CSRF_FIELD: other_browser}
    r = client.post(url, data=data)
    assert r.status_code == 403 and "這次的操作沒有送出" in r.text
    assert store.get_settings() == {THRESHOLD: "0.85"} and len(store.list_documents()) == 1
    assert filled.exists()


def test_settings_forms_carry_the_token(client):
    html = client.get("/settings").text
    forms = re.findall(r'<form\b[^>]*\bmethod="post"[^>]*>(.*?)</form>', html, re.S)
    assert len(forms) == 2 and all('name="csrf_token"' in body for body in forms)


def test_settings_page_has_no_inline_code(client):
    html = client.get("/settings").text
    assert not re.search(r"<script(?![^>]*\bsrc=)|\son[a-z]+\s*=|\sstyle\s*=|javascript:", html, re.I)
