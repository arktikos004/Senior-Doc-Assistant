"""首頁以要處理的事為主(使用者 11/5,六點的第 1 點):拍照永遠是主要按鈕;清單分成已逾期、快到期、需要家人協助;
長輩不帶到他按不了的頁面;已完成收起來。v5 車站時刻表(11/6):各組排在一塊深墨看板上,每列四格:日期翻牌、
「電費 1,286 元」、狀態牌、三個固定格子的按鈕(「看文件」或「去確認」|「已繳費」|「取消提醒」)。不呼叫模型。"""
import re
from datetime import date, timedelta

from fastapi.testclient import TestClient

from samples import BILL
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import create_app
from web.render import STATIC_DIR, reminder_rows

SOON = (date.today() + timedelta(days=10)).isoformat()
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def _t1_css():
    """app.css 最後「T1 首頁與要記得的事」那一段(v5 新規則只寫在這裡)。"""
    return CSS.split("/* ---- T1 首頁與要記得的事 ---- */")[1].split("/* (T1 首頁與要記得的事的規則寫在這一行上面) */")[0]


def _as(cfg, store, role):
    """用家庭成員的身分開網頁(role:elder 長輩、family 家人)。"""
    email = f"{role}@example.test"
    store.set_member(email, role, "媽媽" if role == "elder" else "女兒")
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    return TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))


def _primary(html):
    """頁面上主要按鈕(btn--primary)的字;「開始辨識」要選好照片才看得到(app.css 的 data-ready),不算。"""
    found = re.findall(r'class="btn btn--primary[^"]*"[^>]*>(.*?)</(?:a|label|button)>', html, re.S)
    return [text for text in (re.sub(r"<[^>]+>", "", m).strip() for m in found) if text != "開始辨識"]


def _row_links(html):
    """「要記得的事」每一列的「看文件」或「去確認」連到哪裡。"""
    return re.findall(r'<a class="btn btn--secondary btn--done" href="([^"]+)">', html)


def _groups(html):
    """畫面上的分組標題,照順序。"""
    return re.findall(r'<h3 class="rgroup__title">([^<]+)</h3>', html)


def _board(html):
    """「要記得的事」那一塊深墨看板(首頁到「最近看過的文件」為止,/reminders 到收起來的區塊或頁尾為止)。"""
    start = html.index('<div class="board board--remind">')
    ends = [html.find(mark, start) for mark in ('id="recent-title"', '<details class="section fold">', "</main>")]
    return html[start:min(i for i in ends if i != -1)]


def _rows(html):
    """看板上的每一列,照順序。"""
    return re.findall(r'<li class="ritem">(.*?)</li>', _board(html), re.S)


def _flap(row):
    """這一列日期格的翻牌數字;空白的暗牌是空字串。"""
    cell = row.split('<div class="ritem__date">')[1].split('<div class="ritem__main">')[0]
    return "".join(re.findall(r'<span class="flap__d">([^<]*)</span>', cell))


def _pending(store, add_doc):
    """一件等家人確認的期限、一份待複核的文件;回傳 (期限那份的文件編號, 期限的行動編號)。"""
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "confirm", {"title": "繳電費", "date": SOON})
    add_doc(dict(BILL), action="review", reason="模型自評信心 0.50（未經驗證）低於門檻 0.80，需人工確認")
    return doc_id, action_id


def test_the_camera_is_the_main_button_for_everyone(cfg, store, add_doc):
    """拍照永遠在最上面而且醒目(六點的第 1 點):家人有待辦也一樣,要處理的事放在清單的「需要家人協助」。"""
    _pending(store, add_doc)
    for role in ("family", "elder"):
        html = _as(cfg, store, role).get("/").text
        assert _primary(html) == ["拍照讀文件"], role
        assert 'class="btn btn--primary btn--lg source" for="file"' in html
        assert html.index('class="panel upload"') < html.index('id="remind-title"') < html.index('id="recent-title"')


def test_family_help_group_sends_family_to_where_they_can_act(cfg, store, add_doc):
    """家人:等確認的列是「去確認」,直接到那一件;待複核的合成一列「去複核」。選單的紅色數字照舊。"""
    _doc_id, action_id = _pending(store, add_doc)
    html = _as(cfg, store, "family").get("/").text
    assert _groups(html) == ["需要家人協助"]
    assert _row_links(html) == ["/review", f"/confirm?item={action_id}"]
    assert "1 份文件等您複核" in html and ">去複核<" in html and ">去確認<" in html
    assert '2<span class="visually-hidden"> 件待處理' in html


