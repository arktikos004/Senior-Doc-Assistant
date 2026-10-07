"""Web 路由測試(FastAPI TestClient;資料夾隔離在 tmp_path,不呼叫模型)。

更正讀值與待複核的完整流程在 tests/test_web_correct.py。
"""
import pytest
from fastapi.testclient import TestClient

import web.app as web_app


def test_index_has_accessible_skeleton(client):
    html = client.get("/").text
    assert 'lang="zh-Hant-TW"' in html
    assert 'href="#main"' in html          # 跳到主要內容
    assert '<main id="main"' in html


def test_index_shows_status_message(client, flash_text):
    assert flash_text(client.get("/?msg=已更正").text) == "已更正"
    assert flash_text(client.get("/").text) is None


def test_model_text_in_review_queue_is_escaped(client, review_doc):
    # 待複核清單讀 SQLite:模型讀到的文字(這裡是類型)一律跳脫
    review_doc({"doc_type": "<script>x", "confidence": 0.5})
    html = client.get("/review").text
    assert "<script>x" not in html
    assert "&lt;script&gt;x" in html


@pytest.mark.parametrize("method, url", [
    ("get", "/review/20261001-101500-abcd1234.png"), ("get", "/file/20261001-101500-abcd1234.png"),
    ("get", "/file/..%5Cevil.jpg"), ("post", "/approve/20261001-101500-abcd1234.png"),
    ("post", "/reject/20261001-101500-abcd1234.png"),
])
def test_filename_routes_are_gone(client, review_doc, method, url):
    """以檔名操作的舊路由(F7 前)都移除了:待複核改以文件 ID 操作,原件只走 /doc/{id}/file。"""
    review_doc()
    r = getattr(client, method)(url)
    assert r.status_code == 404 and "找不到這個頁面" in r.text


def test_page_error_brings_its_own_next_step(app):
    """錯誤頁的下一步跟著錯誤走:PageError 可以自己指定;沒指定的部分沿用狀態碼的預設。"""
    @app.get("/test-own-step")
    def own_step():
        raise web_app.PageError(409, "這份文件已經有人改過了", next_url="/review", next_label="回待複核",
                                hint="請回待複核看最新的內容。")

    @app.get("/test-default-step")
    def default_step():
        raise web_app.PageError(409, "這個事項已經處理過了")

    client = TestClient(app)
    own = client.get("/test-own-step")
    assert own.status_code == 409 and "錯誤代碼 409。請回待複核看最新的內容。" in own.text
    assert '<a class="btn btn--primary btn--lg" href="/review">' in own.text and "回待複核</a>" in own.text
    default = client.get("/test-default-step").text
    assert "請回「家人確認」看最新狀態" in default
    assert '<a class="btn btn--primary btn--lg" href="/confirm">' in default and "回家人確認</a>" in default


def test_static_css_served(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200 and "--primary" in r.text


def test_review_shows_verified_confidence_not_self_rating(client, review_doc):
    """待複核只把依核對算出的驗證信心當可信度;AI 自評不出現(原則 1)。原因用白話,不用工程用語。"""
    review_doc({"doc_type": "發票", "confidence": 0.86, "verified_confidence": 0.1}, name="a.png")
    review_doc({"doc_type": "收據", "confidence": 0.5}, name="b.png")
    listing = client.get("/review").text
    assert "驗證信心" in listing and "10%" in listing and "沒有可核對的項目" in listing
    assert "0.86" not in listing and "AI 自評" not in listing
    assert "有必要的欄位沒讀到（日期、金額）" in listing and "低於門檻" not in listing
    assert "a.png" not in listing and "上傳" in listing            # 列上傳時間,不列系統產生的檔名


def test_status_message_is_whitelisted(client, flash_text):
    """?msg= 只顯示系統訊息,任意文字不回顯(避免偽造公告)。"""
    html = client.get("/?msg=請立刻匯款到這個帳戶").text
    assert "請立刻匯款" not in html and flash_text(html) is None


def test_redirect_messages_come_from_the_whitelist():
    """轉址帶的訊息都在白名單內(不在的話頁面不會顯示);新增訊息忘了登記,轉址時就直接失敗。"""
    used = {web_app.MSG_CORRECTED, web_app.MSG_REJECTED, web_app.MSG_CATEGORY} | {
        msg for _, msg in (*web_app._CONFIRM_DECISIONS.values(), *web_app._REMINDER_DECISIONS.values())} | {
        web_app.MSG_SETTINGS_SAVED, web_app.MSG_SETTINGS_UNCHANGED, web_app.MSG_PURGED, web_app.MSG_PURGED_PARTLY,
        web_app.MSG_UPLOAD_CANCELLED}
    assert used == web_app._MESSAGES
    r = web_app._redirect_with_msg("/review", web_app.MSG_REJECTED)
    assert r.status_code == 303 and r.headers["location"] == "/review?msg=%E5%B7%B2%E9%80%80%E5%9B%9E"
    with pytest.raises(AssertionError):
        web_app._redirect_with_msg("/", "請立刻匯款")


def test_review_list_does_not_blame_ai(client):
    """轉人工是程式依固定規則決定的(使用者 10/2 決定):待複核頁的說明不寫成「AI 沒把握」。"""
    html = client.get("/review").text
    assert "核對不符、缺少必要欄位或讀值沒把握" in html and "AI 沒把握" not in html
