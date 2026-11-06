"""上傳路由測試:大小/副檔名限制、檔名重產、類型選項(11/4 起只選類型;舊版首頁送來的大類照舊收)、隱私分流、
轉到結果頁(不呼叫模型、不連網)。"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import web.app as web_app
from src.models import CATEGORIES, DOC_TYPES, SENSITIVE_CATEGORIES, ExtractionResult
from src.prompts import get_prompt
from src.providers import MockAnalyzer, OllamaAnalyzer, RoutingAnalyzer
from src.providers.workers_ai import WorkersAIAnalyzer
from src.store import Store
from web.app import create_app
from web.render import STATIC_DIR


class RecordingAnalyzer(MockAnalyzer):
    """記下每次收到的 doc_type_hint 與 local_only,確認網頁有照使用者的選擇傳下去。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.hints: list = []
        self.local_only: list = []

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.hints.append(doc_type_hint)
        self.local_only.append(local_only)
        return super().analyze(file_path, doc_type_hint)


class MedicationAnalyzer:
    """模擬真實 provider 收到「藥袋」提示後的輸出(合成資料)。"""

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        return ExtractionResult(
            doc_type="藥袋", date="2026-09-30", vendor="示範診所", confidence=0.9,
            plain_summary="這是示範診所開的兩種藥，飯後吃。",
            fields={"items": [
                {"name": "示範藥甲", "dose_text": "每次1顆", "frequency_text": "一天三次",
                 "timing": ["早", "中", "晚"], "prn": False, "days": 7},
                {"name": "示範藥乙", "dose_text": "每次半顆", "frequency_text": "需要時",
                 "timing": [], "prn": True, "days": 0},
            ]},
        )


@pytest.fixture
def analyzer(cfg):
    """覆寫 conftest 的 analyzer:共用的 client 會用這個記錄提示的版本。"""
    return RecordingAnalyzer(cfg)


@pytest.fixture
def upload(png_bytes):
    """回傳 upload(client, name=..., content=None, doc_type=..., field=..., category=None, extra=None):
    像在首頁選檔後按「開始辨識」。category 是第一列的大類(None = 不送這一欄,同舊版表單);extra 是多塞的欄位。"""
    def send(client, name="photo.png", content=None, doc_type="不確定", field="file", category=None, extra=None):
        content = png_bytes() if content is None else content
        data = {"doc_type": doc_type, **({"category": category} if category is not None else {}), **(extra or {})}
        return client.post(
            "/upload",
            files={field: (name, content, "image/png")},
            data=data,
            follow_redirects=False,
        )
    return send


def test_home_has_capture_input_and_type_choices(client):
    html = client.get("/").text
    assert 'type="file"' in html
    assert 'accept="image/*,application/pdf"' in html
    assert 'capture="environment"' in html
    assert re.search(r'value="不確定"\s+checked', html)
    for value in ("帳單", "公文", "藥袋", "收據"):
        assert f'value="{value}"' in html
    assert ">發票／收據</span>" in html and 'value="發票"' not in html     # 發票和收據合成一個選項
    assert 'aria-live="polite"' in html
    # 本機/展示模式所有文件都在這台電腦辨識,不必特別說明藥袋(雲端模式才顯示,見下一個測試)
    assert "只在這台電腦處理" not in html


def _cloud_cfg(tmp_path):
    """雲端模式(workers_ai)的設定,資料夾隔離在 tmp_path。"""
    from src.config import AppConfig, PathsConfig

    paths = PathsConfig(inbox=tmp_path / "inbox", archive=tmp_path / "archive", review=tmp_path / "review",
                        failed=tmp_path / "failed", logs=tmp_path / "logs")
    cfg = AppConfig(paths=paths, provider="workers_ai")
    cfg.ensure_dirs()
    return cfg


def test_cloud_mode_says_which_types_stay_local(tmp_path):
    """雲端模式的上傳區照實際分流寫:只看上傳時選的類型,不再提大類(上傳不選大類了)。"""
    from fastapi.testclient import TestClient

    cfg = _cloud_cfg(tmp_path)
    html = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg))).get("/").text
    upload_area = html.split('<section class="panel upload"')[1].split("</section>")[0]
    assert "藥袋、發票／收據，以及選「不確定」的文件只在這台電腦上辨識；帳單、公文會交給雲端模型讀取。" in upload_area
    assert not any(category in upload_area for category in CATEGORIES) and "拍了會交給家人複核" not in upload_area