def test_elder_is_not_sent_to_pages_they_cannot_use(cfg, store, add_doc):
    """長輩不能確認或複核:等家人確認的列是「看文件」,連到那份文件(首頁與查看全部);沒有待複核那一列、沒有紅色數字。"""
    doc_id, _action_id = _pending(store, add_doc)
    elder = _as(cfg, store, "elder")
    html = elder.get("/").text
    assert _groups(html) == ["需要家人協助"] and "件待處理" not in html and "等您複核" not in html
    assert _row_links(html) == [f"/doc/{doc_id}"] and ">看文件<" in html and ">去確認<" not in html
    assert _row_links(elder.get("/reminders").text) == [f"/doc/{doc_id}"]
    assert "等待家人確認" in elder.get(f"/doc/{doc_id}").text         # 打開文件看得到還在等家人


def test_each_row_says_what_how_much_and_by_when_with_two_buttons(client, store, add_doc):
    """每筆寫事項與金額「電費 1,854 元」,旁邊有「看文件」「已繳費」(六點的第 1 點);整列不再是連結。
    v5 時刻表(11/6):期限畫成翻牌加「前」,標題不再接「｜11/20 前」;翻牌只給眼睛看,讀屏念藏起來的「11/20 前」。"""
    due = date.today() + timedelta(days=2)
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": due.isoformat()})
    html = client.get("/").text
    [row] = _rows(html)
    assert '<p class="ritem__title">電費 1,854 元</p>' in row and "｜" not in _board(html)
    assert _flap(row) == f"{due.month:02d}{due.day:02d}" and '<span class="flap" aria-hidden="true">' in row
    when = f"{due.month}/{due.day} 前" if due.year == date.today().year else f"{due.year}/{due.month}/{due.day} 前"
    assert f'<span class="visually-hidden">{when}</span>' in row
    assert '<span class="ritem__before" aria-hidden="true">前</span>' in row
    assert _groups(html) == ["快到期"] and '<span class="tile tile--wait">還有 2 天</span>' in row
    assert f'href="/doc/{doc_id}">看文件' in html and f'action="/reminder/{action_id}/done"' in html
    assert "ritem__link" not in html


def test_reminders_are_one_timetable_board_on_home_and_the_all_page(client, store, add_doc):
    """v5 時刻表(11/6):各組排在同一塊深墨看板上,組名在看板裡;/reminders 用同一個巨集,一起變。
    首頁的「查看全部」在看板下面置中。"""
    for offset, tier in ((-1, "auto"), (2, "auto"), (10, "confirm"), (40, "auto"), (41, "auto"), (42, "auto"), (43, "auto")):
        store.add_action(add_doc(dict(BILL)), "calendar", tier,
                         {"title": "繳電費", "date": (date.today() + timedelta(days=offset)).isoformat()})
    home = client.get("/").text
    for html in (home, client.get("/reminders").text):
        assert html.count('<div class="board board--remind">') == 1
        assert _groups(_board(html)) == ["已逾期", "快到期", "需要家人協助", "其他"]
    assert '<p class="more more--center"><a href="/reminders">查看全部 7 筆</a></p>' in home


def test_each_row_is_four_cells_with_three_fixed_button_slots(cfg, store, add_doc):
    """使用者 11/6:「欄位要對齊 統一大小 不然看起來很亂」。每一列四格:日期|事項與金額|狀態牌|按鈕;
    狀態牌一律是同一種 .tile(一樣寬)。按鈕排在三個固定的格子:看文件或去確認|已繳費、已辦理或已讀|取消提醒,
    這一列沒有的空著,其他按鈕不挪位置。沒有日期的列是空白的暗牌,看板的節奏不斷。"""
    late = store.add_action(add_doc(dict(BILL)), "calendar", "auto",
                            {"title": "繳電費", "date": (date.today() - timedelta(days=1)).isoformat()})
    _pending(store, add_doc)
    overdue, review, waiting = _rows(_as(cfg, store, "family").get("/").text)
    for row in (overdue, review, waiting):
        cells = re.findall(r'<div class="(ritem__date|ritem__main|ritem__state|remind-acts)">', row)
        assert cells == ["ritem__date", "ritem__main", "ritem__state", "remind-acts"]
    assert '<span class="tile tile--manual">逾期 1 天</span>' in overdue
    acts = overdue.split('<div class="remind-acts">')[1]
    assert (acts.index('<a class="btn btn--secondary btn--done" href=')
            < acts.index(f'<form class="remind-acts__mark" method="post" action="/reminder/{late}/done">')
            < acts.index(f'<form class="remind-acts__off" method="post" action="/reminder/{late}">'))
    assert ">去複核<" in review and "remind-acts__" not in review              # 只有第一格,其他兩格空著
    assert _flap(review) == "" and '<span class="flap flap--blank" aria-hidden="true">' in review
    assert ">去確認<" in waiting and '<span class="tile tile--wait">等待家人確認</span>' in waiting
    t1 = _t1_css()
    assert ".board .remind-acts > a { grid-column: 1; }" in t1
    assert ".remind-acts__mark { grid-column: 2; }" in t1 and ".remind-acts__off { grid-column: 3; }" in t1


