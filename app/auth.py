"""Просте сесійне логування для адмін-доступу до фронта/API.

Облікові дані — з env (AUTH_USER / AUTH_PASSWORD), порівняння в постійному часі.
Сесії тримаються в пам'яті: токен-кука, валідна доти, доки не спливе TTL або
не рестартне процес (тоді просто перелогінитись). Достатньо для одного інстансу
внутрішнього сервісу.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .config import settings

COOKIE_NAME = "bw_session"


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 над сирим тілом для машинних викликів від ncP2P."""
    if not settings.inbound_secret or not signature:
        return False
    expected = hmac.new(settings.inbound_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

_sessions: dict[str, float] = {}  # token -> expiry (epoch seconds)


def auth_enabled() -> bool:
    return bool(settings.auth_user and settings.auth_password)


def verify_credentials(user: str, password: str) -> bool:
    if not auth_enabled():
        return False
    ok_user = hmac.compare_digest(user or "", settings.auth_user)
    ok_pw = hmac.compare_digest(password or "", settings.auth_password)
    return ok_user and ok_pw


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + settings.session_ttl_hours * 3600
    return token


def validate(token: str | None) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def destroy(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)
