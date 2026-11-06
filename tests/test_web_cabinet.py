"""文件四大類的網頁測試:文件櫃(卡片份數、只看一類、依大類分組、未分類含舊資料、身分證明的說明)、
家人在更正頁指定大類、結果頁麵包屑帶出大類。

資料夾隔離在 tmp_path,不連網、不呼叫模型;文件都是合成資料,直接寫進資料庫或經 MockAnalyzer 上傳。
"""
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.config import AppConfig, PathsConfig
from src.models import CATEGORIES, ExtractionResult
from src.providers import MockAnalyzer
from test_web_correct import ISSUED, _bill, _letter, browser_form
from test_web_security import assert_security_headers
from test_web_static import _problems
from web.app import create_app



@pytest.fixture
def filed(store):
    """回傳 filed(doc_type, category, vendor=...) → 文件 ID:一份已存檔的文件,處理紀錄帶「類別」(None = 舊資料)。"""
    def add(doc_type: str, category: str | None, vendor: str = "範例商家") -> int:
        record = {"原始檔案": "20261002-101500-abcd1234.png", "動作": "archive", "原因": "", "目標路徑": None,
                  "AI辨識結果": {"doc_type": doc_type, "vendor": vendor}, "錯誤": None}
        if category is not None:
            record["類別"] = category
        return store.add_document(record)
    return add


def _cards(html: str) -> list[tuple[str, int, bool]]:
    """四張大類卡片:(名稱, 份數, 是不是選中的那一類)。"""
    found = re.findall(r'<a class="cat" href="[^"]+"( aria-current="true")?>.*?<b>(.*?)</b>.*?'
                       r'<span class="n">(\d+)<small>', html, re.S)
    return [(name, int(count), bool(current)) for current, name, count in found]


def _sections(html: str) -> list[tuple[str, int]]:
    """清單的分組標題:(類別, 份數)。"""
    found = re.findall(r'<h2 id="cat-title-\d+">(.*?)</h2><span class="muted">(\d+) 份</span>', html)
    return [(name, int(count)) for name, count in found]


def _links(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'<a class="tbl__row" href="/doc/(\d+)">', html)]


def test_cards_count_each_category_and_everything_is_grouped(client, filed):
    """沒選類別:卡片數四大類的份數;清單依大類分組列出全部,空的組不列,「未分類」放最後(舊資料也在這裡)。"""
    bill = filed("帳單", "生活契約", "範例電力公司")
    bag = filed("藥袋", "醫療與保險", "範例診所")
    letter = filed("公文", "財產資產", "範例市稅務局")
    invoice = filed("發票", "財產資產", "範例超市")
    loose = filed("公文", "未分類", "範例區公所")
    old = filed("發票", None, "舊資料商店")              # 10/2 以前的文件沒有類別
    html = client.get("/cabinet").text
    assert "<h1>文件櫃</h1>" in html
    assert _cards(html) == [("身分證明", 0, False), ("財產資產", 2, False), ("醫療與保險", 1, False),
                            ("生活契約", 1, False)]
    assert _sections(html) == [("財產資產", 2), ("醫療與保險", 1), ("生活契約", 1), ("未分類", 2)]
    assert _links(html) == [invoice, letter, bag, bill, old, loose]   # 每組新的在前
    unsorted = html.split(">未分類</h2>")[1]
    assert "舊資料商店" in unsorted and "已存檔" in unsorted         # 沿用「最近看過的文件」的列


def test_choosing_a_category_lists_only_that_category(client, filed):
    bag = filed("藥袋", "醫療與保險", "範例診所")
    letter = filed("公文", "醫療與保險", "範例健保署")
    filed("帳單", "生活契約")
    html = client.get("/cabinet?cat=醫療與保險").text
    assert _cards(html)[2] == ("醫療與保險", 2, True) and sum(current for *_, current in _cards(html)) == 1
    assert _sections(html) == [("醫療與保險", 2)]
    assert _links(html) == [letter, bag]
    assert 'href="/cabinet?cat={}" aria-current="true"'.format(quote("醫療與保險")) in html


def test_unsorted_filter_includes_old_documents_and_unknown_values_show_everything(client, filed):
    old = filed("發票", None)
    loose = filed("公文", "未分類")
    bill = filed("帳單", "生活契約")
    unsorted = client.get("/cabinet", params={"cat": "未分類"}).text
    assert _sections(unsorted) == [("未分類", 2)] and _links(unsorted) == [loose, old]
    assert 'aria-current="true"' not in unsorted                       # 未分類沒有卡片
    for value in ("亂填", "其他", "<script>alert(1)</script>", ""):
        html = client.get("/cabinet", params={"cat": value}).text
        assert sorted(_links(html)) == sorted([old, loose, bill]) and 'aria-current="true"' not in html
        assert _problems(html) == [] and "alert(1)" not in html        # 不認得的值不回顯


