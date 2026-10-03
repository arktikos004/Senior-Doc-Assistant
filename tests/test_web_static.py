"""樣板與靜態檔測試:沒有行內 JS(W2-B 嚴格 CSP 的前提)、PWA manifest、朗讀的逐位數字。"""
import json
import re
import shutil
import subprocess

import pytest

from web.render import STATIC_DIR, TEMPLATES_DIR

# <script> 只能用 src 引入外部檔,而且中間不能有內容
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>|<script[^>]*>(?!\s*</script>)", re.I)
_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=", re.I)
_STYLE_ATTR = re.compile(r"\sstyle\s*=", re.I)
_JS_URL = re.compile(r"javascript:", re.I)
# 撞名棄用的舊產品名稱(英文、中文);寫成正規式,在 repo 搜尋舊名時只會搜到真的外露,不會搜到這個檢查
_OLD_NAMES = re.compile(r"doc\s*pilot|文書領航員", re.I)


def _problems(html: str) -> list[str]:
    found = []
    for name, pattern in (("行內 script", _INLINE_SCRIPT), ("on* 事件屬性", _EVENT_ATTR),
                          ("style 屬性", _STYLE_ATTR), ("javascript: 網址", _JS_URL)):
        found += [f"{name}: {m.group(0)!r}" for m in pattern.finditer(html)]
    return found


def test_scanner_catches_inline_code():
    assert _problems('<script>alert(1)</script>')
    assert _problems('<script src="/static/app.js">alert(1)</script>')
    assert _problems('<form onsubmit="return confirm()">')
    assert _problems('<p style="color:red">')
    assert not _problems('<script src="/static/app.js" defer></script>')


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES_DIR.glob("*.html")))
def test_templates_have_no_inline_js(template):
    assert _problems((TEMPLATES_DIR / template).read_text(encoding="utf-8")) == []


def test_rendered_pages_have_no_inline_js(client, store, add_doc):
    doc_id = add_doc({"doc_type": "藥袋", "plain_summary": "示範", "fields": {"items": [{"name": "示範藥"}]}})
    store.add_action(doc_id, "medication_schedule", "confirm", {"items": [{"name": "示範藥", "timing": ["早"]}]})
    for url in ("/", "/confirm", "/review", f"/doc/{doc_id}", "/doc/999"):
        assert _problems(client.get(url).text) == [], url


def test_pages_link_manifest_and_external_scripts(client):
    html = client.get("/").text
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in html
    assert '<script src="/static/app.js" defer></script>' in html
    assert "https://" not in html and "http://" not in html   # 沒有 CDN,離線可用


def test_brand_is_khuannu_on_every_page(client):
    """10/1 定案:短名稱「看有」+ 副標;撞名的舊名稱不能再出現在任何頁面。"""
    for url in ("/", "/confirm", "/review", "/doc/999"):
        html = client.get(url).text
        assert "<b>看有</b><small>高齡家庭文書輔助</small>" in html and "看有 高齡家庭文書輔助</title>" in html, url
        assert not _OLD_NAMES.search(html), url


