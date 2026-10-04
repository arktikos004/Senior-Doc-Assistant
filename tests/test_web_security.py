"""上線基本防護(W2-B)的測試:安全標頭、CSRF(資料夾隔離在 tmp_path,不連網、不呼叫模型)。"""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient

import web.app as web_app
from samples import BILL, REMINDER
from src.config import AppConfig
from web.app import CSRF_COOKIE, CSRF_COOKIE_HTTPS, CSRF_FIELD
from web.render import TEMPLATES_DIR

CSP_DIRECTIVES = ("default-src 'self'", "img-src 'self' blob:", "object-src 'none'",
                  "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'")
_POST_FORMS = re.compile(r'<form\b[^>]*\bmethod="post"[^>]*>(.*?)</form>', re.S | re.I)
_TOKEN_FIELD = re.compile(rf'name="{CSRF_FIELD}" value="([^"]+)"')


@pytest.fixture
def plain_client(app) -> TestClient:
    """不會自動帶 CSRF token 的 client:像剛打開網站,或被別的網站騙去送表單的瀏覽器。"""
    return TestClient(app)


@pytest.fixture
def add_original(cfg, add_doc):
    """回傳 add_original(name, content):原件放在 archive/ 內,結果頁與 /doc/{id}/file 才會送出它。"""
    def add(name: str, content: bytes) -> int:
        path = cfg.paths.archive / name
        path.write_bytes(content)
        return add_doc({"doc_type": "帳單", "fields": {}}, target=path)
    return add


def assert_security_headers(response, csp: bool = True) -> None:
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-frame-options"] == "DENY"
    policy = headers["permissions-policy"]
    assert "camera=(self)" in policy and "microphone=()" in policy and "geolocation=()" in policy
    if csp:
        for directive in CSP_DIRECTIVES:
            assert directive in headers["content-security-policy"], directive


# ---- 安全標頭 -------------------------------------------------------------------

# /doc/999 是 404 錯誤頁、/doc/abc 是 422(網址格式不對)錯誤頁
@pytest.mark.parametrize("url", ["/", "/confirm", "/review", "/doc/999", "/doc/abc"])
def test_html_pages_have_security_headers_and_are_not_stored(client, url):
    r = client.get(url)
    assert r.headers["content-type"].startswith("text/html")
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"      # 頁面含個資:瀏覽器與代理都不存


@pytest.mark.parametrize("url", ["/static/app.css", "/static/app.js", "/static/icon.svg"])
def test_static_files_have_security_headers_and_stay_cacheable(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert_security_headers(r)
    assert "no-store" not in r.headers.get("cache-control", "")   # 樣式與程式不含個資,可以快取


def test_oversize_page_from_middleware_has_security_headers(plain_client, monkeypatch):
    """413 是 middleware 在讀 body 之前就回的頁面,也要有標頭。"""
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 10)
    client = plain_client
    r = client.post("/upload", files={"file": ("a.png", b"\0" * (web_app._MULTIPART_SLACK + 100), "image/png")})
    assert r.status_code == 413
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"


def test_unexpected_error_is_chinese_page_with_headers(app, monkeypatch):
    """沒預期到的例外由最外層處理,不經過 security_headers;中文 500 頁要自己帶 CSP 等標頭,也不洩漏例外內容。"""
    def boom(*args, **kwargs):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(web_app, "reminder_rows", boom)
    r = TestClient(app, raise_server_exceptions=False).get("/")
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html")
    assert "系統出了點問題" in r.text and "錯誤代碼 500" in r.text and 'href="/"' in r.text
    assert "secret-internal-detail" not in r.text and "Internal Server Error" not in r.text
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"


