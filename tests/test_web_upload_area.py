"""首頁上傳區(使用者 11/4 的表單規範第 3、12 點與上傳流程第 2 點):虛線框與框內的灰字、拖進來放開、
選好照片後的拍照檢查(偵測到才提示)、按「開始辨識」後的真實上傳進度。

前端流程用假的 DOM 跑真的 app.js(同 tests/test_web_upload.py 的寫法),不開瀏覽器、不連網;
伺服器那一半(POST /upload/check)在 tests/test_photo_check.py。
"""
import json
import re
import shutil
import subprocess

import pytest

import web.app as web_app
from src.config import AppConfig
from src.photo_check import CHECK_EDGE, HINT_BLUR
from web.render import STATIC_DIR

_NODE = shutil.which("node")
_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _upload_area(html: str) -> str:
    return html.split('<section class="panel upload"')[1].split("</section>")[0]


# ---- 樣板與樣式 -----------------------------------------------------------------------------------

def test_upload_area_is_a_dashed_drop_zone_with_the_limits_written_inside(client):
    area = _upload_area(client.get("/").text)
    zone = area.split("data-drop-zone>")[1].split('<div class="upload__row">')[0]
    # 框內常駐一行灰字,只寫填寫要求(第 12 點);在兩顆按鈕前面,沒選照片時也看得到
    assert '<p class="ff__help" id="file-help">照片或 PDF，15MB 以內</p>' in zone
    assert zone.index('id="file-help"') < zone.index("拍照讀文件")
    assert 'id="file-error"' in zone and "data-file-chosen" in zone and ">換一張</button>" in zone
    # 兩個入口都念得到這行灰字
    assert area.count('aria-describedby="file-help"') == 2
    # 拍照檢查的位置是空的:沒偵測到就什麼都不寫
    assert re.search(r'<p class="photo-hint" role="status" data-photo-hint></p>', area)
    assert 'data-check-url="/upload/check"' in area
    # 沒有 JS 時照舊是一般表單送出
    assert 'method="post" action="/upload" enctype="multipart/form-data"' in area


def test_upload_error_is_read_together_with_the_limits(client):
    r = client.post("/upload", data={"doc_type": "不確定"})
    assert r.status_code == 400
    assert 'aria-describedby="file-help file-error" aria-invalid="true"' in _upload_area(r.text)


def test_upload_progress_bar_reports_real_numbers(client):
    area = _upload_area(client.get("/").text)
    assert re.search(r'role="progressbar" aria-label="上傳進度" aria-valuemin="0" aria-valuemax="100" '
                     r'aria-valuenow="0" hidden><span class="progress__bar" data-progress-bar>', area)
    assert "已傳 <span data-wait-percent>0</span>%" in area


def test_drop_zone_styles_live_in_the_upload_section():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    section = css.split("/* ---- 上傳與拍照檢查 ---- */")[1].split("/* (上傳與拍照檢查的規則寫在這一行上面) */")[0]
    assert "border: 2px dashed var(--control)" in section            # 虛線框(v5,11/6:2px)
    drag = section.split('.dropzone[data-drag="true"] {')[1].split("}")[0]   # 拖進來時框要變:深墨實線
    assert "border-style: solid" in drag and "border-color: var(--ink)" in drag
    assert ".photo-hint" in section and "var(--amber)" in section      # 提示是要注意的琥珀色
    assert ".upload .progress__bar { width: 0;" in section             # 進度條照實際傳了多少長


def test_the_two_upload_buttons_are_the_same_width():
    """「拍照讀文件」「選照片」一樣寬(v5,11/6):主要的靠黃色分,不靠大小;手機上對半。"""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    t1 = css.split("/* ---- T1 首頁與要記得的事 ---- */")[1].split("/* (T1 首頁與要記得的事的規則寫在這一行上面) */")[0]
    assert ".drop__btns .btn { width: 12rem; }" in t1
    phone = t1.split("@media (max-width: 47.99rem) {")[1].split("\n}\n")[0]
    assert ".drop__btns .btn { width: auto; flex: 1 1 0;" in phone


