"""使用手冊改成說明中心(使用者 11/5:「使用手冊不是說要向 claude 那樣?」,選了「說明中心+短文章」)。
/help 是首頁:搜尋手冊、主題清單(每篇一句摘要、幾個小節);/help/<編號> 是那一篇:導覽路徑、更新日期、
「在此頁面上」、上一篇與下一篇。手冊還是只有一份 docs/使用手冊.md。不呼叫模型。"""
import re

from web.manual import load_manual, render_manual, search_manual

MANUAL = """# 使用手冊

> 給誰看。
> 最後更新：2026-11-05

「看有」幫您看懂寄到家裡的文件。

## 1. 打開

> 從哪裡打開、分頁在哪裡。

- 第一項，詳見第 2 節與第 2.1 節。

## 2. 拍照

> 怎麼拍照。

先拍一張。

### 2.1 拍清楚

光線充足、把紙放平。

> 這段引言是內文，不是摘要。

### 2.2 選類型

選帳單、公文、藥袋。

## 3. 給開發者

這裡不放到網站上。
"""


# ---- 轉換 ------------------------------------------------------------------------

def test_each_big_section_becomes_one_article_with_its_summary_and_subsections():
    manual = render_manual(MANUAL)
    assert [(t.number, t.title) for t in manual.topics] == [("1", "打開"), ("2", "拍照")]
    first, second = manual.topics
    assert first.summary == "從哪裡打開、分頁在哪裡。" and second.summary == "怎麼拍照。"
    assert "從哪裡打開" not in str(first.html) and "怎麼拍照" not in str(second.html)     # 摘要不重複放進內文
    assert second.sections == (("s-2-1", "2.1 拍清楚"), ("s-2-2", "2.2 選類型"))
    # 一篇自己一頁,標題是 h1,小節就是第二層(Impeccable 稽核:h3 接在「在此頁面上」的 h2 後面,讀屏會當成它的子項)
    assert '<h2 id="s-2-1">2.1 拍清楚</h2>' in str(second.html) and "<h3" not in str(second.html)
    assert "這段引言是內文，不是摘要" in str(second.html)                                  # 小節裡的引言照舊是內文
    assert [p.anchor for p in second.parts] == ["", "s-2-1", "s-2-2"] and "把紙放平" in second.parts[1].text
    assert "看懂寄到家裡的文件" in str(manual.intro) and manual.updated == "2026 年 11 月 5 日"
    assert all("給開發者" not in t.title for t in manual.topics)


def test_section_references_become_links_to_that_article():
    html = str(render_manual(MANUAL).topics[0].html)
    assert '<a href="/help/2">第 2 節</a>' in html and '<a href="/help/2#s-2-1">第 2.1 節</a>' in html


def test_search_needs_every_word_and_points_at_the_subsection():
    manual = render_manual(MANUAL)
    hits = search_manual(manual, "紙  光線")
    assert [(h.href, h.topic, h.title) for h in hits] == [("/help/2#s-2-1", "拍照", "2.1 拍清楚")]
    assert "把<mark>紙</mark>放平" in hits[0].snippet and "<mark>光線</mark>" in hits[0].snippet   # 搜尋的字標出來
    tricky = render_manual(MANUAL.replace("光線充足", "光線<b>充足</b>"))
    assert "&lt;b&gt;" in search_manual(tricky, "光線")[0].snippet                               # 其他字照樣跳脫
    assert search_manual(manual, "ＰＨＯＴＯ 不存在") == () and search_manual(manual, "   ") == ()
    assert [h.href for h in search_manual(manual, "先拍")] == ["/help/2"]                  # 一篇開頭那段連到那一篇


def test_real_manual_describes_the_v5_screens():
    """v5 車站時刻表(11/6):手冊寫到顏色與版面的句子照新介面改——主要按鈕是黃色、分頁在最上面的深色列、
    要注意的事寫在狀態牌上;不再說「藍色」、選單在左邊或目前的分頁有底線。"""
    manual = load_manual()
    text = re.sub(r"<[^>]+>", "", str(manual.html))
    assert manual.updated == "2026 年 11 月 6 日"
    assert "藍" not in text and "電腦版在左邊" not in text and "目前所在的分頁有底線" not in text
    for said in ("最上面是一條深色的橫列", "目前所在的分頁是黃色的", "黃色的按鈕", "紅牌", "黃牌",
                 "結果頁只有一顆黃色的主要按鈕", "黃色的主要按鈕是 更正讀值", "一整塊深色的看板"):
        assert said in text, said


def test_topic_rows_use_narrow_numbers_and_underlined_titles():
    """主題列的編號是序列,留著,用窄體數字;標題和其他連結一樣是深墨字加底線。"""
    from test_web_cabinet import css_rule

    assert "font-family: var(--font-num)" in css_rule(".help-topic__num")
    title = css_rule(".help-topic b, .help-hit b")
    assert "color: var(--link)" in title and "text-decoration: underline" in title
    assert "text-decoration: underline" in css_rule(".help-pager__link b")