def test_original_image_keeps_private_no_store(client, add_original, png_bytes):
    doc_id = add_original("bill.png", png_bytes())
    r = client.get(f"/doc/{doc_id}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, no-store"   # 原件(可能是藥袋)不給快取
    assert_security_headers(r)


def test_original_pdf_has_no_csp_so_the_browser_viewer_works(client, add_original):
    """瀏覽器內建的 PDF 閱讀器用行內樣式與 plugin,套上頁面的 CSP 會顯示不出來;其他標頭照加。"""
    doc_id = add_original("bill.pdf", b"%PDF-1.4\n%synthetic\n")
    r = client.get(f"/doc/{doc_id}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "content-security-policy" not in r.headers
    assert r.headers["cache-control"] == "private, no-store"
    assert_security_headers(r, csp=False)


@pytest.mark.parametrize("ext", AppConfig().supported_extensions)
def test_every_allowed_extension_checks_the_file_header(cfg, client, ext):
    """上傳 MIME 檢查:每個允許的副檔名都比對檔頭,改了副檔名的 HTML 一律擋下、不留檔。

    _looks_like 遇到不認得的副檔名會放行;以後在設定加新格式卻忘了補檔頭比對,這裡會抓到。
    """
    r = client.post("/upload", data={"doc_type": "不確定"},
                    files={"file": (f"fake{ext}", b"<!doctype html><script>alert(1)</script>", "image/png")})
    assert r.status_code == 400 and "對不上" in r.text
    assert not any(cfg.paths.uploads_path.iterdir())


def test_api_docs_are_not_published(client):
    """FastAPI 預設的 API 文件頁從 CDN 載入,違反 CSP 與離線原則;對外也不需要公開 API 結構。"""
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code == 404, url


# ---- CSRF ----------------------------------------------------------------------

@pytest.fixture
def seeded(store, add_doc, review_doc):
    """每個 POST 路由都準備一個能被改動的對象,被擋下時才能確認什麼都沒變。回傳各對象的 ID。"""
    doc_id = add_doc(BILL)
    return {"doc": doc_id,
            "auto": store.add_action(doc_id, "calendar", "auto", REMINDER),
            "confirm": store.add_action(doc_id, "medication_schedule", "confirm", {"items": []}),
            "review": review_doc(name="r.png")}


def post_requests(ids, png: bytes) -> dict:
    """現有的 5 種 POST(上傳、家人確認、取消提醒、更正讀值、退回),內容都合法,只差 token。"""
    return {
        "upload": ("/upload", {"doc_type": "不確定"}, {"file": ("a.png", png, "image/png")}),
        "confirm": (f"/confirm/{ids['confirm']}", {"decision": "done"}, None),
        "reminder": (f"/reminder/{ids['auto']}", {"decision": "cancel"}, None),
        "correct": (f"/doc/{ids['review']}/correct", {"date": "2026-07-04", "amount": "20"}, None),
        "reject": (f"/doc/{ids['review']}/reject", {}, None),
    }


def assert_nothing_changed(cfg, store, ids) -> None:
    docs = {d["id"]: d for d in store.list_documents()}
    assert len(docs) == 2 and docs[ids["review"]]["action"] == "review"
    assert docs[ids["review"]]["result"] == {"doc_type": "發票", "confidence": 0.5}
    assert {a["id"]: a["status"] for a in store.list_actions()} == {ids["auto"]: "pending", ids["confirm"]: "pending"}
    assert store.list_corrections(verified_only=False) == []
    assert (cfg.paths.review / "r.png").exists()
    for folder in (cfg.paths.uploads_path, cfg.paths.archive, cfg.paths.failed):
        assert not any(folder.iterdir()), folder


@pytest.mark.parametrize("route", ["upload", "confirm", "reminder", "correct", "reject"])
@pytest.mark.parametrize("problem", ["missing", "mismatch", "no_cookie"])
def test_post_without_matching_token_is_403_and_changes_nothing(cfg, store, app, seeded, form_client, png_bytes,
                                                                route, problem):
    ids = seeded
    url, data, files = post_requests(ids, png_bytes())[route]
    other_browser = form_client(app).csrf_token()          # 別人的合法 token
    client = TestClient(app)
    if problem != "no_cookie":
        client.get("/")                                   # 開過頁面,有自己的 cookie
    if problem != "missing":
        data = {**data, CSRF_FIELD: other_browser}
    r = client.post(url, data=data, files=files)
    assert r.status_code == 403
    assert "這次的操作沒有送出" in r.text and "錯誤代碼 403" in r.text
    assert "請回首頁重新操作一次" in r.text and 'href="/"' in r.text   # 中文說明 + 下一步
    assert_security_headers(r)
    assert_nothing_changed(cfg, store, ids)


def test_browser_flow_with_token_from_the_page_still_works(store, plain_client, seeded):
    """像真的瀏覽器:開「家人確認」頁,拿頁面表單裡的 token 送出,照常確認。"""
    ids, client = seeded, plain_client
    token = _TOKEN_FIELD.search(client.get("/confirm").text).group(1)
    assert token == client.cookies.get(CSRF_COOKIE)
    r = client.post(f"/confirm/{ids['confirm']}", data={"decision": "done", CSRF_FIELD: token},
                    follow_redirects=False)
    assert r.status_code == 303
    assert {a["id"]: a["status"] for a in store.list_actions()}[ids["confirm"]] == "done"


def test_every_post_form_on_the_pages_carries_the_token(plain_client, seeded):
    ids, client = seeded, plain_client
    forms = []
    for url in ("/", "/confirm", f"/doc/{ids['review']}/correct", f"/doc/{ids['doc']}"):
        forms += _POST_FORMS.findall(client.get(url).text)
    assert len(forms) == 5          # 上傳、家人確認、更正頁的存檔與退回、取消提醒
    hidden = f'<input type="hidden" name="{CSRF_FIELD}" value="{client.cookies.get(CSRF_COOKIE)}">'
    assert all(hidden in body for body in forms)


def test_confirmation_page_is_an_ordinary_page(client, seeded):
    """還沒確認時伺服器回的確認頁(SEC-02):安全標頭、不快取、表單帶這個瀏覽器的 token、沒有行內 JS,
    也只有共用的那一個確認框(確認頁自己的表單不再跳確認框)。"""
    r = client.post(f"/doc/{seeded['review']}/reject")
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"
    (body,) = _POST_FORMS.findall(r.text)
    assert f'<input type="hidden" name="{CSRF_FIELD}" value="{client.cookies.get(CSRF_COOKIE)}">' in body
    assert "data-confirm" not in body and r.text.count("<dialog") == 1
    assert not re.search(r"<script(?![^>]*\bsrc=)|\son[a-z]+\s*=|\sstyle\s*=|javascript:", r.text, re.I)


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES_DIR.glob("*.html")))
def test_post_forms_in_templates_include_csrf_input(template):
    """新表單(例如 F7 的更正頁)忘了放 {{ csrf_input() }},這裡就會抓到。"""
    for body in _POST_FORMS.findall((TEMPLATES_DIR / template).read_text(encoding="utf-8")):
        assert "{{ csrf_input() }}" in body


