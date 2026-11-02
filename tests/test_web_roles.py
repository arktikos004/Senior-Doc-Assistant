"""家庭角色的行為測試；身分與文件都使用合成資料，不連外。"""
import pytest
from fastapi.testclient import TestClient

from src.providers import MockAnalyzer
from web.access import Identity, IdentityError, AccessIdentityResolver
from web.app import create_app, CSRF_FIELD, CSRF_COOKIE


class FixedIdentity:
    def __init__(self, email=None, local=False):
        self.identity = Identity(email, local)

    def resolve(self, request):
        return self.identity


@pytest.fixture
def as_role(cfg, store):
    def make(role):
        email = f"{role}@example.test"
        if role in ("family", "elder"):
            store.set_member(email, role)
            store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)   # 已看過資料使用說明
        resolver = FixedIdentity(email, role == "local")
        return TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=resolver))
    return make


def post(client, path, data=None, **kwargs):
    client.get("/")
    return client.post(path, data={CSRF_FIELD: client.cookies[CSRF_COOKIE], **(data or {})},
                       follow_redirects=False, **kwargs)


@pytest.mark.parametrize("role", ["elder", "viewer"])
@pytest.mark.parametrize("path,data", [
    ("/confirm/1", {"decision": "done"}),
    ("/doc/1/correct", {"amount": "100"}),
    ("/settings", {"auto_threshold": "0.90"}),
    ("/settings/delete", {"understood": "1"}),
    ("/settings/members", {"email": "attacker@example.test", "role": "family"}),
    ("/settings/members/remove", {"email": "family@example.test", "confirmed": "1"}),
])
def test_non_family_cannot_mutate_family_operations(as_role, store, role, path, data):
    client = as_role(role)
    before = (store.get_settings(), store.list_members(), store.list_audit_events())
    assert post(client, path, data).status_code == 403
    assert before == (store.get_settings(), store.list_members(), store.list_audit_events())


@pytest.mark.parametrize("role", ["elder", "viewer"])
@pytest.mark.parametrize("path", ["/settings/export", "/settings/members", "/doc/1/correct"])
def test_non_family_cannot_open_management(as_role, role, path):
    assert as_role(role).get(path).status_code == 403


@pytest.mark.parametrize("path,data", [
    ("/upload", {}), ("/upload/password/synthetic", {"password": "fake"}),
    ("/reminder/1", {"decision": "cancel"}), ("/confirm/1", {"decision": "rejected"}),
    ("/doc/1/reject", {"confirm_reject": "1"}),
])
def test_viewer_cannot_mutate_member_operations(as_role, path, data):
    assert post(as_role("viewer"), path, data).status_code == 403


def test_initial_remote_is_readonly_and_local_can_bootstrap(as_role, store):
    viewer = as_role("viewer")
    assert viewer.get("/").status_code == 200
    assert "只能查看" in viewer.get("/").text
    assert 'action="/upload"' not in viewer.get("/").text
    local = as_role("local")
    r = post(local, "/settings/members", {"email": "Family@Example.test", "role": "family", "display_name": "示範家人"})
    assert r.status_code == 303
    assert store.get_member("family@example.test")["display_name"] == "示範家人"
    assert store.list_setting_changes()[0]["actor"] == "local"


def test_role_revocation_applies_next_request(as_role, store, add_doc):
    client = as_role("family")
    doc = add_doc({"doc_type": "藥袋"})
    action = store.add_action(doc, "medication_schedule", "confirm", {})
    store.set_member("family@example.test", "elder")
    assert post(client, f"/confirm/{action}", {"decision": "done"}).status_code == 403
    assert store.list_actions()[0]["status"] == "pending"
    assert store.list_audit_events() == []


def test_elder_rejects_previously_confirmed_with_explicit_current_state(as_role, store, add_doc):
    family, elder = as_role("family"), as_role("elder")
    doc = add_doc({"doc_type": "藥袋"})
    action = store.add_action(doc, "medication_schedule", "confirm", {})
    assert post(family, f"/confirm/{action}", {"decision": "done", "actor": "forged@example.test", "role": "elder"}).status_code == 303
    assert post(elder, f"/confirm/{action}", {"decision": "rejected", "confirm_reject": "1"}).status_code == 409
    assert post(elder, f"/confirm/{action}", {"decision": "rejected", "confirm_reject": "1", "expected_status": "done"}).status_code == 303
    events = store.list_audit_events(doc)
    assert [(e["event"], e["actor"], e["role"]) for e in events] == [
        ("reject", "elder@example.test", "elder"), ("confirm", "family@example.test", "family")]
    assert events[0]["detail"] == {"from": "done", "to": "rejected"}
    html = elder.get(f"/doc/{doc}").text
    assert "操作紀錄" in html and "family@example.test" in html and "elder@example.test" in html


def test_elder_reminder_changes_are_logged_once(as_role, store, add_doc):
    client = as_role("elder")
    doc = add_doc({"doc_type": "帳單"})
    action = store.add_action(doc, "calendar", "auto", {})
    for decision in ("cancel", "cancel", "restore"):
        assert post(client, f"/reminder/{action}", {"decision": decision}).status_code == 303
    assert [e["event"] for e in store.list_audit_events(doc)] == ["restore_reminder", "cancel_reminder"]


def test_reject_document_records_verified_actor(as_role, store, review_doc):
    client = as_role("elder")
    doc = review_doc()
    assert post(client, f"/doc/{doc}/reject", {"confirm_reject": "1"}).status_code == 303
    event = store.list_audit_events(doc)[0]
    assert (event["event"], event["actor"], event["detail"]) == (
        "reject_document", "elder@example.test", {"from": "review", "to": "failed"})