def test_app_js_matches_the_server_limits():
    """app.js 自己擋的地方要和伺服器一致:拍照檢查的縮圖大小(門檻是照這個量的)、表單上限、
    拖進來的檔案格式與錯誤文字、太大的錯誤文字。"""
    assert int(re.search(r"var CHECK_EDGE = (\d+)", _JS).group(1)) == CHECK_EDGE
    max_bytes = re.search(r"CHECK_MAX_BYTES = (\d+) \* (\d+)", _JS)
    # 照片加上 CSRF 欄位與 multipart 的表頭還要收得進表單上限,超過就整份被 413 擋掉
    assert int(max_bytes.group(1)) * int(max_bytes.group(2)) + 16 * 1024 <= web_app.MAX_FORM_BYTES
    accepted = re.compile(re.search(r"var ACCEPTED_NAME = /(.+)/i;", _JS).group(1), re.I)
    assert all(accepted.search("photo" + ext) for ext in AppConfig.supported_extensions)
    assert not any(accepted.search(name) for name in ("notes.txt", "run.exe", "page.html", "photo.heic", "noext"))
    assert f'var MSG_BAD_EXT = "{web_app.MSG_BAD_EXT}";' in _JS
    assert f'showError("{web_app.MSG_TOO_LARGE}")' in _JS


# ---- 前端流程(假的 DOM) ---------------------------------------------------------------------------

_HARNESS = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