def test_manifest_is_valid(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    data = json.loads(r.text)
    assert data["name"].startswith("看有") and data["short_name"] == "看有" and data["start_url"] == "/"
    icon = data["icons"][0]
    assert client.get(icon["src"]).status_code == 200 and icon["type"] == "image/svg+xml"


def test_static_assets_do_not_load_external_resources():
    text_files = [p for p in STATIC_DIR.rglob("*")
                  if p.is_file() and p.suffix in (".css", ".js", ".svg", ".webmanifest", ".html")]
    assert text_files
    for path in text_files:
        text = path.read_text(encoding="utf-8")
        assert "@import" not in text, path.name
        assert not re.search(r"url\(\s*['\"]?https?:", text), path.name
        assert "fonts.googleapis" not in text and "cdn" not in text.lower(), path.name
        assert not _OLD_NAMES.search(text), path.name      # 舊名稱也不能留在前端(例如 JS 的全域名稱)


def test_css_respects_reduced_motion_and_keeps_tokens():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    assert "--primary" in css and ":focus-visible" in css
    assert "Atkinson" not in css


# ---- 確認框(取代瀏覽器內建的確認視窗;樣稿 A) ---------------------------------
_DIALOG = re.compile(r"<dialog\b([^>]*)>(.*?)</dialog>", re.S)
_BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def test_confirm_dialog_is_shared_and_starts_on_the_safe_choice(client):
    """base.html 的共用確認框:每頁一個、預設關著;「先不要」在前面而且是預設焦點,「確定…」前面是 ✕。
    不寫「取消」:和「取消提醒」撞字,長輩容易按反。"""
    pages = {url: client.get(url).text for url in ("/", "/confirm", "/review", "/doc/999")}
    assert {url: len(_DIALOG.findall(html)) for url, html in pages.items()} == dict.fromkeys(pages, 1)
    attrs, body = _DIALOG.search(pages["/confirm"]).groups()
    assert 'class="dialog"' in attrs and "data-confirm-dialog" in attrs and not re.search(r"\sopen\b", attrs)
    title_id = re.search(r'aria-labelledby="([^"]+)"', attrs).group(1)
    msg_id = re.search(r'aria-describedby="([^"]+)"', attrs).group(1)
    assert f'id="{title_id}"' in body and f'id="{msg_id}"' in body
    (no_attrs, no_text), (ok_attrs, ok_text) = _BUTTON.findall(body)
    assert 'class="btn btn--secondary btn--lg"' in no_attrs and "autofocus" in no_attrs and no_text == "先不要"
    assert 'class="btn btn--danger btn--lg"' in ok_attrs and "autofocus" not in ok_attrs
    assert '<path d="M18 6 6 18"/>' in ok_text and _TAGS.sub("", ok_text) == "確定"   # ✕ + 預設的確定鍵文字
    assert 'type="button"' in no_attrs and 'type="button"' in ok_attrs              # 框裡的按鈕不送出任何表單
    assert "取消" not in body


def test_confirm_dialog_css_and_js():
    """樣式:沒打開不顯示、減少動態時不做淡入、強制色彩時有框;app.js 用 showModal,保留 window.confirm 退路。"""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".dialog:not([open]) { display: none; }" in css and ".dialog::backdrop" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)")[1].split("\n}\n")[0]
    forced = css.split("@media (forced-colors: active)")[1].split("\n}\n")[0]
    assert ".dialog[open], .dialog::backdrop { animation: none; }" in reduced
    assert ".dialog { border: 2px solid CanvasText; }" in forced
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "dialog.showModal()" in js and "button.form.requestSubmit(button)" in js
    assert "window.confirm(" in js                       # 不支援 <dialog> 或 requestSubmit 時的退路
    assert "data-confirm-title" in js and "data-confirm-ok" in js


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端純函式測試")
def test_speech_reads_digits_one_by_one():
    script = (
        "require(%r); const s = globalThis.ReadAloud;"
        "console.log(JSON.stringify(["
        "s.spellDigits('1854'), s.spellDigits('要繳1,854元'), s.spellDigits('2026-10-15'),"
        "s.spellDigits('12.5'), s.spellDigits('１２'), s.sentences('第一句。第二句!第三句')]))"
    ) % str(STATIC_DIR / "speech.js")
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    spelled = json.loads(out.stdout)
    assert spelled[0] == "一 八 五 四"
    assert spelled[1] == "要繳 一 八 五 四 元"
    assert spelled[2] == "二 零 二 六 年 一 零 月 一 五 日"
    assert spelled[3] == "一 二 點 五"
    assert spelled[4] == "一 二"
    assert spelled[5] == ["第一句。", "第二句!", "第三句"]


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端純函式測試")
def test_speech_only_uses_local_voices():
    """雲端語音會把要唸的文字(可能是藥袋)送出這台電腦:只挑本機語音,沒有就不唸。"""
    script = (
        "require(%r); const pick = globalThis.ReadAloud.pickLocalVoice;"
        "const v = (name, lang, local) => ({name, lang, localService: local});"
        "console.log(JSON.stringify(["
        "pick([v('Google', 'zh-TW', false), v('Meijia', 'zh-TW', true)]),"
        "pick([v('Google', 'zh-TW', false)]),"
        "pick([v('Hanhan', 'zh_TW', true)]),"
        "pick([v('Sinji', 'zh-HK', true)]),"
        "pick([v('Tingting', 'zh-CN', true), v('Sinji', 'zh-HK', true)]),"
        "pick([])]))"
    ) % str(STATIC_DIR / "speech.js")
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    picked = [p["name"] if p else None for p in json.loads(out.stdout)]
    assert picked == ["Meijia", None, "Hanhan", None, "Tingting", None]