def test_rows_give_the_flap_its_month_and_day_and_a_year_only_when_it_is_not_this_year():
    """v5 時刻表(11/6):render 多給月、日、年三個值畫翻牌;不是今年的另外寫年份,才不會把去年的看成今年的。
    讀屏念的 when(「11/2 前」)照舊;日期讀不懂的不寫日期,三個值都是空的。"""
    deadline = lambda n, day: {"id": n, "document_id": 1, "kind": "calendar", "tier": "auto",  # noqa: E731
                               "status": "pending", "superseded": 0, "payload": {"title": "繳電費", "date": day}}
    rows = reminder_rows([{"id": 1, "result": dict(BILL)}],
                         [deadline(1, "2026-11-02"), deadline(2, "2027-01-05"), deadline(3, "不是日期")], date(2026, 11, 6))
    assert [(r["when"], r["due_year"], r["due_month"], r["due_day"]) for r in rows] == [
        ("11/2 前", "", 11, 2), ("2027/1/5 前", 2027, 1, 5), ("", "", "", "")]
    assert {r["headline"] for r in rows} == {"電費 1,854 元"}


def test_timetable_rows_and_slots_fit_the_board_width():
    """看板夠寬一列排完(日期|事項|狀態牌|三個按鈕格);平板按鈕換到下一行靠右、格子固定;手機三等分。
    看清單本身的寬度(容器查詢):字級放大時自己換成窄的排法。狀態欄照狀態牌的寬度(共用的 --tile-w)。"""
    t1 = _t1_css()
    wide = t1.split("@container (min-width: 62rem) {")[1].split("\n}\n")[0]
    assert 'grid-template-areas: "date main state acts"' in wide and "repeat(3, var(--slot-w))" in wide
    assert "var(--tile-w)" in wide
    tablet = t1.split("@container (min-width: 38rem) and (max-width: 61.99rem) {")[1].split("\n}\n")[0]
    assert '"acts acts acts"' in tablet and "repeat(3, var(--slot-w))" in tablet and "justify-content: end" in tablet
    phone = t1.split(".board .remind-acts {")[1].split("}")[0]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in phone


def test_home_title_is_only_for_screen_readers(client):
    """v5(11/6):首頁的 h1 留給讀屏,畫面上不顯示:黃色分頁已經寫了「首頁」,第一眼是上傳框。"""
    html = client.get("/").text
    main = html.split('<main id="main"')[1]
    assert '<h1 class="visually-hidden">首頁</h1>' in main and 'class="page-head"' not in main
    assert main.index("<h1") < main.index('class="panel upload"')


def test_completed_rows_line_up_in_three_columns(client, store, add_doc):
    """已完成(v5,11/6):事項|誰、哪天標示|改回,三欄對齊;手機上改回排在右邊,事項與誰按的上下兩行。"""
    action_id = store.add_action(add_doc(dict(BILL)), "calendar", "auto", {"title": "繳電費", "date": SOON})
    store.set_action_status(action_id, "completed", event="complete_reminder", actor="elder@example.test", role="elder")
    fold = client.get("/").text.split('<details class="section fold">')[1]
    assert fold.index('class="done-what"') < fold.index('class="done-who"') < fold.index("改回未繳")
    rule = CSS.split(".done-list li {")[1].split("}")[0]
    assert "display: grid" in rule and "grid-template-columns: minmax(0, 1fr) minmax(0, 15rem) 6.5rem" in rule
    assert 'grid-template-areas: "what undo" "who undo"' in _t1_css()


def test_groups_come_in_the_agreed_order_and_nothing_falls_out(client, store, add_doc):
    """已逾期 → 快到期 → 需要家人協助 → 其他;更遠的期限不在前三組,也不能消失。"""
    day = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731
    for title, offset, tier in (("很遠的", 40, "auto"), ("等確認的", 20, "confirm"), ("這週的", 7, "auto"),
                                ("下週的", 8, "auto"), ("過期的", -1, "auto")):
        store.add_action(add_doc(dict(BILL)), "calendar", tier, {"title": title, "date": day(offset)})
    html = client.get("/").text
    assert _groups(html) == ["已逾期", "快到期", "需要家人協助", "其他"]
    positions = [html.index(f"：{name}") for name in ("過期的", "這週的", "等確認的", "下週的", "很遠的")]
    assert positions == sorted(positions)