function boot(options = {}) {
  const state = {focus: null, assigned: null, written: null, fetches: [], xhrs: [], blobType: null};
  function el(attrs = {}) {
    return {attrs, hidden: false, disabled: false, textContent: "", listeners: {}, children: [], style: {},
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
      hasAttribute(k) { return k in this.attrs; },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      fire(type, extra = {}) {
        const ev = Object.assign({type, target: this, defaultPrevented: false,
                                  preventDefault() { this.defaultPrevented = true; }}, extra);
        (this.listeners[type] || []).forEach(fn => fn(ev));
        return ev;
      },
      querySelector(sel) { return sel === "img" ? this.children[0] || null : null; },
      appendChild(child) { this.children.push(child); child.remove = () => { this.children = []; }; },
      focus() { state.focus = this; }};
  }
  function fileInput() {      // 和瀏覽器一樣:value 設成空字串就清掉選好的檔;舊瀏覽器不能指定 files
    const input = el({type: "file"});
    let files = null;
    Object.defineProperty(input, "files", {get: () => files, set: v => { if (!options.readonlyFiles) files = v; }});
    Object.defineProperty(input, "value", {get: () => (files ? "C:\\fakepath\\x" : ""), set: v => { if (v === "") files = null; }});
    return input;
  }
  const camera = fileInput(), pick = fileInput();
  const zone = el(), inside = el(), outside = el();
  zone.contains = node => node === zone || node === inside;
  const named = {"[data-file-chosen]": Object.assign(el(), {hidden: true}), "[data-file-name]": el(),
    "[data-file-photo]": el(), "[data-file-reset]": el(), "[data-file-error]": Object.assign(el(), {hidden: true}),
    "[data-file-error-text]": el(), "[data-submit]": el(), "[data-submit-label]": Object.assign(el(), {textContent: "開始辨識"}),
    "[data-progress]": Object.assign(el(), {hidden: true}), "[data-progress-bar]": el(), "[data-upload-status]": el(),
    "[data-wait]": Object.assign(el(), {hidden: true}), "[data-wait-seconds]": el(), "[data-wait-percent]": el(),
    "[data-drop-zone]": zone, "[data-photo-hint]": el(), 'input[name="csrf_token"]': {name: "csrf_token", value: "tok"}};
  const form = Object.assign(el({"data-max-bytes": "1000", "data-check-url": "/upload/check", action: "/upload"}), {
    querySelector: sel => named[sel] || null,
    querySelectorAll: sel => (sel === 'input[type="file"]' ? [camera, pick] : [])});
  const doc = {documentElement: el(), addEventListener() {}, querySelectorAll: () => [],
    querySelector: sel => (sel === "[data-upload-form]" ? form : null),
    createElement(tag) {
      if (tag === "canvas") {
        const canvas = el();
        canvas.getContext = () => ({drawImage() { state.drawn = [canvas.width, canvas.height]; }});
        canvas.toBlob = (done, type) => { state.blobType = type; done({size: 2000}); };
        return canvas;
      }
      return Object.assign(el(), {naturalWidth: 3000, naturalHeight: 4000});
    },
    open() { state.opened = true; }, write(html) { state.written = html; }, close() {}};
  class FakeFormData {
    constructor(of) { this.of = of === form ? "form" : null; this.parts = []; }
    append(k, v, name) { this.parts.push([k, typeof v === "string" ? v : "blob", name || null]); }
  }
  class FakeXHR {
    constructor() { this.listeners = {}; this.upload = el(); this.responseURL = ""; this.responseText = ""; state.xhrs.push(this); }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type) { (this.listeners[type] || []).forEach(fn => fn({type})); }
    open(method, url) { this.method = method; this.url = url; }
    send(body) { this.body = body; }
  }
  const win = Object.assign(el(), {
    localStorage: {getItem: () => null}, location: {assign(url) { state.assigned = url; }},
    FormData: FakeFormData, setInterval: () => 1, clearInterval() {},
    fetch(url, init) {
      state.fetches.push({url, method: init.method, parts: init.body.parts, credentials: init.credentials});
      if (options.fetchFails) return Promise.reject(new Error("offline"));
      return Promise.resolve({ok: true, json: () => Promise.resolve(options.answer || {hint: null})});
    }});
  if (!options.noXHR) win.XMLHttpRequest = FakeXHR;
  const URLs = {createObjectURL: () => "blob:fake", revokeObjectURL() {}};
  win.URL = URLs;
  new Function("window", "document", "URL", src)(win, doc, URLs);
  const view = () => ({ready: form.getAttribute("data-ready"), chosen: !named["[data-file-chosen]"].hidden,
    name: named["[data-file-name]"].textContent, photos: named["[data-file-photo]"].children.length,
    error: named["[data-file-error]"].hidden ? null : named["[data-file-error-text]"].textContent,
    hint: named["[data-photo-hint]"].textContent, drag: zone.getAttribute("data-drag"),
    submitDisabled: named["[data-submit]"].disabled, resetDisabled: named["[data-file-reset]"].disabled,
    label: named["[data-submit-label]"].textContent, status: named["[data-upload-status]"].textContent,
    progressShown: !named["[data-progress]"].hidden, bar: named["[data-progress-bar]"].style.width || "",
    valuenow: named["[data-progress]"].getAttribute("aria-valuenow"), percent: named["[data-wait-percent]"].textContent,
    waitShown: !named["[data-wait]"].hidden, focusSubmit: state.focus === named["[data-submit]"]});
  const files = list => Object.assign(list.slice(), {item: i => list[i]});
  const dt = list => ({types: ["Files"], files: list ? files(list) : files([]), dropEffect: "none"});
  return {state, camera, pick, zone, inside, outside, win, form, named, view, dt, files};
}

const JPG = {name: "IMG_0001.jpg", size: 10, type: "image/jpeg"};
const out = {};