def test_correction_and_category_log_verified_family(as_role, store, review_doc):
    from test_web_correct import _bill, browser_form
    client = as_role("family")
    doc = review_doc(_bill())
    path = f"/doc/{doc}/correct"
    form = browser_form(client.get(path).text, path)
    form.update(amount="1400", actor="forged@example.test", role="elder")
    assert post(client, path, form).status_code == 303
    event = next(e for e in store.list_audit_events(doc) if e["event"] == "correct")
    assert (event["actor"], event["role"]) == ("family@example.test", "family")
    assert store.list_corrections(document_id=doc, verified_only=False)[0]["after"]["amount"] == 1400
    form = browser_form(client.get(path).text, path)
    form["category"] = "財產資產"
    assert post(client, path, form).status_code == 303
    assert store.list_audit_events(doc)[0]["event"] == "category"


def test_member_changes_are_readable_in_settings(as_role):
    client = as_role("local")
    post(client, "/settings/members", {"email": "elder@example.test", "role": "elder"})
    html = client.get("/settings").text
    assert "更新家庭成員 elder@example.test（長輩）" in html and "（這台電腦）" in html


def test_last_family_cannot_be_removed_or_demoted(as_role, store):
    client = as_role("family")
    assert post(client, "/settings/members/remove", {"email": "family@example.test", "confirmed": "1"}).status_code == 400
    assert post(client, "/settings/members", {"email": "family@example.test", "role": "elder"}).status_code == 400
    assert store.get_member("family@example.test")["role"] == "family"


def test_removal_needs_confirmation_and_revokes_permissions(as_role, store):
    client = as_role("family")
    store.set_member("other@example.test", "family")
    assert post(client, "/settings/members/remove", {"email": "other@example.test"}).status_code == 400
    assert store.get_member("other@example.test")
    assert post(client, "/settings/members/remove", {"email": "other@example.test", "confirmed": "1"}).status_code == 303
    assert store.get_member("other@example.test") is None


@pytest.mark.parametrize("email,role,name", [
    ("bad", "family", ""), ("x@example.test", "admin", ""), ("x@example.test", "family", "x"*41),
    ("x@example.test", "family", "name\x00"), ("x\n@example.test", "elder", ""),
])
def test_invalid_members_never_saved(as_role, store, email, role, name):
    client = as_role("local")
    assert post(client, "/settings/members", dict(email=email, role=role, display_name=name)).status_code == 400
    assert store.list_members() == []


def test_member_name_html_is_escaped(as_role):
    client = as_role("local")
    assert post(client, "/settings/members", dict(email="family@example.test", role="family", display_name="<script>bad()</script>")).status_code == 303
    html = client.get("/settings/members").text
    assert "<script>bad()" not in html and "&lt;script&gt;bad()" in html


def test_export_includes_actors_and_purge_clears_audit_but_keeps_members(as_role, store, add_doc):
    client = as_role("family")
    doc = add_doc({"doc_type": "藥袋"})
    action = store.add_action(doc, "medication_schedule", "confirm", {})
    post(client, f"/confirm/{action}", {"decision": "done"})
    data = client.get("/settings/export").json()
    assert data["audit_events"][0]["actor"] == "family@example.test" and data["members"]
    assert post(client, "/settings/delete", {"understood": "1"}).status_code == 303
    assert store.list_audit_events() == [] and store.get_member("family@example.test")


def test_real_resolver_rejects_forged_email_before_any_content(cfg, monkeypatch):
    monkeypatch.delenv("KANYOU_ACCESS_ISSUER", raising=False)
    monkeypatch.delenv("KANYOU_ACCESS_AUD", raising=False)
    client = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=AccessIdentityResolver()))
    for path in ("/", "/healthz", "/settings/export", "/doc/1/file"):
        r = client.get(path, headers={"Cf-Access-Authenticated-User-Email": "family@example.test"})
        assert r.status_code == 403 and r.headers["cache-control"] == "no-store"


def test_identity_errors_never_reach_analyzer(cfg):
    class Denied:
        def resolve(self, request):
            raise IdentityError("無法確認登入身分。")
    client = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Denied()))
    assert client.post("/upload").status_code == 403


def test_verified_jwt_drives_role_and_actor_end_to_end(cfg, store, add_doc, monkeypatch):
    import json
    import time
    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    issuer = "https://synthetic-family.cloudflareaccess.com"
    monkeypatch.setenv("KANYOU_ACCESS_ISSUER", issuer)
    monkeypatch.setenv("KANYOU_ACCESS_AUD", "test-audience")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "test-key"}
    resolver = AccessIdentityResolver(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [public]})))
    store.set_member("family@example.test", "family")
    action = store.add_action(add_doc({"doc_type": "藥袋"}), "medication_schedule", "confirm", {})
    token = jwt.encode({"iss": issuer, "aud": ["test-audience"], "exp": int(time.time()) + 60,
                        "email": "Family@Example.test"}, key, algorithm="RS256", headers={"kid": "test-key"})
    client = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=resolver),
                        headers={"Cf-Access-Jwt-Assertion": token,
                                 "Cf-Access-Authenticated-User-Email": "forged@example.test"})
    assert post(client, f"/confirm/{action}", {"decision": "done"}).status_code == 303
    assert store.list_audit_events()[0]["actor"] == "family@example.test"
    store.set_member("family@example.test", "elder")
    assert post(client, "/settings/members", {"email": "attacker@example.test", "role": "family"}).status_code == 403
