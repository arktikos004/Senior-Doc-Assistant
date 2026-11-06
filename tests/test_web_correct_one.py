"""更正頁一次一個問題(使用者 11/5):有問題的欄位一次只展開一個,其他收成一行摘要(欄位名稱加原因),按了才展開;
「下一個問題」收起目前這個、展開下一個;收起來的欄位還在同一張表單裡,送出時照樣送;沒有 JS 時全部展開。

伺服器畫面的結構用 TestClient 檢查;收合的行為用假的 DOM 跑 app.js(node),沒有 node 就略過那幾項。
資料都是測試當場產生的,資料夾隔離在 tmp_path,不連網、不呼叫模型。
"""
import json
import re
import subprocess

import pytest

from test_web_correct import _bag, _correct, browser_form
from test_web_static import _NODE, _problems
from web.render import STATIC_DIR

INVOICE = {"doc_type": "發票", "date": "2026-06-08", "vendor": "測試商店", "amount": 514.0, "currency": "NTD",
           "invoice_number": "QP94525678", "confidence": 0.95}
QR_FAIL = {"status": "fail", "detail": "QR 記載總計 244，辨識金額為 514", "fields": ["amount"], "expected": {"amount": 244}}
THREE = {**INVOICE, "amount": 514.0, "unreadable": ["vendor"], "invoice_number": "QP9452567",
         "verification": {"QR 總計額": QR_FAIL,
                          "字軌格式": {"status": "fail", "detail": "發票號碼 QP9452567 不符合「2 碼英文 + 8 碼數字」",
                                   "fields": ["invoice_number"]}}}


def _page(client, doc_id: int) -> str:
    return client.get(f"/doc/{doc_id}/correct").text


def _fold(page: str, anchor: str) -> str:
    """一個有問題的欄位整塊(到下一塊、「其他欄位」或表單結尾為止;裡面還有「看原件」的 <details>)。"""
    rest = page.split(f'<details class="issue-fold" id="{anchor}"')[1]
    return re.split(r'<details class="issue-fold"|<h3 class="issue-group">|</form>', rest)[0]


def _fold_heads(page: str) -> list[str]:
    """每個有問題的欄位的摘要那一行,去掉標籤只留字。"""
    heads = re.findall(r'<summary class="issue-fold__head">\s*<span class="issue-fold__line">(.*?)</span>\s*<span class="issue-fold__state"',
                       page, re.S)
    return [re.sub(r"<[^>]+>", "", h).strip() for h in heads]


# ---- 伺服器畫面:沒有 JS 時全部展開,摘要寫欄位名稱加原因 ---------------------------------------

def test_every_flagged_field_is_a_fold_with_its_name_and_reason(client, review_doc):
    page = _page(client, review_doc(THREE))
    assert _fold_heads(page) == ["商家：AI 讀不清楚", "金額：和發票上的 QR Code 不符",
                                 "發票號碼：檢查沒通過：發票號碼 QP9452567 不符合「2 碼英文 + 8 碼數字」"]
    tags = re.findall(r'<details class="issue-fold"[^>]*>', page)
    assert len(tags) == 3 and all(" open" in t for t in tags)        # 沒有 JS 時全部展開,和以前一樣
    assert _problems(page) == []


def test_several_reasons_share_one_line(client, review_doc):
    doc_id = review_doc({**INVOICE, "unreadable": ["amount"], "verification": {"QR 總計額": QR_FAIL}})
    assert _fold_heads(_page(client, doc_id)) == ["金額：和發票上的 QR Code 不符；AI 讀不清楚"]


def test_unit_and_label_follow_the_change_note_names(client, review_doc):
    """摘要與「下一個問題」用欄位的短名(不帶單位),和儲存後「改了什麼」同一套叫法。"""
    bill = {"doc_type": "帳單", "date": "2026-09-20", "vendor": "測試電力", "amount": 1236.0, "currency": "NTD",
            "confidence": 0.9, "unreadable": ["vendor"], "fields": {"due_date": "2026-10-15", "bill_kind": "電費"},
            "verification": {"條碼應繳金額": {"status": "fail", "fields": ["amount"], "expected": {"amount": 1286}}}}
    page = _page(client, review_doc(bill))
    assert _fold_heads(page)[0] == "應繳金額：和帳單上的超商條碼不符"
    assert "下一個問題：開單單位" in page and "（元）：" not in page
    assert '<label class="ff__label" for="f-amount">應繳金額（元）' in page          # 框上的標籤照舊寫單位


