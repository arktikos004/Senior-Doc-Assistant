"""Access 身分驗證：只用即時產生的合成金鑰與 MockTransport。"""
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from web.access import AccessIdentityResolver, Identity, IdentityError


ISSUER = "https://synthetic-family.cloudflareaccess.com"
AUD = "synthetic-application-audience"


def request(*, client="203.0.113.8", host="family.example.test", headers=None):
    return Request({
        "type": "http", "method": "GET", "path": "/", "scheme": "https",
        "query_string": b"", "server": ("127.0.0.1", 8000),
        "client": (client, 12345) if client else None,
        "headers": [(b"host", host.encode())] + [
            (key.lower().encode(), value.encode()) for key, value in (headers or [])
        ],
    })


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("KANYOU_ACCESS_ISSUER", ISSUER)
    monkeypatch.setenv("KANYOU_ACCESS_AUD", AUD)
    # 測試若忘記注入 transport，立即失敗，不能碰真實網路。
    def no_network(*args, **kwargs):
        raise AssertionError("測試禁止連網")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def jwk(key, kid="first"):
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**value, "kid": kid, "use": "sig", "alg": "RS256"}


def token(key, *, kid="first", changes=None, omit=None, algorithm="RS256"):
    claims = {"iss": ISSUER, "aud": [AUD], "exp": int(time.time()) + 300,
              "email": "Family@Example.test"}
    claims.update(changes or {})
    if omit:
        claims.pop(omit)
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def authenticated(value, **kwargs):
    return request(headers=[("cf-access-jwt-assertion", value)], **kwargs)


def resolver_for(payload, *, status=200, clock=lambda: 100):
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(status, json=payload)
    return AccessIdentityResolver(transport=httpx.MockTransport(respond), clock=clock), calls


@pytest.mark.parametrize("client,host", [
    ("127.0.0.1", "localhost"), ("127.0.0.1", "127.0.0.1:8000"),
    ("::1", "[::1]:8000"), ("127.0.0.2", "localhost"),
])
def test_direct_local_works_offline_without_access_configuration(monkeypatch, client, host):
    monkeypatch.delenv("KANYOU_ACCESS_ISSUER")
    monkeypatch.delenv("KANYOU_ACCESS_AUD")
    resolver = AccessIdentityResolver()
    assert resolver.resolve(request(client=client, host=host)) == Identity(None, True)


@pytest.mark.parametrize("header", [
    "forwarded", "via", "x-real-ip", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "cf-connecting-ip", "cf-ray", "cf-visitor",
    "cf-access-authenticated-user-email", "cf-access-jwt-assertion",
    "proxy-authorization", "x-proxy-user",
])
def test_proxy_headers_even_empty_prevent_local_privileges(header):
    with pytest.raises(IdentityError):
        AccessIdentityResolver().resolve(request(
            client="127.0.0.1", host="localhost", headers=[(header, "")],
        ))


@pytest.mark.parametrize("client,host", [
    ("203.0.113.1", "localhost"), (None, "localhost"),
    ("127.0.0.1", "family.example.test"), ("127.0.0.1", "localhost.example.test"),
    ("localhost", "localhost"), ("192.168.1.8", "127.0.0.1"),
])
def test_local_requires_socket_loopback_and_local_hostname(client, host):
    with pytest.raises(IdentityError):
        AccessIdentityResolver().resolve(request(client=client, host=host))


@pytest.mark.parametrize("issuer", [
    "", "http://synthetic-family.cloudflareaccess.com", "https://localhost",
    "https://synthetic-family.cloudflareaccess.com.attacker.test",
    "https://user@synthetic-family.cloudflareaccess.com",
    "https://synthetic-family.cloudflareaccess.com:443",
    "https://synthetic-family.cloudflareaccess.com/", "https://a.b.cloudflareaccess.com",
    "https://-bad.cloudflareaccess.com", "https://a.cloudflareaccess.com?x=1",
])
def test_invalid_issuer_fails_closed_before_network(monkeypatch, issuer):
    monkeypatch.setenv("KANYOU_ACCESS_ISSUER", issuer)
    with pytest.raises(IdentityError, match="設定"):
        AccessIdentityResolver().resolve(request())


@pytest.mark.parametrize("audience", ["", " ", "bad audience", "x" * 257])
def test_invalid_audience_fails_closed(monkeypatch, audience):
    monkeypatch.setenv("KANYOU_ACCESS_AUD", audience)
    with pytest.raises(IdentityError, match="設定"):
        AccessIdentityResolver().resolve(request())


