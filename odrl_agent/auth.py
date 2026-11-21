"""Caller identity for the HTTP API: Bearer keys and short-lived cookie sessions.

Keeps the existing gate semantics (fail-closed once any key exists,
``hmac.compare_digest`` for env keys, SHA-256 fingerprints only at rest) and
adds a browser login that never stores the key in ``localStorage``:

* ``POST /api/session`` exchanges a valid key for an opaque random session
  id in an ``HttpOnly; SameSite=Strict`` cookie. The server keeps only the
  key fingerprint and an expiry; the raw key is never stored.
* A cookie-authenticated unsafe request (POST/PUT/PATCH/DELETE) must also send
  ``X-Requested-With: nl2odrl``. A cross-site form cannot set that header,
  and a cross-site ``fetch`` that sets it needs a CORS preflight this server
  never grants, so the cookie alone cannot be replayed (CSRF).
* Revoking a key also invalidates its sessions: every request re-checks that
  the session's fingerprint is still a live key.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SESSION_COOKIE = "nl2odrl_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "nl2odrl"
SESSION_TTL_SECONDS = int(os.environ.get("NL2ODRL_SESSION_TTL", "28800"))  # 8 h
MAX_SESSIONS = 10000
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def fingerprint(raw: str) -> str:
    """Attribution id: sha256 prefix. Raw keys are never stored."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def bearer(headers: Mapping[str, str]) -> str:
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return ""
    return auth[len("Bearer "):].strip()


def key_is_valid(presented: str, env_keys: list[str], valid_fps: set[str]) -> bool:
    if not presented:
        return False
    if any(hmac.compare_digest(presented, k) for k in env_keys):
        return True
    return fingerprint(presented) in valid_fps


def live_fingerprints(env_keys: list[str], valid_fps: set[str]) -> set[str]:
    return {fingerprint(k) for k in env_keys} | set(valid_fps)


@dataclass(frozen=True)
class Principal:
    """Who is calling: a key fingerprint (``local`` when the gate is off)."""

    fp: str
    via: str  # "local" | "bearer" | "cookie"

    @property
    def tenant(self) -> str:
        return self.fp

    @property
    def scope(self) -> str | None:
        """Row filter for tenant-scoped queries (``None`` = everything, local mode)."""
        return None if self.via == "local" else self.fp


class SessionStore:
    """In-memory session ids -> (fingerprint, expiry). Bounded and TTL'd."""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS, max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}

    def _prune(self, now: float) -> None:
        for sid in [s for s, (_fp, exp) in self._sessions.items() if exp <= now]:
            del self._sessions[sid]
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda s: self._sessions[s][1])
            del self._sessions[oldest]

    def create(self, fp: str) -> str:
        now = time.monotonic()
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            self._sessions[sid] = (fp, now + self.ttl)
        return sid

    def lookup(self, sid: str | None) -> str | None:
        if not sid:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                return None
            if entry[1] <= now:
                del self._sessions[sid]
                return None
            return entry[0]

    def revoke(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


SESSIONS = SessionStore()


def authenticate(
    headers: Mapping[str, str],
    cookies: Mapping[str, str],
    key_state: tuple[list[str], set[str], bool],
    sessions: SessionStore = SESSIONS,
) -> Principal | None:
    """Resolve the caller, or ``None`` when the gate is on and nothing valid was shown."""
    env_keys, valid_fps, gate_on = key_state
    if not gate_on:
        return Principal("local", "local")
    presented = bearer(headers)
    if presented:
        if key_is_valid(presented, env_keys, valid_fps):
            return Principal(fingerprint(presented), "bearer")
        return None
    fp = sessions.lookup(cookies.get(SESSION_COOKIE))
    if fp is not None and fp in live_fingerprints(env_keys, valid_fps):
        return Principal(fp, "cookie")
    return None


def csrf_ok(method: str, principal: Principal, headers: Mapping[str, Any]) -> bool:
    """Cookie-authenticated unsafe requests must carry the custom header."""
    if principal.via != "cookie" or method.upper() not in UNSAFE_METHODS:
        return True
    value = headers.get(CSRF_HEADER) or headers.get(CSRF_HEADER.lower()) or ""
    return hmac.compare_digest(str(value), CSRF_VALUE)