def test_real_manual_has_ten_articles_each_with_a_summary():
    manual = load_manual()
    assert [t.number for t in manual.topics] == [str(n) for n in range(1, 11)]
    assert all(t.summary and len(t.summary) <= 60 for t in manual.topics), [t.title for t in manual.topics if not t.summary]
    assert manual.updated and all("給開發者" not in t.title for t in manual.topics)
    assert "需要您處理" not in str(manual.html)                       # 11/5 拿掉的首頁區塊,手冊不能再寫


# ---- 頁面 ------------------------------------------------------------------------

def test_help_home_has_search_and_topics_and_the_current_mode(client):
    html = client.get("/help").text
    assert "<h1>使用手冊</h1>" in html and "展示模式" in html
    form = re.search(r'<form class="search help-search" method="get" action="/help" role="search">(.*?)</form>', html, re.S)
    assert form and 'name="q"' in form.group(1) and 'class="btn btn--secondary search__btn"' in form.group(1)
    topics = re.findall(r'<a class="help-topic" href="/help/(\d+)">', html)
    assert topics == [str(n) for n in range(1, 11)]
    assert "manual__toc" in html and all(t.summary in html for t in load_manual().topics)


def test_help_search_lists_matches_with_links(client):
    html = client.get("/help", params={"q": "退回 確認框"}).text
    assert "搜尋「退回 確認框」" in html
    links = re.findall(r'<a class="help-hit" href="([^"]+)">', html)
    assert links and all(link.startswith("/help/") for link in links)
    none = client.get("/help", params={"q": "<script>不存在的字"}).text
    assert "沒有找到" in none and "<script>不存在" not in none and "&lt;script&gt;" in none


def test_article_page_has_crumbs_date_on_this_page_and_neighbours(client):
    manual = load_manual()
    topic = next(t for t in manual.topics if t.number == "3")
    html = client.get("/help/3").text
    assert '<nav class="crumbs" aria-label="位置"><a href="/help">使用手冊</a>' in html and "第 3 節" in html
    assert f"<h1>{topic.title}</h1>" in html and topic.summary in html and f"更新於 {manual.updated}" in html
    toc = re.search(r'<nav class="help-toc" aria-labelledby="on-this-page">(.*?)</nav>', html, re.S).group(1)
    anchors = re.findall(r'href="#([^"]+)"', toc)
    assert anchors == [a for a, _ in topic.sections] and all(f'id="{a}"' in html for a in anchors)
    assert 'href="/help/2" rel="prev"' in html and 'href="/help/4" rel="next"' in html
    first, last = client.get("/help/1").text, client.get("/help/10").text
    assert 'rel="prev"' not in first and 'rel="next"' not in last


def test_unknown_or_developer_articles_are_not_found(client):
    for number in ("0", "11", "abc", "3x"):
        assert client.get(f"/help/{number}").status_code == 404, number


def test_on_this_page_is_folded_on_phones_and_always_open_on_computers(client):
    """Impeccable 稽核:手機上十幾個小節排在內文前面要捲很久。手機與平板先收起來,電腦一直打開;沒有 JS 時一直打開。"""
    html = client.get("/help/3").text
    toc = re.search(r'<nav class="help-toc" aria-labelledby="on-this-page">(.*?)</nav>', html, re.S).group(1)
    assert '<details class="help-toc__fold" open data-collapse-narrow>' in toc
    assert '<summary><h2 id="on-this-page">在此頁面上</h2>' in toc


_COLLAPSE_FLOW = r"""
const src = require("fs").readFileSync(__APP_JS__, "utf8");
const summary = {tabIndex: 0, listeners: {}, addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  click() { const ev = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
            (this.listeners.click || []).forEach(fn => fn(ev)); return ev.defaultPrevented; }};
const fold = {open: true, querySelector: sel => sel === "summary" ? summary : null};
const mq = {matches: true, listeners: [], addEventListener(type, fn) { this.listeners.push(fn); }};
const doc = {querySelector: () => null, addEventListener() {}, documentElement: {setAttribute() {}, getAttribute() { return null; }},
             querySelectorAll: sel => sel === "details[data-collapse-narrow]" ? [fold] : []};
const win = {matchMedia: () => mq, addEventListener() {}, setTimeout() {}, HTMLFormElement: {prototype: {}}};
new Function("window", "document", "HTMLFormElement", src)(win, doc, win.HTMLFormElement);
const out = {phone: [fold.open, summary.tabIndex, summary.click()]};
mq.matches = false; mq.listeners.forEach(fn => fn({matches: false}));
out.computer = [fold.open, summary.tabIndex, summary.click()];
console.log(JSON.stringify(out));
"""


def test_on_this_page_follows_the_screen_width():
    """手機:收起來、摘要可以按;換成電腦的寬度:打開,摘要只是標題(按了不收,Tab 也不停在上面)。"""
    import json
    import shutil
    import subprocess

    from web.render import STATIC_DIR

    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("沒有 node,略過前端流程測試")
    script = _COLLAPSE_FLOW.replace("__APP_JS__", json.dumps(str(STATIC_DIR / "app.js")))
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    assert json.loads(out.stdout) == {"phone": [False, 0, False], "computer": [True, -1, True]}