def test_folded_fields_stay_in_the_same_form_and_are_sent(client, store, review_doc):
    """收起來只是看不到:欄位都在同一張表單裡,瀏覽器照樣送出;改了收起來的那一欄,存得進去。"""
    doc_id = review_doc(THREE)
    page = _page(client, doc_id)
    form = re.search(rf'<form\b[^>]*action="/doc/{doc_id}/correct"[^>]*>(.*?)</form>', page, re.S).group(1)
    for name in ("vendor", "amount", "invoice_number", "date", "currency", "category"):
        assert f'name="{name}"' in form, name
    data = browser_form(page, f"/doc/{doc_id}/correct")
    r = client.post(f"/doc/{doc_id}/correct", data={**data, "invoice_number": "QP94525678", "amount": "244"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = store.get_document(doc_id)["result"]
    assert saved["invoice_number"] == "QP94525678" and saved["amount"] == 244.0 and saved["vendor"] == "測試商店"


def test_fold_heads_are_buttons_that_say_what_they_hold(client, review_doc):
    """<summary> 就是按鈕:鍵盤 Enter、空白鍵都能開合,讀屏念欄位名稱加原因與「已展開/已收合」。"""
    page = _page(client, review_doc(THREE))
    fold = _fold(page, "p-amount")
    assert 'data-focus="f-amount"' in fold and '<span class="issue-fold__state" data-fold-state hidden></span>' in fold
    assert 'aria-describedby="f-amount-issue' in fold                               # 輸入框念得到原因
    assert '<a class="next-issue" href="#p-invoice_number" data-next-issue="f-invoice_number">下一個問題：發票號碼</a>' in fold


def test_the_last_flagged_field_points_to_save(client, review_doc):
    page = _page(client, review_doc(THREE))
    fold = _fold(page, "p-invoice_number")
    assert '<a class="next-issue" href="#save" data-next-issue="save">下一步：儲存更正並重新核對</a>' in fold


def test_fields_without_problems_stay_open_under_other_fields(client, review_doc):
    page = _page(client, review_doc(THREE))
    rest = page.split("<h3 class=\"issue-group\">其他欄位</h3>")[1].split("</form>")[0]
    assert "issue-fold" not in rest and 'id="p-date"' in rest and 'id="p-currency"' in rest


def test_nothing_flagged_means_nothing_folded(client, review_doc):
    page = _page(client, review_doc(INVOICE))
    assert "issue-fold" not in page and "這張表單上的欄位沒有標出問題" in page


def test_a_flagged_medicine_list_folds_as_one_block(client, review_doc):
    doc_id = review_doc(_bag(verification={"藥品清單完整": {"status": "fail", "detail": "藥品清單不完整：第 2 項沒有讀到用法",
                                                        "fields": ["fields.items"]}}))
    page = _page(client, doc_id)
    assert _fold_heads(page) == ["藥品：檢查沒通過：藥品清單不完整：第 2 項沒有讀到用法"]
    fold = _fold(page, "p-items")
    assert 'data-focus="m0-name"' in page and "第 1 種藥" in fold and "第 2 種藥" in fold and 'name="add_item"' in fold


def test_a_typing_error_is_inside_its_fold(client, review_doc):
    """伺服器回錯誤時,錯的那一欄在它自己的收合區塊裡;app.js 會展開有錯或有焦點的那一個。"""
    doc_id = review_doc(THREE)
    data = browser_form(_page(client, doc_id), f"/doc/{doc_id}/correct")
    r = client.post(f"/doc/{doc_id}/correct", data={**data, "invoice_number": "QP12"})
    fold = _fold(r.text, "p-invoice_number")
    assert 'aria-invalid="true"' in fold and "autofocus" in fold
    assert "autofocus" not in _fold(r.text, "p-vendor") + _fold(r.text, "p-amount")


def test_fold_styles_live_in_the_correction_section():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    section = css.split("/* ---- 更正頁:原件與欄位並排對照 ---- */")[1].split("(更正頁的規則寫在這一行上面)")[0]
    for rule in (".issue-fold {", ".issue-fold__head {", ".issue-fold:not([open]) .issue-fold__line {", ".issue-fold__state--bad {"):
        assert rule in section, rule
    assert ".issue-fold" not in css.replace(section, "")


# ---- 有 JS:一次只展開一個(假的 DOM 跑 app.js) -------------------------------------------------

_FOLD_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
function el(name, attrs = {}) {
  return {name, attrs, listeners: {}, hidden: false, focused: [], scrolled: [], textContent: "", classes: new Set(),
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    hasAttribute(k) { return k in this.attrs; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    focus(opts) { this.focused.push(opts || null); },
    scrollIntoView(opts) { this.scrolled.push(opts || null); },
    getBoundingClientRect() { return {top: this.top === undefined ? 900 : this.top}; },
    fire(type, extra = {}) {
      const ev = Object.assign({type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }}, extra);
      (this.listeners[type] || []).forEach(fn => fn(ev));
      return ev;
    },
    get classList() { const s = this.classes; return {add: c => s.add(c), remove: c => s.delete(c), toggle: (c, on) => on ? s.add(c) : s.delete(c)}; }};
}
function fold(id, input, {autofocus = false, invalid = false} = {}) {
  const f = el("fold-" + id, {"data-issue-fold": "", "data-focus": input.attrs.id});
  f.open = true;
  const head = el("summary-" + id), state = el("state-" + id);
  state.hidden = true;
  input.tagName = "INPUT"; input.type = "text";
  if (autofocus) input.attrs.autofocus = "";
  if (invalid) input.attrs["aria-invalid"] = "true";
  f.querySelector = sel => ({"summary": head, "[data-fold-state]": state,
    "[autofocus]": autofocus ? input : null, '[aria-invalid="true"]': invalid ? input : null})[sel] || null;
  f.querySelectorAll = sel => sel === "input[name], select[name], textarea[name]" ? [input] : [];
  return {f, head, state, input};
}
function input(id, value) { return Object.assign(el("input-" + id, {id}), {value, defaultValue: value}); }
function load({autofocusOn = null, invalidOn = null} = {}) {
  const a = fold("a", input("f-vendor", "測試商店"), {autofocus: autofocusOn === "a", invalid: invalidOn === "a"});
  const b = fold("b", input("f-amount", "514"), {autofocus: autofocusOn === "b", invalid: invalidOn === "b"});
  const c = fold("c", input("f-invoice_number", "QP9452567"), {autofocus: autofocusOn === "c", invalid: invalidOn === "c"});
  const save = el("save", {id: "save"});
  const toB = el("next-a", {"data-next-issue": "f-amount", href: "#p-amount"});
  const toSave = el("next-c", {"data-next-issue": "save", href: "#save"});
  const byId = {"f-vendor": a.input, "f-amount": b.input, "f-invoice_number": c.input, "save": save, "p-amount": b.f, "p-vendor": a.f};
  b.f.attrs.id = "p-amount";
  const doc = {activeElement: null, addEventListener() {}, querySelector: () => null,
    getElementById: id => byId[id] || null,
    documentElement: {setAttribute() {}, getAttribute() { return null; }},
    querySelectorAll: sel => ({"details[data-issue-fold]": [a.f, b.f, c.f], "a[data-next-issue]": [toB, toSave]})[sel] || []};
  const win = Object.assign(el("window"), {setTimeout(fn) { fn(); }, HTMLFormElement: {prototype: {}}, innerHeight: 800,
    getComputedStyle: () => ({scrollPaddingTop: "88px"})});
  new Function("window", "document", "HTMLFormElement", src)(win, doc, win.HTMLFormElement);
  const open = () => [a, b, c].map(x => x.f.open);
  const states = () => [a, b, c].map(x => x.state.hidden ? "" : x.state.textContent);
  return {a, b, c, save, toB, toSave, open, states};
}
const out = {};
{ const t = load(); out.start = t.open(); }
{ const t = load({autofocusOn: "b"}); out.autofocus = t.open(); }
{ const t = load({invalidOn: "c"}); out.invalid = t.open(); out.invalidStates = t.states(); }
{ const t = load();
  t.a.input.value = "改過的商店";                            // 改了第一欄還沒存
  const ev = t.b.head.fire("click");                         // 按第二欄的摘要
  out.click = {prevented: ev.defaultPrevented, open: t.open(), states: t.states(), focused: [...t.b.input.focused],
               scrolled: [...t.b.f.scrolled], valueKept: t.a.input.value};
  t.b.head.fire("click");                                    // 再按一次:收起來
  out.closeAgain = t.open();
  const next = t.toB.fire("click");                          // 「下一個問題」:展開第二欄
  out.next = {prevented: next.defaultPrevented, open: t.open(), focused: t.b.input.focused.length};
  const last = t.toSave.fire("click");                       // 最後一個:全部收起來,焦點到儲存
  out.last = {prevented: last.defaultPrevented, open: t.open(), saveFocused: t.save.focused, states: t.states(),
              saveScrolled: t.save.scrolled};
}
{ const t = load();                                          // 已經在畫面上半部:不必捲
  t.b.f.top = 200; t.b.head.fire("click"); out.noScroll = t.b.f.scrolled; }
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_one_problem_is_open_at_a_time():
    script = _FOLD_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    r = json.loads(out.stdout)
    assert r["start"] == [True, False, False]                               # 有 JS:只留第一個
    assert r["autofocus"] == [False, True, False]                           # 伺服器要焦點去的那一個
    assert r["invalid"] == [False, False, True] and r["invalidStates"] == ["", "", ""]
    click = r["click"]
    assert click["prevented"] and click["open"] == [False, True, False]     # 按摘要:那一個展開,原本的收起
    assert click["states"] == ["已修改，尚未儲存", "", ""]                     # 收起來的寫出改了還沒存
    assert click["focused"] == [{"preventScroll": True}] and click["scrolled"] == [{"block": "start"}]
    assert click["valueKept"] == "改過的商店"                                  # 收起來,填的字還在
    assert r["closeAgain"] == [False, False, False]
    assert r["next"] == {"prevented": True, "open": [False, True, False], "focused": 2}
    last = r["last"]
    assert last["prevented"] and last["open"] == [False, False, False] and last["saveFocused"] == [{"preventScroll": True}]
    assert last["states"][0] == "已修改，尚未儲存" and last["saveScrolled"] == [{"block": "center"}]
    assert r["noScroll"] == []


_BAD_FLOW = _FOLD_FLOW.split("const out = {};")[0] + r"""
const t = load({invalidOn: "a"});
t.b.head.fire("click");                                      // 寫錯的那一欄被收起來:摘要旁寫「要修改」
console.log(JSON.stringify({open: t.open(), states: t.states(), bad: [...t.a.state.classes]}));
"""


@pytest.mark.skipif(_NODE is None, reason="沒有 node,略過前端流程測試")
def test_a_folded_field_with_an_error_says_it_needs_fixing():
    script = _BAD_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    r = json.loads(out.stdout)
    assert r == {"open": [False, True, False], "states": ["要修改", "", ""], "bad": ["issue-fold__state--bad"]}


def test_saving_a_folded_change_works_without_opening_it(client, store, review_doc):
    """伺服器不管哪一個展開:沒有展開過的欄位照樣送出、照樣檢查。"""
    doc_id = review_doc(THREE)
    r = _correct(client, doc_id, vendor="家人填的商家", amount="244", invoice_number="QP94525678")
    assert r.status_code == 303 and store.get_document(doc_id)["result"]["vendor"] == "家人填的商家"