# 用假的 DOM 跑 app.js 的確認框流程(真的瀏覽器行為另外用 CDP 截圖自評);requestSubmit 和瀏覽器一樣當場觸發 submit
_CONFIRM_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
function load({dialog = true, requestSubmit = true, answer = true} = {}) {
  const log = [], clock = {now: 0}, later = [];
  const doc = {activeElement: null, addEventListener() {},
               documentElement: {setAttribute() {}, getAttribute() { return null; }}};
  function node(name, attrs = {}) {
    return {name, attrs, textContent: "", hidden: false, listeners: {},
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      fire(type, extra = {}) {
        const ev = Object.assign({type, target: this, defaultPrevented: false,
                                  preventDefault() { this.defaultPrevented = true; }}, extra);
        (this.listeners[type] || []).forEach(fn => fn(ev));
        return ev;
      },
      focus() { doc.activeElement = this; log.push("focus " + name); }};
  }
  function form(name, attrs) {
    const f = node(name, attrs);
    f.requestSubmit = submitter => {
      log.push("requestSubmit " + (submitter ? submitter.name : "-"));
      if (!f.fire("submit", {submitter}).defaultPrevented) log.push("sent " + name + " " + (submitter ? submitter.attrs.value : "-"));
    };
    return f;
  }
  const parts = {title: node("title"), msg: node("msg"), no: node("no"), ok: node("ok"), "ok-label": node("ok-label")};
  parts["ok-label"].textContent = "確定";
  const dlg = Object.assign(node("dialog"), {open: false, querySelector: sel => parts[sel.slice(13, -1)] || null,
    close() { if (this.open) { this.open = false; log.push("close"); later.push(() => dlg.fire("close")); } }});
  if (dialog) dlg.showModal = () => { dlg.open = true; log.push("showModal"); parts.no.focus(); };
  const decision = form("decision"), whole = form("whole", {"data-confirm": "整張表單送出前先問"});
  const reject = Object.assign(node("reject", {value: "rejected", "data-confirm-title": "確定要退回「服藥時間表」嗎?",
                                               "data-confirm": "退回後就不會生效。", "data-confirm-ok": "確定退回"}), {form: decision});
  doc.querySelector = sel => sel === "[data-confirm-dialog]" ? dlg : null;
  doc.querySelectorAll = sel => ({"form[data-confirm]": [whole], "button[data-confirm]": [reject]})[sel] || [];
  const win = {HTMLFormElement: {prototype: requestSubmit ? {requestSubmit() {}} : {}}, addEventListener() {},
               confirm: text => { log.push("confirm " + text); return answer; }};
  new Function("window", "document", "HTMLFormElement", "Date", src)(win, doc, win.HTMLFormElement, {now: () => clock.now});
  return {log, clock, dlg, parts, reject, whole, submitter: node("submitter", {value: "go"}),
          flush() { while (later.length) later.shift()(); }};
}
const out = {};
{ // 退回鈕:擋下、填字、開框;剛打開就按「確定」不算數;過半秒再按 → 焦點回退回鈕、帶 decision 送出一次
  const t = load(), ev = t.reject.fire("click");
  t.clock.now = 200; t.parts.ok.fire("click");
  const early = t.dlg.open;
  t.clock.now = 1500; t.parts.ok.fire("click"); t.flush();
  out.ok = {prevented: ev.defaultPrevented, early, title: t.parts.title.textContent, msg: t.parts.msg.textContent,
            msgHidden: t.parts.msg.hidden, label: t.parts["ok-label"].textContent, log: t.log};
}
{ // 「先不要」與 Esc(瀏覽器直接關框):都不送出,焦點回到退回鈕;之後再按退回會再問一次
  const t = load();
  t.reject.fire("click"); t.clock.now = 1000; t.parts.no.fire("click"); t.flush();
  t.reject.fire("click"); t.dlg.open = false; t.dlg.fire("close");
  out.no = t.log;
}
{ // 整張表單:只寫 data-confirm 就整句當標題、確定鍵用框裡原本的字;重新送出帶上原本的按鈕,不會再被攔
  const t = load(), ev = t.whole.fire("submit", {submitter: t.submitter});
  const shown = {title: t.parts.title.textContent, msgHidden: t.parts.msg.hidden, label: t.parts["ok-label"].textContent};
  t.clock.now = 1000; t.parts.ok.fire("click");
  out.form = Object.assign(shown, {prevented: ev.defaultPrevented, log: t.log});
}
{ // 舊瀏覽器:沒有 showModal 或 requestSubmit → 內建確認視窗,標題與說明接成兩行;按取消才擋下
  const a = load({dialog: false, answer: false}), b = load({requestSubmit: false, answer: true});
  out.fallback = [[a.reject.fire("click").defaultPrevented, a.log], [b.reject.fire("click").defaultPrevented, b.log]];
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_confirm_dialog_flow():
    """按「確定」才送出(按鈕觸發要帶 decision、表單觸發不再被攔);先不要與 Esc 不送出、焦點回原處;舊瀏覽器退路。"""
    script = _CONFIRM_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    r = json.loads(out.stdout)
    ok = r["ok"]
    assert ok["prevented"] and ok["early"]                       # 連點兩下的第二下不算數,框還開著
    assert (ok["title"], ok["msg"], ok["msgHidden"], ok["label"]) == ("確定要退回「服藥時間表」嗎?", "退回後就不會生效。",
                                                                    False, "確定退回")
    assert ok["log"] == ["showModal", "focus no", "close", "focus reject", "requestSubmit reject", "sent decision rejected"]
    assert r["no"] == ["showModal", "focus no", "close", "focus reject", "showModal", "focus no", "focus reject"]
    form = r["form"]
    assert form["prevented"] and (form["title"], form["msgHidden"], form["label"]) == ("整張表單送出前先問", True, "確定")
    assert form["log"] == ["showModal", "focus no", "close", "focus submitter", "requestSubmit submitter", "sent whole go"]
    asked = "confirm 確定要退回「服藥時間表」嗎?\n退回後就不會生效。"
    assert r["fallback"] == [[True, [asked]], [False, [asked]]]