def test_completed_is_folded_at_the_bottom_of_the_home_page(client, store, add_doc):
    """已完成收起來,放在最近文件下面,首頁只留要處理的事;打開來看得到誰按的、可以改回。"""
    doc_id = add_doc(dict(BILL))
    action_id = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": SOON})
    store.set_action_status(action_id, "completed", event="complete_reminder", actor="elder@example.test", role="elder")
    html = client.get("/").text
    fold = html.split('<details class="section fold">')[1].split("</details>")[0]
    assert '<h2 id="done-title">已完成</h2>' in fold and "最近 7 天 1 筆" in fold and "改回未繳" in fold
    assert html.index('id="recent-title"') < html.index('id="done-title"')


def test_the_nav_bar_shows_whenever_it_fits():
    """導覽列放得下就排在品牌列,不收成漢堡(使用者 11/4);切換點照實測寬度,字級越大要越寬,放不下才用「選單」。"""
    from web.render import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for width, font in (("40rem", "standard"), ("45rem", "large"), ("50rem", "xlarge")):   # v5 頂列多了誰在用(11/6)
        block = css.split(f"@media (min-width: {width}) and (max-width: 63.99rem) {{")[1].split("\n}\n")[0]
        assert f'html[data-font="{font}"] .side__nav {{ display: flex; }}' in block
        assert f'html[data-font="{font}"] .menu {{ display: none; }}' in block
    assert ".side__nav { flex-wrap: wrap;" in css                      # 萬一還是太擠就換行,不藏起來


def test_nav_counts_are_small_red_badges_for_family_only(cfg, store, add_doc):
    """分頁的待處理數字是名稱右上角的紅色小標(使用者 11/5):不佔位置,分頁的字照舊在同一個地方;
    長輩不能確認或複核,不放紅色數字催他(同手機選單的小圓點)。"""
    from web.render import STATIC_DIR

    _pending(store, add_doc)
    family = _as(cfg, store, "family").get("/").text
    nav = family.split('<nav class="side__nav" aria-label="主要">')[1].split("</nav>")[0]
    assert '<span class="tab">確認<span class="nav-badge">1<span class="visually-hidden"> 件待確認</span></span></span>' in nav
    assert '<span class="tab">複核<span class="nav-badge">1<span class="visually-hidden"> 份待複核</span></span></span>' in nav
    elder = _as(cfg, store, "elder").get("/").text
    assert "nav-badge" not in elder and "件待確認" not in elder and "份待複核" not in elder
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    badge = css.split(".nav-badge {")[1].split("}")[0]
    assert "position: absolute" in badge and "background: var(--red-solid)" in badge
    # 使用者看過後:「紅點數字 在小一點」→ 分頁與選單按鈕的小標一樣大:16px 高、11px 字
    menu = css.split(".menu__badge {")[1].split("}")[0]
    for rule in (badge, menu):
        assert "height: 1rem;" in rule and "font-size: .6875rem;" in rule


def test_wide_screens_center_the_whole_page():
    """大螢幕上內容置中(使用者 11/5),最寬 74rem;v5 的頂列全寬,字標與分頁和內容的左右邊對齊(11/6)。"""
    from web.render import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    main = css.split(".main {")[1].split("}")[0]
    assert "margin-inline: auto" in main and "max-width: 74rem" in main
    side = css.split(".side {")[1].split("}")[0]
    assert "position: sticky" in side and "calc((100% - 74rem) / 2 + var(--s12))" in side


def test_top_bar_shows_the_flap_wordmark_and_who_is_using_it(client):
    """v5 頂列(11/6):字標是「看」「有」兩塊翻牌(只給眼睛看,讀屏念品牌名),誰在用從內容上面搬到頂列。"""
    html = client.get("/").text
    bar = html.split('<aside class="side">')[1].split("</aside>")[0]
    assert '<span class="brandflap" aria-hidden="true"><span class="brandflap__d">看</span><span class="brandflap__d">有</span></span>' in bar
    assert '<span class="visually-hidden" translate="no">看有</span>' in bar
    main = html.split('<main id="main"')[1]
    assert 'class="identity"' not in main
    assert "/static/logo.svg" not in html and "/static/favicon-32.png" in html


def test_shared_form_field_floats_its_label_and_marks_the_eye_button():
    """表單元件(_field.html):浮動標籤(輸入框在前、標籤在後,placeholder 只放空白)、錯誤寫在框的正下方、
    密碼的眼睛在框內右邊,沒有 JS 時藏著。PDF 密碼頁是第一個用它的表單。"""
    from web.render import TEMPLATES_DIR

    html = (TEMPLATES_DIR / "upload_password.html").read_text(encoding="utf-8")
    assert '{% call ff("pdf-password", "PDF 密碼"' in html and 'placeholder=" "' in html
    assert 'class="ff__eye" type="button" data-show-password="pdf-password"' in html and " hidden>" in html
    macro = (TEMPLATES_DIR / "_field.html").read_text(encoding="utf-8")
    assert macro.index("{{ caller() }}") < macro.index('<label class="ff__label"') < macro.index('class="ff__error"')
