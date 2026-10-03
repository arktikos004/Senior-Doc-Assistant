"""上傳路由測試:大小/副檔名限制、檔名重產、大類與類型提示、隱私分流、轉到結果頁(不呼叫模型、不連網)。"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import web.app as web_app
from src.models import CATEGORIES, CATEGORY_DOCS, DOC_TYPES, SENSITIVE_CATEGORIES, ExtractionResult
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
            plain_summary="這是示範診所開的兩種藥,飯後吃。",
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
    for value in ("藥袋", "帳單", "公文", "發票", "收據"):
        assert f'value="{value}"' in html
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


def test_cloud_mode_says_sensitive_categories_stay_local(tmp_path):
    from fastapi.testclient import TestClient

    cfg = _cloud_cfg(tmp_path)
    html = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg))).get("/").text
    assert "醫療與保險、身分證明兩類只在這台電腦處理,不會送到雲端。" in html
    # 上傳區不加身分證明或「初賽還不能自動判讀」的說明(使用者 10/3);說明留在文件櫃
    assert "初賽還不能自動判讀" not in html and "data-kinds-none" not in html


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
    for choice in ("不確定", "藥袋", "帳單", "公文", "發票", "收據", "亂填的類型"):
        assert upload(client, doc_type=choice).status_code == 303
    assert analyzer.hints == [None, "藥袋", "帳單", "公文", "發票", "收據", None]


def _category_rows(html: str) -> dict[str, list[str]]:
    """第一列每個大類 → 選了它之後第二列留下的選項(data-kinds)。"""
    return {category: kinds.split()
            for category, kinds in re.findall(r'name="category" value="([^"]+)"[^>]*data-kinds="([^"]*)"', html)}


def test_every_form_value_reaches_pipeline_exactly(client, monkeypatch, upload):
    """隱私分流只認 DOC_TYPES、CATEGORIES 與清單上的原字串:畫面上每個大類留下的每個選項,送到 process_file
    都必須一字不差。沒選大類:五種類型當類型提示;選了大類:清單上的名稱當 label(類型提示由 Pipeline 依清單決定)。"""
    seen = []
    original = web_app.Pipeline.process_file

    def spy(self, path, doc_type_hint=None, *, category=None, label=None):
        seen.append((doc_type_hint, category, label))
        return original(self, path, doc_type_hint, category=category, label=label)

    monkeypatch.setattr(web_app.Pipeline, "process_file", spy)
    rows = _category_rows(client.get("/").text)
    assert list(rows) == ["不確定", *CATEGORIES]
    for category, values in rows.items():
        seen.clear()
        assert values[-1] == "不確定"                                   # 照樣稿:「不確定」在最後
        for value in values:
            assert upload(client, category=category, doc_type=value).status_code == 303
        if category == "不確定":
            assert seen == [(v, None, None) for v in values[:-1]] + [(None, None, None)]
            assert all(v in DOC_TYPES for v in values[:-1])
        else:
            assert seen == [(None, category, v) for v in values[:-1]] + [(None, category, None)]


def test_medication_hint_shows_disclaimer(client, upload):
    r = upload(client, doc_type="藥袋")
    assert "?" not in r.headers["location"]   # 提示存在資料庫,不靠網址參數
    html = client.get(r.headers["location"]).text
    assert "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。" in html
    # 重新整理(同一網址再開一次)聲明仍在
    assert "不提供醫療建議" in client.get(r.headers["location"]).text


def test_medication_result_renders_items(cfg, form_client, upload):
    client = form_client(create_app(cfg, analyzer=MedicationAnalyzer()))
    r = upload(client, doc_type="藥袋")
    html = client.get(r.headers["location"]).text
    assert "AI 白話解說" in html
    assert "這是示範診所開的兩種藥,飯後吃。" in html
    assert "示範藥甲" in html and "示範藥乙" in html
    assert "需要時" in html
    assert "不提供醫療建議" in html


def test_upload_filename_is_regenerated(cfg, client, upload):
    r = upload(client, name="我的 帳單(1).png")
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    doc = Store(cfg.paths.db_path).get_document(doc_id)
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}\.png", doc["source_file"])


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


# ---- 先選大類,再選這一類裡的哪一種 -----------------------------------------------------------

def _doc_id(response) -> int:
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_home_offers_category_first_then_kind(client):
    """第一列:不確定 + 四大類(各有圖示);兩列預設都是不確定。每個大類帶著第二列要留下的選項(data-kinds:
    這一類清單上的名稱,照 CATEGORY_DOCS 的順序,最後是「不確定」)與標題;沒選大類留下五種類型。
    第二列沒有 JS 時只看得到五種類型 + 不確定,各類的常見文件先藏著;上傳區不寫任何說明(使用者 10/3)。"""
    html = client.get("/").text
    assert "這是哪一類?" in html and "是哪一種文件?" in html
    assert re.search(r'name="category" value="不確定" checked', html)
    assert re.search(r'name="doc_type" value="不確定" checked data-kinds-unsure', html)
    first_row = html.split("這是哪一類?")[1].split("</fieldset>")[0]
    assert first_row.count("<svg") == 5
    rows = _category_rows(html)
    legends = dict(re.findall(r'name="category" value="([^"]+)"[^>]*data-kinds-legend="([^"]*)"', html))
    assert rows["不確定"] == ["藥袋", "帳單", "發票", "收據", "公文", "不確定"] and legends["不確定"] == "是哪一種文件?"
    for category, docs in CATEGORY_DOCS.items():
        assert rows[category] == [name for name, _ in docs] + ["不確定"]
        assert legends[category] == f"{category}裡的哪一種?"
    chips = re.findall(r'<label class="chip"( hidden)?><input class="chip__input" type="radio" name="doc_type" '
                       r'value="([^"]+)"', html)
    assert [value for hidden, value in chips if not hidden] == rows["不確定"]    # 沒有 JS 時看得到的
    kinds = [value for _, value in chips]
    assert len(kinds) == len(set(kinds)) and set(kinds) == {v for values in rows.values() for v in values}
    for values in rows.values():                          # app.js 依大類篩出來的順序和清單一樣
        assert [k for k in kinds if k in values] == values
    upload_area = html.split('<section class="panel upload"')[1].split("</section>")[0]
    assert "upload__note" not in upload_area and "初賽還不能自動判讀" not in upload_area   # 本機模式:沒有說明


# 用假的 DOM 跑真的 app.js:兩列的選項照伺服器畫出來的(__DATA__),換大類後看第二列留下哪些、選中哪一個
_KINDS_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8"), data = __DATA__;
function el(attrs = {}) {
  return {attrs, value: attrs.value, hidden: false, checked: false, textContent: "", listeners: {},
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type) { (this.listeners[type] || []).forEach(fn => fn({type, target: this, preventDefault() {}})); },
    focus() {}};
}
function radios(list, checked) {   // 同一組 radio:選了一個,其他自動取消(和瀏覽器一樣)
  list.forEach((node, i) => {
    let on = checked[i];
    Object.defineProperty(node, "checked", {get: () => on, set: v => {
      if (v) list.forEach(other => { if (other !== node) other.off(); });
      on = Boolean(v);
    }});
    node.off = () => { on = false; };
  });
  return list;
}
const categories = radios(data.categories.map(c => el({value: c.value, "data-kinds": c.kinds,
  "data-kinds-legend": c.legend})), data.categories.map(c => c.checked));
const kinds = radios(data.kinds.map(k => {
  const chip = Object.assign(el(), {hidden: k.hidden});
  return Object.assign(el(Object.assign({value: k.value}, k.unsure ? {"data-kinds-unsure": ""} : {})),
                       {closest: () => chip});
}), data.kinds.map(k => k.checked));
const title = el(), named = {"[data-kinds-group]": el(), "[data-kinds-title]": title,
  "[data-kinds-unsure]": kinds.find(k => "data-kinds-unsure" in k.attrs), "[data-file-error]": Object.assign(el(), {hidden: true})};
const form = Object.assign(el(), {
  querySelector: sel => sel === 'input[name="category"]:checked' ? categories.find(c => c.checked) || null
    : sel === 'input[name="doc_type"]:checked' ? kinds.find(k => k.checked) || null : named[sel] || el(),
  querySelectorAll: sel => ({'input[type="file"]': [el()], 'input[name="doc_type"]': kinds,
                             'input[name="category"]': categories})[sel] || []});
const doc = {documentElement: el(), addEventListener() {}, querySelectorAll: () => [],
             querySelector: sel => sel === "[data-upload-form]" ? form : null};
new Function("window", "document", src)({addEventListener() {}, localStorage: {getItem: () => null}}, doc);
const pick = (list, value) => { const node = list.find(x => x.value === value); node.checked = true; return node; };
const state = () => ({title: title.textContent, shown: kinds.filter(k => !k.closest().hidden).map(k => k.value),
                      checked: (kinds.find(k => k.checked) || {}).value});
const out = {initial: state()};
pick(categories, "醫療與保險").fire("change"); out.health = state();
pick(kinds, "保單"); pick(categories, "生活契約").fire("change"); out.living = state();
pick(kinds, "公文"); pick(categories, "財產資產").fire("change"); out.assets = state();
pick(categories, "不確定").fire("change"); out.none = state();
pick(categories, "身分證明").fire("change"); out.identity = state();
console.log(JSON.stringify(out));
"""
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_kinds_follow_the_chosen_category_in_app_js(client):
    """app.js 依第一列只留那一類清單上的名稱 + 不確定,標題跟著換;被藏起來的選項正被選著就改回「不確定」,
    兩類都有的(公文)留著;沒選大類回到五種類型。"""
    html = client.get("/").text
    data = {
        "categories": [{"value": v, "checked": bool(c), "kinds": k, "legend": legend} for v, c, k, legend in re.findall(
            r'name="category" value="([^"]+)"( checked)? data-kinds="([^"]*)" data-kinds-legend="([^"]*)"', html)],
        "kinds": [{"value": v, "hidden": bool(h), "checked": bool(c), "unsure": bool(u)} for h, v, c, u in re.findall(
            r'<label class="chip"( hidden)?><input class="chip__input" type="radio" name="doc_type" value="([^"]+)"'
            r'( checked)?( data-kinds-unsure)?>', html)],
    }
    script = (_KINDS_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
              .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    r = json.loads(subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)
    five = ["藥袋", "帳單", "發票", "收據", "公文", "不確定"]
    assert r["initial"] == {"title": "是哪一種文件?", "shown": five, "checked": "不確定"}
    assert r["health"] == {"title": "醫療與保險裡的哪一種?", "checked": "不確定",
                           "shown": ["藥袋", "醫療收據", "保單", "診斷證明", "檢驗報告", "公文", "不確定"]}
    assert r["living"] == {"title": "生活契約裡的哪一種?", "checked": "不確定",        # 保單被藏起來 → 不確定
                           "shown": ["水電瓦斯費", "電信費", "管理費", "租約", "公文", "不確定"]}
    assert r["assets"] == {"title": "財產資產裡的哪一種?", "checked": "公文",           # 兩類都有的公文留著
                           "shown": ["發票", "收據", "稅單", "存摺", "房地權狀", "公文", "不確定"]}
    assert r["none"] == {"title": "是哪一種文件?", "shown": five, "checked": "公文"}
    assert r["identity"] == {"title": "身分證明裡的哪一種?", "checked": "不確定",
                             "shown": ["身分證", "健保卡", "戶口名簿", "駕照", "護照", "印鑑證明", "不確定"]}


def test_category_choice_reaches_pipeline_and_is_stored(cfg, client, analyzer, upload):
    """敏感大類要求本機;清單上的名稱換成清單的類型提示(水電瓦斯費 → 帳單),不在這一類清單上的(財產資產 + 藥袋)
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


def test_error_page_keeps_both_choices(client):
    """沒選檔就按送出:錯誤頁保留剛才選的大類與這一類的名稱,長輩不必重選;沒選大類時保留類型。
    第二列只留伺服器收得下的值:不在所選大類清單上的(生活契約 + 藥袋)回到「不確定」。"""
    r = client.post("/upload", data={"category": "生活契約", "doc_type": "水電瓦斯費"})
    assert r.status_code == 400 and "請先拍照" in r.text
    assert re.search(r'name="category" value="生活契約" checked', r.text)
    assert re.search(r'name="doc_type" value="水電瓦斯費" checked', r.text)
    assert not re.search(r'value="不確定" checked', r.text)
    r = client.post("/upload", data={"doc_type": "帳單"})
    assert re.search(r'name="doc_type" value="帳單" checked', r.text)
    r = client.post("/upload", data={"category": "生活契約", "doc_type": "藥袋"})
    assert re.search(r'name="category" value="生活契約" checked', r.text)
    assert re.search(r'name="doc_type" value="不確定" checked', r.text) and "藥袋\" checked" not in r.text


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


def test_cloud_mode_routes_by_category_end_to_end(tmp_path, monkeypatch, form_client, upload):
    """驗收(雲端模式):醫療與保險 + 公文 → 本機、用公文的提示詞;生活契約 + 水電瓦斯費 → 雲端、用帳單的提示詞;
    沒選大類照舊分流。"""
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
    初賽不能自動判讀的名稱不論大類都只在本機、用通用提示詞(保單、存摺、身分證、租約),而且一律轉人工。"""
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


# ---- 上傳選項清單(使用者 10/3):每類的常見文件,不能自動判讀的先列上去、交給家人複核 ------------------

def test_unreadable_item_is_local_and_waits_for_the_family(cfg, client, analyzer, upload):
    """(醫療與保險, 保單):只在本機、沒有類型提示;MockAnalyzer 讀成自評 0.92 的發票也轉人工;記下「保單」。"""
    doc = Store(cfg.paths.db_path).get_document(_doc_id(upload(client, category="醫療與保險", doc_type="保單")))
    assert analyzer.hints == [None] and analyzer.local_only == [True]
    assert (doc["doc_label"], doc["action"], doc["category"]) == ("保單", "review", "醫療與保險")
    assert doc["reason"] == "「保單」初賽還不能自動判讀,需人工確認"


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