def test_upload_redirects_to_result_page(client, upload):
    r = upload(client)
    assert r.status_code == 303
    assert re.fullmatch(r"/doc/\d+", r.headers["location"])

    page = client.get(r.headers["location"])
    assert page.status_code == 200
    html = page.text
    assert 'id="summary"' in html            # 白話解說區塊
    assert "讀到的內容" in html               # 欄位表
    assert "測試商店" in html                 # MockAnalyzer 的商家
    assert "data-speak" in html               # 朗讀按鈕
    assert "不提供醫療建議" not in html        # 非藥袋不顯示藥袋聲明


def test_uncertain_becomes_none_and_known_types_pass_through(client, analyzer, upload):
    """「不確定」與白名單以外的值當作沒選;舊版首頁的「發票」併進「發票／收據」(送「收據」,一律本機)。"""
    for choice in ("不確定", "藥袋", "帳單", "公文", "收據", "發票", "亂填的類型"):
        assert upload(client, doc_type=choice).status_code == 303
    assert analyzer.hints == [None, "藥袋", "帳單", "公文", "收據", "收據", None]


def _type_chips(html: str) -> list[tuple[str, str]]:
    """首頁上傳區的類型選項:(送出值, 畫面文字),照畫面順序。"""
    return re.findall(r'<input class="chip__input" type="radio" name="doc_type" value="([^"]+)"[^>]*><span>([^<]+)</span>',
                      html)


def test_every_form_value_reaches_pipeline_exactly(client, monkeypatch, upload):
    """隱私分流只認 DOC_TYPES 的原字串:首頁每個類型選項送到 process_file 都必須一字不差(「不確定」是 None),
    首頁不送大類,也沒有名稱(label)。"""
    seen = []
    original = web_app.Pipeline.process_file

    def spy(self, path, doc_type_hint=None, *, category=None, label=None, **kwargs):
        seen.append((doc_type_hint, category, label))
        return original(self, path, doc_type_hint, category=category, label=label, **kwargs)

    monkeypatch.setattr(web_app.Pipeline, "process_file", spy)
    values = [value for value, _ in _type_chips(client.get("/").text)]
    assert values[-1] == "不確定" and all(v in DOC_TYPES for v in values[:-1])   # 「不確定」在最後
    for value in values:
        assert upload(client, doc_type=value).status_code == 303
    assert seen == [(v, None, None) for v in values[:-1]] + [(None, None, None)]


def test_medication_hint_shows_disclaimer(client, upload):
    r = upload(client, doc_type="藥袋")
    assert "?" not in r.headers["location"]   # 提示存在資料庫,不靠網址參數
    html = client.get(r.headers["location"]).text
    assert "本系統只協助閱讀，不提供醫療建議；用藥請依醫師與藥師指示。" in html
    # 重新整理(同一網址再開一次)聲明仍在
    assert "不提供醫療建議" in client.get(r.headers["location"]).text


def test_medication_result_renders_items(cfg, form_client, upload):
    client = form_client(create_app(cfg, analyzer=MedicationAnalyzer()))
    r = upload(client, doc_type="藥袋")
    html = client.get(r.headers["location"]).text
    assert "AI 白話解說" in html
    assert "這是示範診所開的兩種藥，飯後吃。" in html
    assert "示範藥甲" in html and "示範藥乙" in html
    assert "需要時" in html
    assert "不提供醫療建議" in html


def test_upload_filename_is_regenerated(cfg, client, upload):
    r = upload(client, name="我的 帳單（1）.png")
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    doc = Store(cfg.paths.db_path).get_document(doc_id)
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}\.png", doc["source_file"])


