"""驗證 Access 登入身分；家庭角色另由本機資料庫決定。"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import jwt
from starlette.requests import Request


@dataclass(frozen=True)
class Identity:
    email: str | None
    local: bool


class IdentityError(ValueError):
    """登入無法驗證；訊息可直接顯示，不含憑證與部署資訊。"""


_ISSUER = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com")
_AUD = re.compile(r"[A-Za-z0-9_-]{1,256}")
_LOGIN_ERROR = "無法確認登入身分，請重新登入後再試。"
_CONFIG_ERROR = "尚未完成遠端登入設定，請家人在這台電腦完成設定。"
_KEY_ERROR = "暫時無法驗證登入身分，請稍後再試。"


def _is_direct_local(request: Request) -> bool:
    """三個條件同時成立，才接受本機管理入口。"""
    if not request.client:
        return False
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
        hostname = request.url.hostname
    except ValueError:
        return False
    if not loopback or hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    return not any(
        name in {"forwarded", "via", "x-real-ip"}
        or name.startswith(("cf-", "x-forwarded-", "proxy-", "x-proxy-"))
        for name in request.headers
    )


class AccessIdentityResolver:
    """固定信任設定中的 Access 團隊；只在遠端請求時取得公鑰。

    transport 與 clock 為離線測試接縫；正式環境不需傳入。
    公鑰快取五分鐘，未知 kid 最多每三十秒刷新一次，失敗不沿用過期公鑰。
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = os.environ.get("KANYOU_ACCESS_ISSUER", "")
        self._audience = os.environ.get("KANYOU_ACCESS_AUD", "")
        self._configured = bool(
            _ISSUER.fullmatch(self._issuer) and _AUD.fullmatch(self._audience)
        )
        self._transport = transport
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._next_refresh = 0.0
        self._lock = threading.Lock()

    def resolve(self, request: Request) -> Identity:
        if _is_direct_local(request):
            return Identity(email=None, local=True)
        if not self._configured:
            raise IdentityError(_CONFIG_ERROR)
        assertions = request.headers.getlist("cf-access-jwt-assertion")
        if len(assertions) != 1 or not assertions[0] or len(assertions[0]) > 16384:
            raise IdentityError(_LOGIN_ERROR)
        token = assertions[0]
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                raise IdentityError(_LOGIN_ERROR)
            key = self._get_key(kid)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["iss", "aud", "exp", "email"]},
            )
            expiry = claims["exp"]
            email = claims["email"]
            if (
                isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
                or not isinstance(email, str)
                or len(email) > 254
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
                or any(ord(char) < 32 or ord(char) == 127 for char in email)
            ):
                raise IdentityError(_LOGIN_ERROR)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError) as exc:
            if isinstance(exc, IdentityError):
                raise
            raise IdentityError(_LOGIN_ERROR) from None
        return Identity(email=email.casefold(), local=False)

    def _get_key(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            now = self._clock()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now < self._next_refresh:
                raise IdentityError(_KEY_ERROR)
            self._next_refresh = now + 30
            keys = self._fetch_keys()
            self._keys = keys
            self._expires_at = self._clock() + 300
            if kid not in keys:
                raise IdentityError(_LOGIN_ERROR)
            return keys[kid]

    def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        # 不採用 token 內的 URL、不追蹤轉址，也不從環境帶入代理設定。
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(5.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream("GET", self._issuer + "/cdn-cgi/access/certs") as response:
                    response.raise_for_status()
                    body = bytearray()
                    started = self._clock()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 131072 or self._clock() - started > 10:
                            raise IdentityError(_KEY_ERROR)
            raw = json.loads(body)
            entries = raw.get("keys") if isinstance(raw, dict) else None
            if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                raise IdentityError(_KEY_ERROR)
            keys: dict[str, jwt.PyJWK] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise IdentityError(_KEY_ERROR)
                kid = entry.get("kid")
                if (
                    not isinstance(kid, str) or not 1 <= len(kid) <= 256 or kid in keys
                    or entry.get("kty") != "RSA"
                    or entry.get("alg", "RS256") != "RS256"
                    or entry.get("use", "sig") != "sig"
                    or ("key_ops" in entry and entry["key_ops"] != ["verify"])
                ):
                    raise IdentityError(_KEY_ERROR)
                key = jwt.PyJWK.from_dict(entry, algorithm="RS256")
                if key.key.key_size < 2048:
                    raise IdentityError(_KEY_ERROR)
                keys[kid] = key
            return keys
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
            raise IdentityError(_KEY_ERROR) from None