def test_signed_identity_ignores_email_header_and_fetches_only_fixed_url(keys):
    resolver, calls = resolver_for({"keys": [jwk(keys[0])]})
    assert not calls
    req = request(client="127.0.0.1", headers=[
        ("cf-access-jwt-assertion", token(keys[0])),
        ("cf-access-authenticated-user-email", "attacker@example.test"),
    ])
    assert resolver.resolve(req) == Identity("family@example.test", False)
    assert resolver.resolve(req) == Identity("family@example.test", False)
    assert len(calls) == 1
    assert str(calls[0].url) == ISSUER + "/cdn-cgi/access/certs"
    assert "cf-access-jwt-assertion" not in calls[0].headers


@pytest.mark.parametrize("omit", ["iss", "aud", "exp", "email"])
def test_required_claims(keys, omit):
    resolver, _ = resolver_for({"keys": [jwk(keys[0])]})
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0], omit=omit)))


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.cloudflareaccess.com"}, {"aud": "another-app"},
    {"exp": 1}, {"exp": "9999999999"}, {"exp": True}, {"exp": float("nan")},
    {"nbf": int(time.time()) + 3600}, {"iat": int(time.time()) + 3600},
    {"email": ""}, {"email": None}, {"email": ["family@example.test"]},
    {"email": " family@example.test"}, {"email": "not-an-email"},
    {"email": "a\x00@example.test"}, {"email": "a@b@c"},
])
def test_invalid_claims(keys, changes):
    resolver, _ = resolver_for({"keys": [jwk(keys[0])]})
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0], changes=changes)))


def test_wrong_signature_and_algorithm_are_rejected(keys):
    resolver, calls = resolver_for({"keys": [jwk(keys[0])]})
    for value in [token(keys[1]), token("synthetic-secret-" * 4, algorithm="HS256"),
                  token(None, algorithm="none")]:
        with pytest.raises(IdentityError):
            resolver.resolve(authenticated(value))
    assert len(calls) == 1


@pytest.mark.parametrize("headers", [
    [], [("cf-access-authenticated-user-email", "family@example.test")],
    [("cf-access-jwt-assertion", "malformed")],
    [("cf-access-jwt-assertion", "a" * 16385)],
    [("cf-access-jwt-assertion", "first"), ("cf-access-jwt-assertion", "second")],
])
def test_untrusted_headers_are_not_login(headers):
    with pytest.raises(IdentityError):
        AccessIdentityResolver().resolve(request(headers=headers))


def test_unknown_key_rotation_refresh_is_throttled(keys):
    now = [100]
    payload = {"keys": [jwk(keys[0])]}
    resolver, calls = resolver_for(payload, clock=lambda: now[0])
    resolver.resolve(authenticated(token(keys[0])))
    second = authenticated(token(keys[1], kid="second"))
    payload["keys"] = [jwk(keys[1], "second")]
    with pytest.raises(IdentityError):
        resolver.resolve(second)
    assert len(calls) == 1
    now[0] = 131
    assert resolver.resolve(second).email == "family@example.test"
    assert len(calls) == 2
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))
    assert len(calls) == 2


def test_expired_cache_fails_closed_and_network_failure_is_throttled(keys):
    now = [100]
    calls = []
    def respond(req):
        calls.append(req)
        if len(calls) > 1:
            raise httpx.ReadTimeout("synthetic secret deployment domain")
        return httpx.Response(200, json={"keys": [jwk(keys[0])]})
    resolver = AccessIdentityResolver(transport=httpx.MockTransport(respond), clock=lambda: now[0])
    req = authenticated(token(keys[0]))
    resolver.resolve(req)
    now[0] = 401
    for _ in range(2):
        with pytest.raises(IdentityError) as caught:
            resolver.resolve(req)
        assert "synthetic" not in str(caught.value)
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [None, [], {}, {"keys": []}, {"keys": [None]},
                                        {"keys": [{"kid": "first", "kty": "oct"}]}])
def test_invalid_jwks_fails_closed(keys, payload):
    resolver, _ = resolver_for(payload)
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))


@pytest.mark.parametrize("alter", [
    {"alg": "HS256"}, {"use": "enc"}, {"key_ops": ["sign"]}, {"n": "!"},
])
def test_invalid_rsa_key_fails_closed(keys, alter):
    resolver, _ = resolver_for({"keys": [{**jwk(keys[0]), **alter}]})
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))


def test_duplicate_key_ids_fail_closed(keys):
    resolver, _ = resolver_for({"keys": [jwk(keys[0]), jwk(keys[1])]})
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))


@pytest.mark.parametrize("status", [302, 404, 500])
def test_jwks_http_failures_and_redirects_fail_closed(keys, status):
    resolver, calls = resolver_for({"keys": [jwk(keys[0])]}, status=status)
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))
    assert len(calls) == 1


def test_oversized_key_response_fails_closed(keys):
    resolver, _ = resolver_for({"keys": [jwk(keys[0])], "padding": "x" * 131072})
    with pytest.raises(IdentityError):
        resolver.resolve(authenticated(token(keys[0])))
