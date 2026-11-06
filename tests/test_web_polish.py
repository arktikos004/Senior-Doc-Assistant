"""全站優化(使用者 11/5:用 Taste Skill、frontend-design、UI/UX Pro Max、Impeccable、Make Interfaces Feel Better、
web-design-guidelines 與 unslop 優化頁面,範圍是全站)。每一條寫明出自哪一份規則;大改(間距改成 4px 格線、
浮動標籤改用 transform)之後再做。合成資料,不呼叫模型。"""
import json
import shutil
import subprocess

import pytest

from web.render import STATIC_DIR, TEMPLATES_DIR

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
_NODE = shutil.which("node")


def _reduced() -> str:
    return CSS.split("@media (prefers-reduced-motion: reduce) {")[1].split("\n}\n")[0]


def test_buttons_press_down_a_little():
    """Make Interfaces Feel Better 第 12 條、unslop 動態:按下去縮成 0.96,只轉場列出來的屬性
    (web-design-guidelines:不用 transition: all);減少動態時不縮。"""
    assert ".btn:active:not([disabled]) { transform: scale(.96); }" in CSS
    btn = CSS.split("\n.btn {")[1].split("}")[0]
    assert "transform .1s ease-out" in btn and "transition: all" not in CSS
    assert ".btn:active:not([disabled]) { transform: none; }" in _reduced()


def test_the_chosen_photo_gets_a_hairline_outline():
    """Make Interfaces Feel Better 第 11 條:照片加 1px、10% 的黑框(深色是白),白底的帳單照片邊緣才看得出來。
    原件與確認頁的照片是等比例縮在框裡,外框會畫在留白上,不加。"""
    rule = CSS.split(".chosen__preview { outline:")[1].split("}")[0]
    assert "1px solid rgba(0, 0, 0, .1); outline-offset: -1px;" in rule
    assert ".chosen__preview { outline-color: rgba(255, 255, 255, .1); }" in CSS


def test_headings_wrap_evenly():
    """web-design-guidelines、Make Interfaces Feel Better:標題用 text-wrap: balance,不會剩一兩個字自己一行。"""
    assert "h1, h2, h3 { text-wrap: balance; }" in CSS


def test_dialog_and_menu_do_not_scroll_the_page_behind():
    """web-design-guidelines:確認框與手機選單捲到底時不帶動後面的頁面。"""
    for selector in ("\n.dialog {", "  .menu__panel {"):
        assert "overscroll-behavior: contain" in CSS.split(selector)[1].split("}")[0], selector


def test_touch_targets_side_by_side_keep_8px_apart():
    """UI/UX Pro Max(Touch Spacing):相鄰的觸控目標至少隔 8px;類型選項原本約 7px。"""
    assert ".chips { display: flex; flex-wrap: wrap; gap: .5rem; }" in CSS


def test_note_icon_eases_out_without_bounce():
    """unslop(簡單的介面不用回彈)、Make Interfaces Feel Better:滑過只放大一點,平順減速。"""
    assert "1.56" not in CSS.split(".note__icon {")[1].split("}")[0]
    assert ".note > summary:hover .note__icon { transform: scale(1.1);" in CSS


def test_long_forms_warn_before_leaving_with_unsaved_changes():
    """web-design-guidelines:改了還沒存就要離開時先問(更正頁、對獎、系統設定)。"""
    for name in ("correct.html", "lottery.html", "settings.html"):
        assert "data-warn-unsaved" in (TEMPLATES_DIR / name).read_text(encoding="utf-8"), name


# 用假的 DOM 跑 app.js 的 data-warn-unsaved;真的瀏覽器只會顯示它自己的固定句子
_UNSAVED_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
const later = [], heard = {};
function field(value) {
  return {tagName: "INPUT", type: "text", value, defaultValue: value, checked: false, defaultChecked: false};
}
const amount = field("1286"), note = field("");
const form = {listeners: {}, querySelectorAll: () => [amount, note],
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  fire(type, prevented) { const ev = {type, defaultPrevented: Boolean(prevented)};
    (this.listeners[type] || []).forEach(fn => fn(ev)); return ev; }};
const doc = {querySelector: () => null, addEventListener() {},
             documentElement: {setAttribute() {}, getAttribute() { return null; }},
             querySelectorAll: sel => sel === "form[data-warn-unsaved]" ? [form] : []};
const win = {setTimeout(fn) { later.push(fn); }, HTMLFormElement: {prototype: {}},
             addEventListener(type, fn) { (heard[type] = heard[type] || []).push(fn); }};
new Function("window", "document", "HTMLFormElement", src)(win, doc, win.HTMLFormElement);
const leave = () => { const ev = {defaultPrevented: false, returnValue: undefined,
  preventDefault() { this.defaultPrevented = true; }}; (heard.beforeunload || []).forEach(fn => fn(ev));
  return [ev.defaultPrevented, ev.returnValue === ""]; };
const flush = () => { while (later.length) later.shift()(); };
const out = {};
out.untouched = leave();                                   // 沒改:不問
amount.value = "1268"; out.changed = leave();               // 改了:問
amount.value = "1286"; out.back = leave();                  // 改回原樣:不問
amount.value = "1268";
form.fire("submit", true); flush(); out.blocked = leave();  // 送出被擋下(還在這一頁):照樣問
form.fire("submit", false); flush(); out.sent = leave();    // 真的送出:不問
(heard.pageshow || []).forEach(fn => fn({persisted: true})); out.back_again = leave();   // 按上一頁回來:又會問
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_unsaved_changes_are_asked_about_only_when_really_leaving():
    script = _UNSAVED_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    assert json.loads(out.stdout) == {"untouched": [False, False], "changed": [True, True], "back": [False, False],
                                      "blocked": [True, True], "sent": [False, False], "back_again": [True, True]}