def test_stored_original_keeps_no_gps_or_camera_details(cfg, client, upload):
    """存起來的原件(結果頁、匯出、家裡的電腦都看得到)不能留拍攝地點與裝置;轉向套到像素上。"""
    import io

    from PIL import Image
    img = Image.new("RGB", (300, 200), "white")
    exif = Image.Exif()
    exif[0x010F] = "SyntheticCam"                 # 拍攝裝置
    exif[0x0112] = 6                              # 轉向:要順時針轉 90°
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (25.0, 2.0, 30.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    r = upload(client, name="photo.jpg", content=buf.getvalue())
    doc = Store(cfg.paths.db_path).get_document(int(r.headers["location"].rsplit("/", 1)[1]))
    with Image.open(doc["target_path"]) as stored:
        assert not stored.getexif().get_ifd(0x8825) and 0x010F not in stored.getexif()
        assert stored.size == (200, 300)


def test_malicious_filename_does_not_escape_uploads(cfg, client, tmp_path, upload):
    r = upload(client, name="../x.png")
    assert r.status_code == 303
    assert not (tmp_path / "x.png").exists()
    assert not (cfg.paths.uploads_path.parent / "x.png").exists()
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    doc = Store(cfg.paths.db_path).get_document(doc_id)
    assert ".." not in doc["source_file"] and "x.png" not in doc["source_file"]
    target = Path(doc["target_path"]).resolve()
    assert target.is_relative_to(cfg.paths.archive.resolve())


def test_backslash_filename_does_not_escape(cfg, client, tmp_path, upload):
    r = upload(client, name="..\\..\\evil.png")
    assert r.status_code == 303
    assert not list(tmp_path.glob("*evil*"))


def test_second_picker_field_is_accepted(client, upload):
    r = upload(client, field="file_pick")
    assert r.status_code == 303


def test_oversize_upload_is_413(cfg, client, upload, png_bytes):
    big = png_bytes(pad=web_app.MAX_UPLOAD_BYTES + 1)
    r = upload(client, content=big)
    assert r.status_code == 413
    assert "15MB" in r.text
    assert not any(cfg.paths.uploads_path.iterdir())


def test_oversize_checked_while_copying(cfg, client, monkeypatch, upload, png_bytes):
    """Content-Length 看起來正常時,寫檔過程仍要數位元組、超過就刪掉。"""
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 1000)
    r = upload(client, content=png_bytes(pad=5000))
    assert r.status_code == 413
    assert not any(cfg.paths.uploads_path.iterdir())


@pytest.mark.parametrize("name", ["notes.txt", "run.exe", "page.html", "noext"])
def test_bad_extension_is_400(cfg, client, name, upload):
    r = upload(client, name=name)
    assert r.status_code == 400
    assert not any(cfg.paths.uploads_path.iterdir())


def test_content_not_matching_extension_is_400(cfg, client, upload):
    r = upload(client, name="fake.png", content=b"<html><script>alert(1)</script></html>")
    assert r.status_code == 400
    assert not any(cfg.paths.uploads_path.iterdir())


def test_missing_file_is_400_with_message_beside_field(client):
    r = client.post("/upload", data={"doc_type": "不確定"})
    assert r.status_code == 400
    assert 'id="file-error"' in r.text
    assert "請先拍照" in r.text


def test_empty_file_is_400(client, upload):
    r = upload(client, content=b"")
    assert r.status_code == 400


def test_home_lists_recent_documents(client, upload):
    r = upload(client)
    html = client.get("/").text
    assert f'href="{r.headers["location"]}"' in html
    assert "最近看過的文件" in html


def test_home_empty_state_invites_first_upload(client):
    assert "拍下第一張" in client.get("/").text


# ---- 上傳只選類型(使用者 11/4);大類只在文件櫃 --------------------------------------------------