def test_token_cookie_is_http_only_and_issued_once(plain_client):
    client = plain_client
    cookie = client.get("/").headers["set-cookie"]
    assert cookie.startswith(f"{CSRF_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie                          # 本機 http:Secure cookie 瀏覽器不會存
    assert "set-cookie" not in client.get("/").headers     # 已經有就沿用,多開幾個分頁也不會互相蓋掉
    assert "set-cookie" not in TestClient(client.app).get("/static/app.css").headers   # 只有頁面發 cookie


def test_https_uses_host_prefixed_secure_cookie(app, form_client, png_bytes):
    """經 Tunnel 是 https:用 __Host- 名稱 + Secure,同網域的其他子網域無法塞一個假 token 進來。"""
    client = form_client(app, base_url="https://testserver")
    cookie = client.get("/").headers["set-cookie"]
    assert cookie.startswith(f"{CSRF_COOKIE_HTTPS}=") and "Secure" in cookie and "Path=/" in cookie
    assert "domain" not in cookie.lower()
    r = client.post("/upload", data={"doc_type": "不確定"}, files={"file": ("a.png", png_bytes(), "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303


def test_malformed_cookie_is_replaced_not_echoed(plain_client):
    client = plain_client
    client.cookies.set(CSRF_COOKIE, "abc<script>")
    r = client.get("/")
    assert "abc<script>" not in r.text and "abc&lt;script&gt;" not in r.text
    assert r.headers["set-cookie"].startswith(f"{CSRF_COOKIE}=")


def test_oversize_upload_is_413_before_token_check_and_keeps_token(plain_client, monkeypatch):
    """413 在讀 body 之前就回(不必先收完整個檔案);重畫的首頁帶著 token,長輩改選小檔就能直接再送。"""
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 10)
    client = plain_client                                   # 沒開過頁面、也沒帶 token
    r = client.post("/upload", files={"file": ("a.png", b"\0" * (web_app._MULTIPART_SLACK + 100), "image/png")})
    assert r.status_code == 413 and "15MB" in r.text
    assert _TOKEN_FIELD.search(r.text).group(1) == client.cookies.get(CSRF_COOKIE)


# ---- 每個 POST 的 body 都有大小上限(SEC-10) ---------------------------------------------------
# 原本只有 /upload 有:其他 POST 的 body(含沒有上限的檔案部分)會先整份收下、寫進暫存檔,才檢查 CSRF

_FORM_POSTS = {"confirm": "/confirm/{confirm}", "reminder": "/reminder/{auto}", "correct": "/doc/{review}/correct",
               "reject": "/doc/{review}/reject", "settings": "/settings", "delete": "/settings/delete"}


@pytest.mark.parametrize("route", sorted(_FORM_POSTS))
@pytest.mark.parametrize("with_token", [False, True])
def test_oversize_form_post_is_413_and_changes_nothing(cfg, store, app, seeded, form_client, route, with_token):
    """Content-Length 超過上限就回 413 的中文頁:不看有沒有 token(還沒讀 body),帶了合法的 token 與欄位也不做。"""
    client = form_client(app) if with_token else TestClient(app)
    data = {"decision": "done", "understood": "1", "confirm_reject": "1", "auto_threshold": "0.95", "amount": "20"}
    junk = {"junk": ("junk.bin", b"\0" * (web_app.MAX_FORM_BYTES + 1), "application/octet-stream")}
    r = client.post(_FORM_POSTS[route].format(**seeded), data=data, files=junk)
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html")
    assert "<h1>送出的資料太多了</h1>" in r.text and "錯誤代碼 413" in r.text and 'href="/"' in r.text
    assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"
    assert_nothing_changed(cfg, store, seeded)
    assert store.get_settings() == {} and store.list_setting_changes() == []


def test_the_largest_valid_form_fits_under_the_limit(client, store, review_doc):
    """更正表單收得下的最大內容(30 種藥,藥名與用法各 500 個中文字,網址編碼後一個字 9 個位元組)不會被上限擋下。"""
    from web.render import MAX_FORM_TEXT, MAX_MED_ITEMS
    doc_id = review_doc({"doc_type": "藥袋", "vendor": "範例診所", "date": "2026-10-01", "fields": {"items": []}},
                        name="bag.png")
    data = {"vendor": "範例診所", "date": "2026-10-01"}
    for i in range(MAX_MED_ITEMS):
        data.update({f"items-{i}-name": "藥" * MAX_FORM_TEXT, f"items-{i}-usage": "用" * MAX_FORM_TEXT,
                     f"items-{i}-timing": "早", f"items-{i}-days": "7"})
    r = client.post(f"/doc/{doc_id}/correct", data=data, follow_redirects=False)
    assert int(r.request.headers["content-length"]) > 256 * 1024       # 比 256KB 還大的合法表單
    assert r.status_code == 303
    items = store.get_document(doc_id)["result"]["fields"]["items"]
    assert len(items) == MAX_MED_ITEMS and all(len(it["name"]) == MAX_FORM_TEXT for it in items)


def _asgi_post(app, path: str, headers: dict[str, str], chunks) -> tuple[int, int]:
    """不經 TestClient,直接用 ASGI 送一個 POST:body 一塊一塊給。回傳(狀態碼, 伺服器實際收了幾塊)。"""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    pulled, sent, source = [], [], iter(chunks)

    async def receive():
        chunk = next(source, None)
        if chunk is None:
            return {"type": "http.disconnect"}
        pulled.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), len(pulled)


@pytest.mark.parametrize("path", ["/settings/delete", "/doc/1/correct", "/upload"])
def test_body_without_content_length_is_cut_off_at_the_limit(app, monkeypatch, path):
    """沒有 Content-Length(分塊傳送)的請求邊收邊數,超過上限就停、回 413,不把整份收完;
    不用 411 拒收:中間的代理改用分塊傳送時,表單還是送得出去。"""
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", web_app.MAX_FORM_BYTES - web_app._MULTIPART_SLACK)   # 兩種上限一樣大
    chunk = b"a" * (64 * 1024)
    status, pulled = _asgi_post(app, path, {"content-type": "application/x-www-form-urlencoded"}, [chunk] * 100)
    assert status == 413
    assert pulled == web_app.MAX_FORM_BYTES // len(chunk) + 1        # 超過上限的那一塊之後就不再收


def test_oversize_content_length_is_refused_before_reading_anything(app):
    status, pulled = _asgi_post(app, "/settings/delete", {"content-type": "application/x-www-form-urlencoded",
                                                         "content-length": str(web_app.MAX_FORM_BYTES + 1)},
                                [b"a" * 1024] * 300)
    assert (status, pulled) == (413, 0)


def test_chunked_form_within_the_limit_still_works(store, plain_client, seeded):
    """沒有 Content-Length 但大小正常的表單照常處理(回 411 的話,這種請求會全部被拒)。"""
    client = plain_client
    token = _TOKEN_FIELD.search(client.get("/confirm").text).group(1)
    body = f"decision=done&{CSRF_FIELD}={token}".encode()
    r = client.post(f"/confirm/{seeded['confirm']}", content=iter([body]),
                    headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert "content-length" not in r.request.headers and r.status_code == 303
    assert {a["id"]: a["status"] for a in store.list_actions()}[seeded["confirm"]] == "done"
