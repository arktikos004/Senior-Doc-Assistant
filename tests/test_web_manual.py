"""使用手冊與「說明」按鈕(使用者 11/4):畫面上只留標題與內容,說明集中在一份手冊,真的重要的事按了才展開。
手冊只有一份(docs/使用手冊.md),網站直接讀它。不呼叫模型。"""
import pytest
from fastapi.testclient import TestClient

from samples import BILL, LETTER
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import create_app
from web.manual import MANUAL_PATH, load_manual, render_manual

MANUAL = """# 使用手冊

> 給誰看。

開頭這段要留著。

## 1. 第一節

- 第一項，**粗體**
  續行
  - 第二層 `程式字`
- [連結](部署指南.md)

1. 第一步
2. 第二步

   | 欄位 | 說明 |
   |---|---|
   | <script>alert(1)</script> | **粗** |

5. 第五步

### 1.1 小節

> 引言要留著。

## 2. 給開發者

這裡不放到網站上。
"""


# ---- 轉換 ------------------------------------------------------------------------

def test_render_keeps_structure_and_escapes_everything():
    manual = render_manual(MANUAL)
    html = str(manual.html)
    assert manual.toc == (("m-1", "1. 第一節"),)
    assert "給誰看" not in html and "開頭這段要留著" in html and "引言要留著" in html
    assert '<h2 id="m-1">1. 第一節</h2>' in html and '<h3 id="s-1-1">1.1 小節</h3>' in html    # 小節有錨點:「這一篇的內容」
    assert "<li>第一項，<strong>粗體</strong>續行<ul><li>第二層 <code>程式字</code></li></ul></li>" in html
    assert "<li>連結</li>" in html and "部署指南.md" not in html                   # 連結只留文字
    assert "<ol><li>第一步</li><li>第二步</li></ol>" in html and '<ol start="5"><li>第五步</li></ol>' in html
    assert "<table>" in html and "<th>欄位</th>" in html and "<td><strong>粗</strong></td>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_developer_section_stays_out_of_the_site():
    html = str(render_manual(MANUAL).html)
    assert "給開發者" not in html and "這裡不放到網站上" not in html


def test_real_manual_renders_and_keeps_the_notes_moved_off_the_pages():
    assert MANUAL_PATH.is_file()
    manual = load_manual()
    html = str(manual.html)
    assert len(manual.toc) >= 9 and "給開發者" not in html and "<script" not in html
    for moved in ("系統不會自動刪除", "只能調得更嚴，不能低於 80%", "15MB 以內", "只是記錄", "不會替您付款",
                  "改了馬上生效", "遇到假日會順延", "類型不對", "填收到日期"):
        assert moved in html, moved


# ---- 頁面 ------------------------------------------------------------------------

def test_manual_page_is_in_the_menu_and_says_the_current_mode(client):
    assert 'href="/help"' in client.get("/").text
    html = client.get("/help").text
    assert "<h1>使用手冊</h1>" in html and "manual__toc" in html and "展示模式" in html    # 主題清單(說明中心,11/5)


def test_anyone_who_can_log_in_can_read_the_manual(cfg):
    class Viewer:
        def resolve(self, request):
            return Identity("stranger@example.test", False)

    r = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Viewer())).get("/help")
    assert r.status_code == 200 and "使用手冊" in r.text


@pytest.mark.parametrize("url", ["/", "/reminders", "/cabinet", "/confirm", "/review", "/settings", "/lottery",
                                 "/settings/members"])
def test_pages_keep_only_titles(client, url):
    """說明句與每頁的安心說明都搬去手冊了。"""
    html = client.get(url).text
    assert 'class="lede"' not in html
    assert "只協助閱讀" not in html and "核對結果由程式依規則檢查" not in html and 'class="trust' not in html


# ---- 「說明」按鈕 -----------------------------------------------------------------

def test_result_page_notes_are_closed_until_pressed(client, add_doc):
    doc_id = add_doc(dict(BILL, plain_summary="這是帳單。", source_model="mock:demo"))
    html = client.get(f"/doc/{doc_id}").text
    assert html.count('<details class="note">') >= 3 and "<details class=\"note\" open" not in html
    for inside in ("期限由 AI 讀取，請對照帳單。", "這段是 AI 讀完整理的"):
        note = html[:html.index(inside)].rsplit('<details class="note">', 1)[1]
        assert "</details>" not in note, inside                                   # 都在按鈕裡面
    fold = html[:html.index("辨識來源：mock:demo")].rsplit('<details class="section fold" id="fields">', 1)[1]
    assert "</details>" not in fold                                               # 辨識來源收在「讀到的內容」裡