def _doc_id(response) -> int:
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_home_offers_types_only(client):
    """上傳只選類型:帳單、公文、藥袋、發票／收據、不確定,預設「不確定」,不用先打開;四大類只在文件櫃。
    發票和收據合成一個選項、送「收據」(一律本機)。本機模式的上傳區不寫說明。"""
    html = client.get("/").text
    assert _type_chips(html) == [("帳單", "帳單"), ("公文", "公文"), ("藥袋", "藥袋"), ("收據", "發票／收據"),
                                 ("不確定", "不確定")]
    assert re.search(r'name="doc_type" value="不確定" checked', html)
    upload_area = html.split('<section class="panel upload"')[1].split("</section>")[0]
    assert 'name="category"' not in upload_area and "<details" not in upload_area
    assert not any(category in upload_area for category in CATEGORIES)
    assert "upload__note" not in upload_area                         # 本機模式:沒有說明
    # 選好照片後才用得到的:縮圖的位置、「換一張」
    assert "data-file-photo" in upload_area and ">換一張</button>" in upload_area


# 用假的 DOM 跑真的 app.js:選檔 → 縮圖、「換一張」、類型與「開始辨識」;換一張 → 回到兩顆按鈕;太大的檔不算選好
_PICK_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8"), state = {};
function el(attrs = {}) {
  return {attrs, hidden: false, textContent: "", listeners: {}, children: [],
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type) { (this.listeners[type] || []).forEach(fn => fn({type, target: this, preventDefault() {}})); },
    querySelector(sel) { return sel === "img" ? this.children[0] || null : null; },
    appendChild(child) { this.children.push(child); child.remove = () => { this.children = []; }; },
    focus() { state.focus = this; }};
}
function fileInput() {      // 和瀏覽器一樣:value 設成空字串就清掉選好的檔
  const input = el({type: "file"});
  let files = null;
  Object.defineProperty(input, "files", {get: () => files, set: v => { files = v; }});
  Object.defineProperty(input, "value", {get: () => (files ? "C:\\fakepath\\x" : ""), set: v => { if (v === "") files = null; }});
  return input;
}
const camera = fileInput(), pick = fileInput();
const named = {"[data-file-chosen]": Object.assign(el(), {hidden: true}), "[data-file-name]": el(),
  "[data-file-photo]": el(), "[data-file-reset]": el(), "[data-file-error]": Object.assign(el(), {hidden: true}),
  "[data-file-error-text]": el(), "[data-submit]": el(), "[data-submit-label]": el(), "[data-progress]": el(),
  "[data-upload-status]": el(), "[data-wait]": el(), "[data-wait-seconds]": el()};
const form = Object.assign(el({"data-max-bytes": "1000"}), {
  querySelector: sel => named[sel] || null,
  querySelectorAll: sel => (sel === 'input[type="file"]' ? [camera, pick] : [])});
const doc = {documentElement: el(), addEventListener() {}, querySelectorAll: () => [],
             querySelector: sel => (sel === "[data-upload-form]" ? form : null), createElement: () => el()};
const URLs = {createObjectURL: () => "blob:fake", revokeObjectURL() {}};
new Function("window", "document", "URL", src)(
  {addEventListener() {}, localStorage: {getItem: () => null}, URL: URLs}, doc, URLs);
const chosen = named["[data-file-chosen]"], photo = named["[data-file-photo]"];
const view = () => ({ready: form.getAttribute("data-ready"), chosen: !chosen.hidden, photos: photo.children.length,
                     name: named["[data-file-name]"].textContent, error: !named["[data-file-error]"].hidden});
