"""資料使用說明與同意:從外面登入的家庭成員第一次使用先看說明、按「我看過了」才進得去,誰、什麼時候看過都留紀錄。

在這台電腦直接開(本機)的不攔:那是管理的人在設定。身分是注入的合成帳號,不連外。
"""
from fastapi.testclient import TestClient

from src.providers import MockAnalyzer
from src.store import CONSENT_PREFIX
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app


def _member(cfg, store, role="elder", email="elder@example.test"):
    store.set_member(email, role)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    return TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))


def test_first_visit_from_outside_shows_the_notice_until_acknowledged(cfg, store):
    elder = _member(cfg, store)
    r = elder.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/data-use?next=/cabinet"
    notice = elder.get("/data-use?next=/cabinet")
    assert notice.status_code == 200 and "資料怎麼用" in notice.text and "我看過了" in notice.text
    assert "拍攝地點" in notice.text and "不會替您付款" in notice.text

    ack = elder.post("/data-use", data={CSRF_FIELD: elder.cookies[CSRF_COOKIE], "next": "/cabinet"},
                     follow_redirects=False)
    assert ack.status_code == 303 and ack.headers["location"] == "/cabinet"
    assert elder.get("/cabinet", follow_redirects=False).status_code == 200
    assert f"{CONSENT_PREFIX}elder@example.test" in store.get_settings()
    [change] = [c for c in store.list_setting_changes() if c["key"].startswith(CONSENT_PREFIX)]
    assert change["actor"] == "elder@example.test"


def test_the_family_sees_who_read_the_notice_on_the_settings_page(cfg, store):
    elder = _member(cfg, store)
    elder.get("/data-use")
    elder.post("/data-use", data={CSRF_FIELD: elder.cookies[CSRF_COOKIE]}, follow_redirects=False)
    family = _member(cfg, store, "family", "family@example.test")
    family.get("/data-use")
    family.post("/data-use", data={CSRF_FIELD: family.cookies[CSRF_COOKIE]}, follow_redirects=False)
    assert "elder@example.test 看過資料使用說明" in family.get("/settings").text


def test_next_only_goes_to_a_page_on_this_site(cfg, store):
    elder = _member(cfg, store)
    elder.get("/data-use")
    for target in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        r = elder.post("/data-use", data={CSRF_FIELD: elder.cookies[CSRF_COOKIE], "next": target},
                       follow_redirects=False)
        assert r.headers["location"] == "/"


def test_static_files_and_health_check_are_not_gated(cfg, store):
    elder = _member(cfg, store)
    assert elder.get("/static/app.css").status_code == 200
    assert elder.get("/healthz").status_code == 200


def test_this_computer_is_not_gated_and_can_read_the_notice(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    page = client.get("/data-use").text
    assert "資料怎麼用" in page and "我看過了" not in page          # 本機不必按,說明照樣看得到