def test_empty_category_says_how_to_fill_it(client, filed):
    filed("帳單", "生活契約")
    html = client.get("/cabinet?cat=財產資產").text
    assert _sections(html) == [("財產資產", 0)] and _links(html) == []
    assert "這一類還沒有文件。" in html and "上傳時選" not in html      # 上傳不選大類了(使用者 11/4)


def test_empty_cabinet_invites_first_upload(client):
    html = client.get("/cabinet").text
    assert "還沒有文件。拍下第一張，結果會出現在這裡。" in html
    assert [count for _, count, _ in _cards(html)] == [0, 0, 0, 0]


def test_cabinet_page_is_safe_and_only_has_links(client, filed):
    """文件文字一律跳脫;沒有行內 JS;頁面只有連結(點一類只看那一類)與找文件的 GET 表單,沒有會改資料的表單。"""
    filed("藥袋", "醫療與保險", "<script>alert(1)</script>")
    r = client.get("/cabinet")
    assert r.status_code == 200
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"
    assert _problems(r.text) == [] and "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
    forms = re.findall(r"<form\b[^>]*>", r.text.split('<main id="main"')[1])
    assert forms == ['<form class="search" method="get" action="/cabinet" role="search">']
    for category in CATEGORIES:
        assert f'<a class="cat" href="/cabinet?cat={quote(category)}"' in r.text
    assert '<span class="visually-hidden" translate="no">看有</span>' in r.text and "文件櫃 - 看有 高齡家庭文書輔助</title>" in r.text


# ---- 家人指定大類(更正頁)與結果頁的麵包屑 ------------------------------------------------

@pytest.fixture
def stored(cfg, store, png_bytes):
    """回傳 stored(result, category=None) → 文件 ID:完整讀值的已存檔文件,原檔在 archive/(可以更正)。"""
    def add(result: dict, category: str | None = None) -> int:
        path = cfg.paths.archive / f"20261002-1015{len(list(cfg.paths.archive.iterdir())):02d}-abcd1234.png"
        path.write_bytes(png_bytes())
        record = {"原始檔案": path.name, "動作": "archive", "原因": "", "目標路徑": str(path),
                  "AI辨識結果": result, "錯誤": None}
        if category is not None:
            record["類別"] = category
        return store.add_document(record)
    return add


def _crumbs(html: str) -> str:
    return re.search(r'<nav class="crumbs" aria-label="位置">(.*?)</nav>', html, re.S).group(1)


def _category_options(html: str) -> tuple[list[str], list[str]]:
    """「這份文件屬於哪一類」的下拉:(全部的選項, 選中的)。"""
    select = re.search(r'<select[^>]*name="category"[^>]*>(.*?)</select>', html, re.S).group(1)
    return re.findall(r'<option value="([^"]+)"', select), re.findall(r'<option value="([^"]+)" selected', select)


def _form_of(client, doc_id: int) -> dict:
    return browser_form(client.get(f"/doc/{doc_id}/correct").text, f"/doc/{doc_id}/correct")


def test_result_page_breadcrumb_names_the_category(client, filed):
    """結果頁:首頁 / 大類 / 文件,大類連到文件櫃的這一類;舊資料沒有類別就是未分類。"""
    bag = filed("藥袋", "醫療與保險")
    old = filed("公文", None)
    sep = '<span aria-hidden="true">/</span>'
    assert _crumbs(client.get(f"/doc/{bag}").text) == (
        f'<a href="/">首頁</a>{sep}<a href="/cabinet?cat={quote("醫療與保險")}">醫療與保險</a>{sep}<span>藥袋</span>')
    assert f'<a href="/cabinet?cat={quote("未分類")}">未分類</a>' in _crumbs(client.get(f"/doc/{old}").text)


def test_correction_page_offers_the_categories_with_the_current_one_chosen(client, stored):
    doc_id = stored(_bill(), category="生活契約")
    page = client.get(f"/doc/{doc_id}/correct").text
    assert "這份文件屬於哪一類" in page
    assert _category_options(page) == ([*CATEGORIES, "未分類"], ["生活契約"])
    form = re.search(rf'<form\b[^>]*action="/doc/{doc_id}/correct"[^>]*>(.*?)</form>', page, re.S).group(1)
    assert 'name="category"' in form and 'name="csrf_token"' in form   # 同一張表單,帶 CSRF token
    old = stored(_bill(), category=None)                                 # 舊資料:預設未分類
    assert _category_options(client.get(f"/doc/{old}/correct").text)[1] == ["未分類"]