const out = {initial: view()};
camera.files = [{name: "IMG_0001.jpg", size: 10, type: "image/jpeg"}];
camera.fire("change");
out.picked = Object.assign(view(), {focus: state.focus === named["[data-submit]"]});
named["[data-file-reset]"].fire("click");
out.again = Object.assign(view(), {focus: state.focus === camera, files: camera.files});
pick.files = [{name: "big.jpg", size: 5000, type: "image/jpeg"}];
pick.fire("change");
out.big = Object.assign(view(), {files: pick.files});
console.log(JSON.stringify(out));
"""
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="沒有 node，略過前端流程測試")
def test_choosing_a_photo_swaps_the_buttons_for_a_preview_in_app_js():
    """一次一個主要按鈕(使用者 11/4):選好照片 → data-ready="true"(app.css 收起兩顆按鈕,改顯示類型與「開始辨識」)、
    縮圖、焦點到「開始辨識」;「換一張」清掉檔案、收起縮圖、回到兩顆按鈕;超過大小的檔不算選好,寫出錯誤。"""
    script = _PICK_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    r = json.loads(subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8",
                                  timeout=30, check=True).stdout)
    assert r["initial"] == {"ready": "false", "chosen": False, "photos": 0, "name": "", "error": False}
    assert r["picked"] == {"ready": "true", "chosen": True, "photos": 1, "name": "IMG_0001.jpg", "error": False,
                           "focus": True}
    assert r["again"] == {"ready": "false", "chosen": False, "photos": 0, "name": "IMG_0001.jpg", "error": False,
                          "focus": True, "files": None}
    assert r["big"]["ready"] == "false" and r["big"]["error"] and r["big"]["files"] is None


def test_upload_steps_are_styled_by_the_ready_state():
    """app.css:還沒選照片時類型與「開始辨識」收起來,選好後收起兩顆按鈕(和 app.js 的 data-ready 對得上)。"""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert '[data-ready="false"] .upload__row' in css and '[data-ready="true"] .drop__btns { display: none; }' in css


def test_category_choice_reaches_pipeline_and_is_stored(cfg, client, analyzer, upload):
    """(還開著舊版首頁的表單才會送大類,伺服器照舊收)敏感大類要求本機;清單上的名稱換成清單的類型提示(水電瓦斯費 → 帳單),不在這一類清單上的(財產資產 + 藥袋)
    當作沒選;文件歸使用者選的大類,沒選就依類型(MockAnalyzer 一律讀成發票)。"""
    store = Store(cfg.paths.db_path)
    cases = [("醫療與保險", "公文"), ("身分證明", "不確定"), ("生活契約", "水電瓦斯費"), ("財產資產", "藥袋"),
             (None, "帳單"), ("不確定", "不確定")]
    ids = [_doc_id(upload(client, category=category, doc_type=kind)) for category, kind in cases]
    assert analyzer.hints == ["公文", None, "帳單", None, "帳單", None]
    assert analyzer.local_only == [True, True, False, False, False, False]
    docs = [store.get_document(i) for i in ids]
    assert [d["category"] for d in docs] == ["醫療與保險", "身分證明", "生活契約", "財產資產", "財產資產", "財產資產"]
    assert [d["doc_label"] for d in docs] == ["公文", None, "水電瓦斯費", None, None, None]
    assert [d["doc_type_hint"] for d in docs] == ["公文", None, "帳單", None, "帳單", None]


@pytest.mark.parametrize("value", ["亂填的大類", "未分類", "醫療與保險 ", "其他", "<script>", ""])
def test_unknown_category_counts_as_not_chosen(cfg, client, analyzer, upload, value):
    """伺服器只收四大類的原字串,其他(含「未分類」、多了空白)當作沒選:分流照類型提示、文件依類型歸類。"""
    r = upload(client, category=value, doc_type="帳單")
    assert r.status_code == 303
    assert analyzer.hints == ["帳單"] and analyzer.local_only == [False]
    assert Store(cfg.paths.db_path).get_document(_doc_id(r))["category"] == "財產資產"


def test_extra_form_fields_cannot_turn_off_local_only(client, analyzer, upload):
    """注入:多塞 local_only、tier、provider 之類的欄位沒有作用;要不要留在本機只看使用者選的大類與類型。"""
    r = upload(client, category="醫療與保險", doc_type="帳單",
               extra={"local_only": "false", "tier": "auto", "kind": "payment", "provider": "workers_ai"})
    assert r.status_code == 303 and analyzer.local_only == [True]


def test_error_page_keeps_the_chosen_type(client):
    """沒選檔就按送出:錯誤頁留住剛才選的類型,長輩不必重選;只留類型選項收得下的值。
    舊版的「發票」是現在的「發票／收據」;舊版首頁送來的大類與名稱(生活契約 + 水電瓦斯費)不是類型,回到「不確定」。"""
    r = client.post("/upload", data={"doc_type": "帳單"})
    assert r.status_code == 400 and "請先拍照" in r.text
    assert re.search(r'name="doc_type" value="帳單" checked', r.text)
    r = client.post("/upload", data={"doc_type": "發票"})
    assert re.search(r'name="doc_type" value="收據" checked', r.text)
    r = client.post("/upload", data={"category": "生活契約", "doc_type": "水電瓦斯費"})
    assert re.search(r'name="doc_type" value="不確定" checked', r.text) and 'name="category"' not in r.text


_LETTER_REPLY = {"doc_type": "公文", "date": "2026-09-28", "vendor": "範例區公所", "confidence": 0.9,
                 "fields": {"subject": "請補送敬老卡申請文件"}, "plain_summary": "區公所請您補送文件。"}


class _FakeOllama:
    """假的 Ollama client(不需 Ollama 服務):記下每次請求,回一份合成讀值。"""

    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(_LETTER_REPLY, ensure_ascii=False)))


def _cloud_client(tmp_path, monkeypatch, form_client):
    """雲端模式的 (client, 送到雲端的請求, 本機 Ollama 收到的請求, 資料庫)。

    兩端都是真的 provider:本機 Ollama 用假 client,Workers AI 用 httpx.MockTransport,不連網。
    """
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct-0000")
    monkeypatch.setenv("CF_API_TOKEN", "tok-SYNTHETIC")
    cfg = _cloud_cfg(tmp_path)
    sent: list[httpx.Request] = []

    def cloudflare(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        reply = json.dumps(_LETTER_REPLY, ensure_ascii=False)
        return httpx.Response(200, json={"result": {"choices": [{"message": {"content": reply}}]},
                                         "success": True, "errors": [], "messages": []})

    ollama = _FakeOllama()
    workers = WorkersAIAnalyzer(cfg, client=httpx.Client(transport=httpx.MockTransport(cloudflare)))
    client = form_client(create_app(cfg, analyzer=RoutingAnalyzer(OllamaAnalyzer(cfg, client=ollama), workers, cfg)))
    return client, sent, ollama.calls, Store(cfg.paths.db_path)


def _cloud_prompt(request: httpx.Request) -> str:
    parts = json.loads(request.content)["messages"][0]["content"]
    return next(p["text"] for p in parts if p["type"] == "text")


def test_cloud_mode_routes_by_type_end_to_end(tmp_path, monkeypatch, form_client, upload):
    """驗收(雲端模式,首頁只選類型):帳單、公文 → 雲端、用各自的提示詞;藥袋、發票／收據(收據)、舊版的「發票」、
    不確定 → 本機,類型提示照樣交給本機挑提示詞(收據與發票共用發票版)。"""
    client, sent, local, _ = _cloud_client(tmp_path, monkeypatch, form_client)
    for choice in ("帳單", "公文"):
        assert upload(client, doc_type=choice).status_code == 303
    assert [_cloud_prompt(r) for r in sent] == [get_prompt("帳單")[0], get_prompt("公文")[0]] and local == []
    for choice in ("藥袋", "收據", "發票", "不確定"):
        assert upload(client, doc_type=choice).status_code == 303
    assert len(sent) == 2
    assert [c["messages"][0]["content"] for c in local] == [
        get_prompt("藥袋")[0], get_prompt("收據")[0], get_prompt("收據")[0], get_prompt(None)[0]]


def test_cloud_mode_routes_by_category_end_to_end(tmp_path, monkeypatch, form_client, upload):
    """驗收(雲端模式,還開著舊版首頁送來的大類):醫療與保險 + 公文 → 本機、用公文的提示詞;
    生活契約 + 水電瓦斯費 → 雲端、用帳單的提示詞;沒選大類照類型分流。"""
    client, sent, local, _ = _cloud_client(tmp_path, monkeypatch, form_client)

    assert upload(client, category="醫療與保險", doc_type="公文").status_code == 303
    assert sent == [] and len(local) == 1                     # 雲端一次都沒被叫
    assert local[0]["messages"][0]["content"] == get_prompt("公文")[0]

    assert upload(client, category="生活契約", doc_type="水電瓦斯費").status_code == 303
    assert len(sent) == 1 and _cloud_prompt(sent[0]) == get_prompt("帳單")[0] and len(local) == 1

    assert upload(client, doc_type="帳單").status_code == 303          # 沒選大類:照類型提示分流
    assert upload(client, doc_type="不確定").status_code == 303        # 沒選類型:本機、通用提示詞
    assert upload(client, category="身分證明").status_code == 303
    assert len(sent) == 2 and len(local) == 3
    assert [c["messages"][0]["content"] for c in local[1:]] == [get_prompt(None)[0]] * 2


def test_cloud_mode_routes_catalog_items_end_to_end(tmp_path, monkeypatch, form_client, upload):
    """驗收(雲端模式,上傳選項清單):財產資產 + 稅單 → 雲端、用帳單的提示詞(財產資產不是敏感類);
    交給家人確認的名稱不論大類都只在本機、用通用提示詞(保單、存摺、身分證、租約),而且一律轉人工。"""
    client, sent, local, store = _cloud_client(tmp_path, monkeypatch, form_client)

    tax = _doc_id(upload(client, category="財產資產", doc_type="稅單"))
    assert len(sent) == 1 and _cloud_prompt(sent[0]) == get_prompt("帳單")[0] and local == []
    assert (store.get_document(tax)["doc_label"], store.get_document(tax)["doc_type_hint"]) == ("稅單", "帳單")

    manual = [("醫療與保險", "保單"), ("財產資產", "存摺"), ("身分證明", "身分證"), ("生活契約", "租約")]
    ids = [_doc_id(upload(client, category=category, doc_type=name)) for category, name in manual]
    assert len(sent) == 1 and len(local) == 4                 # 雲端沒有多叫
    assert [c["messages"][0]["content"] for c in local] == [get_prompt(None)[0]] * 4
    docs = [store.get_document(i) for i in ids]
    assert [(d["doc_label"], d["action"]) for d in docs] == [(name, "review") for _, name in manual]


# ---- 上傳選項清單(使用者 10/3):每類的常見文件,不能自動判讀的也列上去、交給家人複核 ------------------

def test_unreadable_item_is_local_and_waits_for_the_family(cfg, client, analyzer, upload):
    """(醫療與保險, 保單):只在本機、沒有類型提示;MockAnalyzer 讀成自評 0.92 的發票也轉人工;記下「保單」。"""
    doc = Store(cfg.paths.db_path).get_document(_doc_id(upload(client, category="醫療與保險", doc_type="保單")))
    assert analyzer.hints == [None] and analyzer.local_only == [True]
    assert (doc["doc_label"], doc["action"], doc["category"]) == ("保單", "review", "醫療與保險")
    assert doc["reason"] == "「保單」交給家人對照原件確認"


def test_identity_item_is_local_and_waits_for_the_family(cfg, client, analyzer, upload):
    """(身分證明, 身分證):只在本機、轉人工,記下「身分證」。"""
    doc = Store(cfg.paths.db_path).get_document(_doc_id(upload(client, category="身分證明", doc_type="身分證")))
    assert analyzer.hints == [None] and analyzer.local_only == [True]
    assert (doc["doc_label"], doc["action"], doc["category"]) == ("身分證", "review", "身分證明")


@pytest.mark.parametrize("category, value", [
    ("生活契約", "保單"),                       # 別類的名稱
    ("財產資產", "帳單"),                       # 類型名稱不是這一類清單上的名稱(稅單才是)
    ("醫療與保險", "保單 "),                    # 多了空白
    ("身分證明", "<script>alert(1)</script>"),
    ("不確定", "稅單"),                         # 沒選大類只收五種類型
    ("財產資產", ""),
])
def test_names_outside_the_list_count_as_not_chosen(cfg, client, analyzer, upload, category, value):
    """伺服器只收所選大類清單上的名稱(沒選大類只收五種類型),其他當作沒選:不記名稱、沒有類型提示、
    不因此轉人工;要不要本機只看大類(身分證明整類照舊轉人工)。"""
    r = upload(client, category=category, doc_type=value)
    assert r.status_code == 303
    doc = Store(cfg.paths.db_path).get_document(_doc_id(r))
    assert doc["doc_label"] is None and analyzer.hints == [None]
    assert analyzer.local_only == [category in SENSITIVE_CATEGORIES]
    assert doc["action"] == ("review" if category == "身分證明" else "archive")
