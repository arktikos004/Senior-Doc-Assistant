"""置中(使用者 11/5):「搜尋框元件置中 不要靠左」「確認 退回也是置中 還有其他類似的元件也改成置中」,
接著選了表單底部的按鈕、結果頁的按鈕列與首頁上傳框。置中的是一塊內容最後的動作與獨立的輸入框;
清單每一列的按鈕跟著那一列,段落、清單與表單欄位的字照舊靠左。合成資料。"""
import re

import pytest

from web.render import STATIC_DIR, TEMPLATES_DIR

from samples import BILL

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def CENTER():
    """app.css 裡「置中」那一段(其他段落不受影響)。"""
    assert "/* ---- 置中(使用者 11/5" in CSS, "app.css 還沒有置中那一段"
    return CSS.split("/* ---- 置中(使用者 11/5")[1].split("/* (置中的規則寫在這一行上面) */")[0]


def _template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def test_search_button_is_a_magnifier_and_the_box_sits_in_the_middle(client, CENTER):
    """文件櫃的「找」改成放大鏡圖示,讀屏照樣念「找」;搜尋框整塊置中。"""
    html = client.get("/cabinet").text
    form = re.search(r'<form class="search"[^>]*>(.*?)</form>', html, re.S).group(1)
    button = re.search(r"<button[^>]*>(.*?)</button>", form, re.S)
    assert 'class="btn btn--secondary search__btn"' in button.group(0)
    assert button.group(1).startswith('<svg class="icon"') and button.group(1).endswith('<span class="visually-hidden">找</span>')
    assert ">找<" not in form.replace('<span class="visually-hidden">找</span>', "")
    assert ".search { margin-inline: auto; }" in CENTER
    assert ".search__btn .icon {" in CSS                       # 按鈕裡的圖示不能被「裝飾圖示都不顯示」藏起來


def test_confirm_and_reject_sit_in_the_middle(client, store, add_doc, CENTER):
    """家人確認的「確認」「退回」與下面的「看下一件」置中;都確認完了也置中。"""
    for title in ("繳電費", "繳水費"):
        store.add_action(add_doc(dict(BILL)), "calendar", "confirm", {"title": title, "date": "2026-12-01"})
    html = client.get("/confirm").text
    assert '<p class="confirm__more"><a class="confirm__next"' in html
    assert ".confirm__actions, .form-actions, .first__acts { justify-content: center; }" in CENTER
    assert ".confirm__done { justify-items: center; text-align: center; }" in CENTER
    assert ".confirm__wait, .confirm__more" in CENTER


def test_buttons_at_the_end_of_forms_sit_in_the_middle(CENTER):
    """表單底部的按鈕:更正頁、PDF 密碼、改類型、設定、確認頁的退路(.form-actions),對獎與家庭成員(單獨一顆),
    資料使用說明、結果頁最下面的退回與刪除、系統忙碌時的「重新辨識」(.end-action)。"""
    assert ".lot-form > .btn, .family-form > .btn { justify-self: center; }" in CENTER
    for name in ("correct.html", "upload_password.html", "retype.html", "settings.html", "ask.html"):
        assert 'class="form-actions"' in _template(name), name
    doc = _template("doc.html")
    assert doc.count('class="section end-action"') == 2 and '<form class="end-action" method="post" action="{{ v.retry_url }}">' in doc
    assert '<form class="section end-action" method="post" action="/data-use">' in _template("data_use.html")
    assert ".member-remove, .end-action" in CENTER


def test_result_page_buttons_sit_in_the_middle(CENTER):
    """結果頁的「聽重點」「查看原件」「請家人看一下」與最下面的「拍下一張」置中。"""
    assert ".first__acts" in CENTER.split("{ justify-content: center; }")[0]
    assert ".first__next" in CENTER


def test_home_upload_box_is_centred(CENTER):
    """首頁上傳框:標題、灰字與「拍照讀文件」「選照片」上下排、置中;選好後的縮圖、類型與「開始辨識」也置中。"""
    assert ".drop { flex-direction: column; gap: var(--s4); text-align: center; }" in CENTER
    assert ".drop__btns { flex-wrap: wrap; justify-content: center; }" in CENTER
    assert ".upload__row { flex-direction: column; align-items: center; }" in CENTER
    assert ".upload .chips { justify-content: center; }" in CENTER and ".upload .kinds legend { margin-inline: auto; }" in CENTER


def test_list_rows_keep_their_buttons_and_text_stays_left(CENTER):
    """清單每一列的按鈕跟著那一列(使用者沒有選),字也不改成置中。"""
    for kept in (".remind-acts", ".rlist", ".manual", ".tbl", ".kv", "body {"):
        assert kept not in CENTER, kept