def test_letter_note_links_to_the_received_date_field_for_family_only(client, add_doc):
    doc_id = add_doc(dict(LETTER, fields=dict(LETTER["fields"], deadline_basis="以發文日期 2026-09-25 起算；實際收到日可能較晚，期限以公文原文為準。")))
    html = client.get(f"/doc/{doc_id}").text
    note = html[html.index('id="answer-title"'):html.index("</details>", html.index('id="answer-title"'))]
    assert "以發文日期 2026-09-25 起算" in note and "#f-fields-received_date" in note


def test_home_note_says_marking_done_does_not_pay(client):
    html = client.get("/").text
    head = html[html.index('id="remind-title"'):html.index("</details>", html.index('id="remind-title"'))]
    assert '<details class="note">' in head and "系統不會付款" in head


def test_note_button_is_an_icon_that_screen_readers_still_call_note(client):
    """「說明」改成 ⓘ 圖示(使用者 11/5):接在標題後面,畫面上不寫字,讀屏照樣念「說明」。"""
    html = client.get("/").text
    head = html[html.index('id="remind-title"'):html.index("</details>", html.index('id="remind-title"'))]
    summary = head.split("<summary>")[1].split("</summary>")[0]
    assert summary.startswith('<svg class="icon note__icon"')
    assert summary.endswith('<span class="visually-hidden">說明</span>')


def test_note_opens_as_a_small_popup_with_a_little_motion():
    """使用者 11/5:「說明圖示在小一點 用彈出視窗呈現 加一點過度動畫 屬標放在下面(沒點)會有些動畫」(屬標是鼠標)。
    ⓘ 縮小,觸控區照舊 44px;內容是浮在圖示下面的小框,不把下面的內容往下推;打開淡入、收起淡出(收起由 app.js 加
    data-closing);只有滑鼠的裝置,滑過(沒按)時圖示放大、後面浮出淡色圓底。減少動態時(Windows 的「動畫效果」關著,
    瀏覽器就會這樣要求)不放大、不移動,只留淡入淡出,不是整個沒有過渡。"""
    from web.render import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "width: 1.125rem; height: 1.125rem;" in css.split(".note__icon {")[1].split("}")[0]
    summary = css.split(".note > summary {")[1].split("}")[0]
    assert "min-width: 2.75rem; min-height: 2.75rem;" in summary
    assert "position: absolute;" in css.split(".note__body {")[1].split("}")[0]
    assert ".note[open] > .note__body { animation: note-pop-in" in css
    assert ".note[data-closing] > .note__body { animation: note-pop-out" in css
    hover = css.split("@media (hover: hover) {")[1].split("\n}\n")[0]
    assert ".note > summary:hover .note__icon {" in hover and "scale(" in hover
    reduced = css.split("@media (prefers-reduced-motion: reduce) {")[1].split("\n}\n")[0]
    assert ".note[open] > .note__body { animation: note-fade-in .18s ease-out !important; }" in reduced
    assert ".note > summary:hover .note__icon, .note > summary:active .note__icon { transform: none; }" in reduced
    assert "@keyframes note-fade-in { from { opacity: 0; } }" in css and "@keyframes note-fade-out { to { opacity: 0; } }" in css


def test_a_note_with_no_heading_beside_it_keeps_its_word():
    """前面沒有標題可以接的(PDF 密碼頁、更正頁藥品那一格)傳 label:圖示旁照樣寫字,不會只剩一個圖示。"""
    from web.render import TEMPLATES_DIR, env

    html = env.from_string('{% from "_note.html" import note %}{{ note("內容", label="說明") }}').render()
    summary = html.split("<summary>")[1].split("</summary>")[0]
    assert summary.startswith('<svg class="icon note__icon"') and summary.endswith('<span class="note__label">說明</span>')
    assert 'label="說明"' in (TEMPLATES_DIR / "upload_password.html").read_text(encoding="utf-8")
    assert '{{ med_note("說明") }}' in (TEMPLATES_DIR / "correct.html").read_text(encoding="utf-8")


def test_medication_disclaimer_is_in_the_note_and_still_read_aloud(client, add_doc):
    """藥袋聲明收進「服藥時間表」旁的「說明」(使用者 11/4);聽重點照樣會唸(data-speak-part)。"""
    doc_id = add_doc({"doc_type": "藥袋", "vendor": "示範診所", "fields": {"items": [{"name": "示範藥甲"}]}})
    html = client.get(f"/doc/{doc_id}").text
    assert '<p class="disclaimer"' not in html and html.count("本系統只協助閱讀，不提供醫療建議") == 1
    note = html[html.index('id="answer-title"'):html.index("</details>", html.index('id="answer-title"'))]
    assert '<span class="note__line" data-speak-part>本系統只協助閱讀，不提供醫療建議；用藥請依醫師與藥師指示。</span>' in note