def test_letter_without_category_is_unsorted_until_the_family_files_it(cfg, store, form_client, png_bytes):
    """驗收:公文上傳時沒選大類 → 未分類;家人在更正頁選「財產資產」存檔 → 文件櫃、麵包屑都跟著變。"""
    class LetterAnalyzer:
        def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
            return ExtractionResult(**_letter(date=ISSUED.isoformat(), unreadable=[]))

    client = form_client(create_app(cfg, analyzer=LetterAnalyzer()))
    r = client.post("/upload", files={"file": ("a.png", png_bytes(), "image/png")}, data={"doc_type": "公文"},
                    follow_redirects=False)
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert store.get_document(doc_id)["category"] == "未分類"
    assert ">未分類</a>" in _crumbs(client.get(f"/doc/{doc_id}").text)
    assert _links(client.get(f"/cabinet?cat={quote('未分類')}").text) == [doc_id]

    data = _form_of(client, doc_id)
    assert data["category"] == "未分類"
    data["category"] = "財產資產"
    r = client.post(f"/doc/{doc_id}/correct", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{doc_id}?msg=")
    assert store.get_document(doc_id)["category"] == "財產資產"
    assert ">財產資產</a>" in _crumbs(client.get(r.headers["location"]).text)
    cabinet = client.get("/cabinet").text
    assert _cards(cabinet)[1] == ("財產資產", 1, False) and _sections(cabinet) == [("財產資產", 1)]
    assert [d["id"] for d in store.list_documents()] == [doc_id]          # 更新同一筆,沒有新增


def test_category_choice_is_kept_but_not_saved_when_the_form_has_errors(client, store, stored):
    doc_id = stored(_bill(), category="生活契約")
    data = {**_form_of(client, doc_id), "amount": "", "category": "財產資產"}
    r = client.post(f"/doc/{doc_id}/correct", data=data)
    assert r.status_code == 400 and "這一欄必填" in r.text
    assert _category_options(r.text)[1] == ["財產資產"]
    assert store.get_document(doc_id)["category"] == "生活契約"             # 沒存檔就不改類別


def test_adding_a_medicine_keeps_the_chosen_category(client, store, stored):
    from test_web_correct import _bag

    doc_id = stored(_bag(), category="醫療與保險")
    data = {**_form_of(client, doc_id), "category": "未分類", "add_item": "1"}
    page = client.post(f"/doc/{doc_id}/correct", data=data)
    assert page.status_code == 200 and "第 3 種藥" in page.text
    assert _category_options(page.text)[1] == ["未分類"]
    assert store.get_document(doc_id)["category"] == "醫療與保險"


@pytest.mark.parametrize("value", ["其他", "亂填的類別", "<script>alert(1)</script>", "財產資產 ", "", None])
def test_unknown_category_keeps_the_current_one(client, store, stored, value):
    """更正頁只收四大類與未分類的原字串;其他值(或舊頁面沒有這一欄)不改類別,讀值照常存檔。"""
    doc_id = stored(_bill(), category="生活契約")
    data = _form_of(client, doc_id)
    if value is None:
        data.pop("category")
    else:
        data["category"] = value
    r = client.post(f"/doc/{doc_id}/correct", data={**data, "vendor": "改過的開單單位"}, follow_redirects=False)
    assert r.status_code == 303
    doc = store.get_document(doc_id)
    assert doc["category"] == "生活契約" and doc["result"]["vendor"] == "改過的開單單位"


def test_changing_only_the_category_keeps_reminders_and_reading(client, store, stored):
    """只改類別:不重新核對、不換行動——已取消的提醒維持取消、讀值不動、不寫更正紀錄(10/3 整合修正)。"""
    doc_id = stored(_bill(), category="生活契約")
    aid = store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15"}, status="rejected")
    before = store.get_document(doc_id)["result"]
    r = client.post(f"/doc/{doc_id}/correct", data={**_form_of(client, doc_id), "category": "財產資產"},
                    follow_redirects=False)
    assert r.status_code == 303 and quote("已更新類別") in r.headers["location"]
    assert store.get_document(doc_id)["category"] == "財產資產"
    assert [(a["id"], a["status"]) for a in store.list_actions(document_id=doc_id)] == [(aid, "rejected")]
    assert store.get_document(doc_id)["result"] == before
    assert store.list_corrections(verified_only=False) == []


def test_identity_documents_say_why_they_wait_for_review(client, store, cfg, png_bytes):
    """選了身分證明的文件一律轉人工,原因照實寫,不說成 AI 沒把握。"""
    path = cfg.paths.review / "20261003-010000-abcd1234.png"
    path.write_bytes(png_bytes())
    doc_id = store.add_document({"原始檔案": path.name, "動作": "review", "原因": "身分證明類文件交給家人對照原件確認",
                                 "目標路徑": str(path), "AI辨識結果": {"doc_type": "收據", "date": "2026-10-01",
                                                                     "amount": 100.0}, "錯誤": None, "類別": "身分證明"})
    reason = "身分證明類文件請家人對照原件確認。"
    assert reason in client.get("/review").text
    assert reason in client.get(f"/doc/{doc_id}").text
    assert reason in client.get(f"/doc/{doc_id}/correct").text      # 更正頁和待複核清單同一句


# ---- 上傳選項清單(10/3):畫面顯示家人選的名稱 ---------------------------------------------------

def _titles(html: str) -> list[str]:
    """文件清單(最近看過的文件、文件櫃、待複核)每一列的標題。"""
    return re.findall(r'<span class="tbl__title">(.*?)</span>', html)


def _labelled(store, label: str, category: str, result: dict, action: str = "archive") -> int:
    """一份上傳時選了清單名稱的文件(處理紀錄帶「使用者選的文件」),直接寫進資料庫。"""
    return store.add_document({"原始檔案": "20261003-101500-abcd1234.png", "動作": action, "原因": "",
                               "目標路徑": None, "AI辨識結果": result, "錯誤": None, "類別": category,
                               "使用者選的文件": label})


@pytest.mark.parametrize("category, name", [("醫療與保險", "保單"), ("身分證明", "身分證"), ("財產資產", "存摺")])
def test_unreadable_item_is_shown_by_its_name_everywhere(client, png_bytes, category, name):
    """驗收:選了交給家人確認的名稱上傳(MockAnalyzer 讀成發票)→ 結果頁大標、頁籤、麵包屑、原件說明、
    文件櫃、最近看過的文件、待複核清單、更正頁都顯示家人選的名稱;原因照實寫,不說成 AI 沒把握。"""
    r = client.post("/upload", files={"file": ("a.png", png_bytes(), "image/png")},
                    data={"category": category, "doc_type": name}, follow_redirects=False)
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    reason = f"「{name}」請家人對照原件確認。"
    page = client.get(f"/doc/{doc_id}").text
    assert f">這是{name}</h1>" in page and f"<title>{name} - 看有 高齡家庭文書輔助</title>" in page
    assert _crumbs(page).endswith(f"<span>{name}</span>") and f"您上傳的{name}原件" in page
    assert reason in page and "等待複核" in page
    assert _titles(client.get(f"/cabinet?cat={quote(category)}").text) == [name]
    assert _titles(client.get("/").text) == [name]
    review = client.get("/review").text
    assert _titles(review) == [name] and reason in review
    correct = client.get(f"/doc/{doc_id}/correct").text
    assert f"<h2>{name}</h2>" in correct and reason in correct


def test_bill_kind_keeps_the_heading_but_lists_keep_the_name(client, store):
    """結果頁大標:帳單有固定種類(電費…)時維持「電費帳單」,其他情況用家人選的名稱(稅單讀成種類「其他」的帳單);
    清單一律顯示家人選的名稱。"""
    from samples import BILL

    power = _labelled(store, "水電瓦斯費", "生活契約", dict(BILL))
    tax = _labelled(store, "稅單", "財產資產", {**BILL, "fields": {"due_date": "2099-10-15", "bill_kind": "其他"}})
    assert ">這是電費帳單</h1>" in client.get(f"/doc/{power}").text
    assert ">這是稅單</h1>" in client.get(f"/doc/{tax}").text
    assert _titles(client.get("/cabinet").text) == ["稅單", "水電瓦斯費"]     # 財產資產組在生活契約組前面
    assert _titles(client.get("/").text) == ["稅單", "水電瓦斯費"]


def test_family_confirm_card_names_the_chosen_document(client, store):
    from samples import BILL, REMINDER

    doc_id = _labelled(store, "管理費", "生活契約", {**BILL, "fields": {"due_date": "2099-10-15", "bill_kind": "其他"}},
                       action="review")
    store.add_action(doc_id, "calendar", "confirm", dict(REMINDER))
    assert '<p class="muted">管理費，' in client.get("/confirm").text


@pytest.mark.parametrize("label", ["<script>alert(1)</script>", "亂填的名稱", "保單 "])
def test_names_outside_the_list_are_never_shown(client, store, label):
    """名稱一律從固定清單取:資料庫裡不是清單名稱的值(被改過、舊版留下的)不顯示,標題照讀到的類型。"""
    doc_id = _labelled(store, label, "醫療與保險", {"doc_type": "收據", "date": "2026-10-01", "amount": 100.0},
                       action="review")
    page = client.get(f"/doc/{doc_id}").text
    assert ">這是收據</h1>" in page and label not in page and "alert(1)" not in page
    assert "請家人對照原件確認" not in page
    assert _titles(client.get("/review").text) == ["收據"]


def test_list_icon_follows_the_title():
    """清單前面的小圖示跟著標題走:家人選的名稱對應的類型;不能自動判讀的用一般文件圖示;不是清單名稱就照讀到的類型。"""
    from web.render import doc_icon

    assert doc_icon({"doc_label": "保單", "result": {"doc_type": "發票"}}) == "file"
    assert doc_icon({"doc_label": "稅單", "result": {"doc_type": "帳單"}}) == "bill"
    assert doc_icon({"doc_label": "醫療收據", "result": {"doc_type": "發票"}}) == "receipt"
    assert doc_icon({"doc_label": "亂填", "result": {"doc_type": "發票"}}) == "qr"
    assert doc_icon({"result": {"doc_type": "藥袋"}}) == doc_icon({"doc_type": "藥袋"}) == "pill"


# ---- v5 車站時刻表(使用者 11/6:「欄位要對齊 統一大小 不然看起來很亂」) ---------------------------------

def css_rule(selector: str, *, nested: bool = False) -> str:
    """app.css 裡這個選擇器的規則內容;寫了好幾次時取最後一條(後面的蓋過前面的)。
    nested=False 只找從行首寫起的規則,nested=True 只找寫在 @media 裡(縮排)的。"""
    from web.render import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    indent = "[ \t]+" if nested else ""
    found = re.findall(rf"^{indent}{re.escape(selector)} \{{([^}}]*)\}}", css, re.M)
    assert found, selector
    return found[-1]


def test_doc_table_lines_up_title_time_and_status(client, add_doc, review_doc):
    """文件表格(首頁「最近看過的文件」與文件櫃共用)每一列都是標題、時間、狀態三欄,上下對齊:
    標題是深墨加底線的連結;狀態一樣寬、字置中,要注意的是撐滿那一格的實心小牌(黃:等待複核,紅:讀不出來),
    沒事的「已存檔」是淡字。手機上時間一欄固定寬,狀態接在後面,照樣對齊。"""
    add_doc({"doc_type": "帳單", "vendor": "範例電力公司"})
    review_doc()
    add_doc(None, action="failed")
    for url in ("/cabinet", "/"):
        table = re.search(r'<div class="tbl tbl--docs">(.*?)</div>', client.get(url).text, re.S).group(1)
        rows = re.findall(r"<li>(.*?)</li>", table, re.S)
        assert len(rows) == 3, url
        for row in rows:
            assert re.findall(r'class="(tbl__title|tbl__when|tbl__status)"', row) == ["tbl__title", "tbl__when", "tbl__status"]
        states = {re.sub(r"<[^>]+>", "", text): cls for cls, text in
                  re.findall(r'<span class="tbl__status"><span class="pill pill--(\w+)">(.*?)</span></span>', table, re.S)}
        assert states == {"已存檔": "auto", "等待複核": "wait", "讀不出來": "manual"}, url
    title = css_rule(".tbl__title")
    assert "color: var(--link)" in title and "text-decoration: underline" in title
    assert "text-align: center" in css_rule(".tbl--docs .tbl__status")
    assert "display: block" in css_rule(".tbl--docs .tbl__status .pill--wait, .tbl--docs .tbl__status .pill--manual")
    assert "grid-template-columns: 7.5rem 6.5rem" in css_rule(".tbl--docs .tbl__meta", nested=True)


def test_category_counts_use_narrow_digits_and_the_chosen_one_is_yellow(client, filed):
    """四大類一類一格、一樣大;份數用窄體數字(--font-num)。選中的那一類和選中的選項一樣:月台黃底、深墨框與字。"""
    filed("帳單", "生活契約")
    assert _cards(client.get("/cabinet?cat=生活契約").text)[3] == ("生活契約", 1, True)
    cat = css_rule(".cat")
    assert "border: 1px solid var(--control)" in cat and "min-height: 6rem" in cat
    assert "font-family: var(--font-num)" in css_rule(".cat .n")
    chosen = css_rule('.cat[aria-current="true"]')
    assert "background: var(--signal)" in chosen and "border-color: #14212B" in chosen and "color: #14212B" in chosen