async function main() {
  // 拖進框:框變色;在框外不收(也不讓瀏覽器打開照片);放開的效果和按「選照片」一樣
  {
    const t = boot();
    t.zone.fire("dragenter", {dataTransfer: t.dt()});
    const over = t.win.fire("dragover", {target: t.inside, dataTransfer: t.dt()});
    const overOutside = t.win.fire("dragover", {target: t.outside, dataTransfer: t.dt()});
    out.dragIn = {drag: t.view().drag, prevented: over.defaultPrevented, effect: over.dataTransfer.dropEffect,
                  outsidePrevented: overOutside.defaultPrevented, outsideEffect: overOutside.dataTransfer.dropEffect};
    t.zone.fire("dragenter", {dataTransfer: t.dt()});    // 經過框裡的元素:進一層、出一層
    t.zone.fire("dragleave", {dataTransfer: t.dt()});
    out.dragInner = t.view().drag;
    t.zone.fire("dragleave", {dataTransfer: t.dt()});
    out.dragOut = t.view().drag;
    t.zone.fire("dragenter", {dataTransfer: t.dt()});
    const list = t.dt([JPG]);
    const drop = t.win.fire("drop", {target: t.inside, dataTransfer: list});
    out.dropped = Object.assign(t.view(), {prevented: drop.defaultPrevented, sameFile: t.pick.files[0] === JPG,
                                           cameraCleared: t.camera.files === null});
  }
  // 拖錯了:照實說,原本選好的照片不動;放在框外什麼都不做
  {
    const t = boot();
    t.camera.files = [JPG];
    t.camera.fire("change");
    t.win.fire("drop", {target: t.inside, dataTransfer: t.dt([JPG, Object.assign({}, JPG, {name: "b.jpg"})])});
    out.dropTwo = t.view();
    t.win.fire("drop", {target: t.inside, dataTransfer: t.dt([{name: "notes.txt", size: 5, type: "text/plain"}])});
    out.dropText = t.view();
    const elsewhere = t.win.fire("drop", {target: t.outside, dataTransfer: t.dt([{name: "x.png", size: 5, type: "image/png"}])});
    out.dropOutside = Object.assign(t.view(), {prevented: elsewhere.defaultPrevented, kept: t.camera.files[0] === JPG});
    t.win.fire("drop", {target: t.inside, dataTransfer: t.dt([{name: "big.jpg", size: 5000, type: "image/jpeg"}])});
    out.dropBig = t.view();
  }
  {
    const t = boot({readonlyFiles: true});
    t.win.fire("drop", {target: t.inside, dataTransfer: t.dt([JPG])});
    out.dropOld = t.view();
  }
  // 拍照檢查:縮圖出來才問;偵測到才寫;換一張就清掉,還沒回來的答案不算數
  {
    const t = boot({answer: {hint: __HINT__}});
    t.camera.files = [JPG];
    t.camera.fire("change");
    out.beforeLoad = {fetches: t.state.fetches.length, hint: t.view().hint};
    t.named["[data-file-photo]"].children[0].fire("load");
    await tick(); await tick();
    out.checked = Object.assign(t.view(), {fetch: t.state.fetches[0], drawn: t.state.drawn, blobType: t.state.blobType});
    t.named["[data-file-reset]"].fire("click");
    out.checkReset = t.view();
  }
  {
    const t = boot({answer: {hint: __HINT__}});
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.named["[data-file-photo]"].children[0].fire("load");
    t.named["[data-file-reset]"].fire("click");        // 答案回來之前就換一張
    await tick(); await tick();
    out.checkStale = t.view();
  }
  {
    const t = boot({answer: {hint: null}});
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.named["[data-file-photo]"].children[0].fire("load");
    await tick(); await tick();
    out.checkClean = t.view();
  }
  {
    const t = boot({fetchFails: true});
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.named["[data-file-photo]"].children[0].fire("load");
    await tick(); await tick();
    out.checkOffline = t.view();
  }
  {
    const t = boot({answer: {hint: __HINT__}});
    t.pick.files = [{name: "bill.pdf", size: 10, type: "application/pdf"}];
    t.pick.fire("change");
    out.checkPdf = {photos: t.view().photos, fetches: t.state.fetches.length};
  }
  // 開始辨識:真的上傳進度,傳完照伺服器的轉址走
  {
    const t = boot({answer: {hint: __HINT__}});
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.named["[data-file-photo]"].children[0].fire("load");
    await tick(); await tick();
    const submit = t.form.fire("submit");
    const xhr = t.state.xhrs[0];
    out.sending = Object.assign(t.view(), {prevented: submit.defaultPrevented, method: xhr.method, url: xhr.url,
                                           body: xhr.body.of});
    xhr.upload.fire("progress", {lengthComputable: true, loaded: 45, total: 100});
    out.progress = t.view();
    const again = t.form.fire("submit");
    out.again = {prevented: again.defaultPrevented, xhrs: t.state.xhrs.length};
    xhr.upload.fire("load");
    out.uploaded = t.view();
    xhr.responseURL = "http://127.0.0.1:8000/upload/progress/abc";
    xhr.fire("load");
    out.redirect = {assigned: t.state.assigned, written: t.state.written};
  }
  {
    const t = boot();
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.form.fire("submit");
    const xhr = t.state.xhrs[0];
    xhr.responseURL = "http://127.0.0.1:8000/upload";
    xhr.responseText = "<!doctype html><p>檔案超過 15MB。</p>";
    xhr.fire("load");
    out.errorPage = {assigned: t.state.assigned, written: t.state.written, opened: Boolean(t.state.opened)};
  }
  {
    const t = boot();
    t.pick.files = [JPG];
    t.pick.fire("change");
    t.form.fire("submit");
    t.state.xhrs[0].fire("error");
    out.network = t.view();
  }
  {
    const t = boot({noXHR: true});
    t.pick.files = [JPG];
    t.pick.fire("change");
    const submit = t.form.fire("submit");
    out.fallback = Object.assign(t.view(), {prevented: submit.defaultPrevented});
  }
  {
    const t = boot();
    const submit = t.form.fire("submit");
    out.empty = Object.assign(t.view(), {prevented: submit.defaultPrevented, xhrs: t.state.xhrs.length});
  }
  console.log(JSON.stringify(out));
}
main();
"""


@pytest.fixture(scope="module")
def flows():
    if _NODE is None:
        pytest.skip("沒有 node，略過前端流程測試")
    script = (_HARNESS.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
              .replace("__HINT__", json.dumps(HINT_BLUR, ensure_ascii=False)))
    run = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(run.stdout)


def test_dragging_a_photo_over_the_box_changes_the_box(flows):
    assert flows["dragIn"] == {"drag": "true", "prevented": True, "effect": "copy",
                               "outsidePrevented": True, "outsideEffect": "none"}
    assert flows["dragInner"] == "true"           # 經過框裡的元素不會一閃一閃
    assert flows["dragOut"] is None               # 拖出框外就恢復


def test_dropping_a_photo_works_like_choosing_one(flows):
    dropped = flows["dropped"]
    assert dropped["prevented"] and dropped["sameFile"] and dropped["cameraCleared"]
    assert (dropped["ready"], dropped["chosen"], dropped["name"], dropped["photos"]) == ("true", True, "IMG_0001.jpg", 1)
    assert dropped["drag"] is None and dropped["error"] is None and dropped["focusSubmit"]


def test_wrong_drops_say_what_to_do_and_keep_the_chosen_photo(flows):
    assert flows["dropTwo"]["error"] == "一次只能放一張，請只拖一張進來。"
    assert flows["dropTwo"]["name"] == "IMG_0001.jpg" and flows["dropTwo"]["ready"] == "true"
    assert flows["dropText"]["error"] == web_app.MSG_BAD_EXT and flows["dropText"]["ready"] == "true"
    outside = flows["dropOutside"]
    assert outside["prevented"] and outside["kept"] and outside["error"] == web_app.MSG_BAD_EXT   # 框外的不算,也不換掉
    assert flows["dropBig"]["error"] == web_app.MSG_TOO_LARGE and flows["dropBig"]["ready"] == "false"
    assert flows["dropOld"]["error"] == "這個瀏覽器不能把照片拖進來，請按「選照片」。"
    assert flows["dropOld"]["ready"] == "false"


def test_photo_check_asks_only_after_the_preview_loads_and_shows_only_what_was_found(flows):
    assert flows["beforeLoad"] == {"fetches": 0, "hint": ""}
    checked = flows["checked"]
    assert checked["hint"] == HINT_BLUR
    assert checked["fetch"] == {"url": "/upload/check", "method": "POST", "credentials": "same-origin",
                                "parts": [["csrf_token", "tok", None], ["photo", "blob", "check.jpg"]]}
    assert checked["drawn"] == [1200, 1600] and checked["blobType"] == "image/jpeg"   # 縮到長邊 1600 再送
    # 提示不擋人:「開始辨識」照樣能按,也沒有寫成錯誤
    assert not checked["submitDisabled"] and checked["error"] is None
    assert flows["checkReset"]["hint"] == "" and flows["checkReset"]["ready"] == "false"
    assert flows["checkStale"]["hint"] == ""      # 換一張之後才回來的答案不算數
    assert flows["checkClean"]["hint"] == ""      # 沒偵測到就什麼都不寫
    assert flows["checkOffline"]["hint"] == "" and flows["checkOffline"]["error"] is None
    assert flows["checkPdf"] == {"photos": 0, "fetches": 0}     # PDF 沒有縮圖,也不檢查


def test_start_shows_the_real_upload_progress_then_follows_the_server(flows):
    sending = flows["sending"]
    assert sending["prevented"] and (sending["method"], sending["url"], sending["body"]) == ("POST", "/upload", "form")
    assert sending["submitDisabled"] and sending["resetDisabled"] and sending["label"] == "上傳中…"
    assert sending["progressShown"] and sending["waitShown"] and sending["bar"] == "0%"
    assert sending["hint"] == HINT_BLUR           # 已經寫出來的提示留著
    progress = flows["progress"]
    assert (progress["bar"], progress["valuenow"], progress["percent"]) == ("45%", "45", "45")
    assert flows["again"] == {"prevented": True, "xhrs": 1}       # 傳的時候再按一次不會再送一份
    assert flows["uploaded"]["bar"] == "100%" and flows["uploaded"]["status"] == "照片傳好了，請稍等。"
    assert flows["redirect"] == {"assigned": "http://127.0.0.1:8000/upload/progress/abc", "written": None}


def test_server_error_pages_replace_the_page_as_without_js(flows):
    """太大、格式不對、處理失敗:伺服器回的那一頁(沿用現有的錯誤文字與下一步)整頁換上"""
    assert flows["errorPage"] == {"assigned": None, "written": "<!doctype html><p>檔案超過 15MB。</p>", "opened": True}


def test_network_failure_says_what_to_do_and_lets_the_user_try_again(flows):
    network = flows["network"]
    assert network["error"] == "上傳沒有成功。請檢查網路，再按一次「開始辨識」；還是不行就重新整理這一頁。"
    assert not network["submitDisabled"] and not network["resetDisabled"] and network["label"] == "開始辨識"
    assert not network["progressShown"] and network["ready"] == "true" and network["focusSubmit"]


def test_browsers_without_progress_fall_back_to_a_plain_form_post(flows):
    fallback = flows["fallback"]
    assert not fallback["prevented"] and fallback["submitDisabled"]
    assert fallback["status"] == "正在上傳，請不要關閉這個畫面。" and not fallback["progressShown"]


def test_start_without_a_photo_still_asks_for_one(flows):
    assert flows["empty"]["prevented"] and flows["empty"]["xhrs"] == 0
    assert flows["empty"]["error"] == web_app.MSG_NO_FILE
