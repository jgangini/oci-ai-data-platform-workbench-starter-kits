from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path


PBKDF2_ALGORITHM = "pbkdf2_sha256"


def hash_secret(value: str, *, iterations: int = 600_000, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, iterations)
    return "$".join(
        (
            PBKDF2_ALGORITHM,
            str(iterations),
            base64.urlsafe_b64encode(salt).decode().rstrip("="),
            base64.urlsafe_b64encode(digest).decode().rstrip("="),
        )
    )


def verify_secret(value: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != PBKDF2_ALGORITHM:
            return False
        deploy_studio_format = expected_text.endswith("=")
        salt = salt_text.encode() if deploy_studio_format else _b64decode(salt_text)
        expected = base64.b64decode(expected_text) if deploy_studio_format else _b64decode(expected_text)
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, int(iterations_text))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def opaque_rate_limit_key(key: bytes, value: str) -> str:
    return hmac.new(key, value.strip().casefold().encode(), hashlib.sha256).hexdigest()


def load_or_create_session_key(path_text: str) -> bytes:
    path = Path(path_text)
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as stream:
        stream.write(key)
    return key


def issue_session(key: bytes, username: str, *, now: int | None = None, ttl: int = 28_800) -> str:
    payload = json.dumps(
        {"sub": username, "exp": (now or int(time.time())) + ttl, "sid": secrets.token_urlsafe(24)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(key, body.encode(), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def verify_session(token: str, key: bytes, *, now: int | None = None) -> str | None:
    try:
        body, signature_text = token.split(".", 1)
        expected = hmac.new(key, body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_text)):
            return None
        payload = json.loads(_b64decode(body))
        if int(payload["exp"]) <= (now or int(time.time())):
            return None
        return str(payload["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


class RateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _retry_after(self, hits: deque[float], current: float) -> int:
        while hits and current - hits[0] >= self.window_seconds:
            hits.popleft()
        return (
            max(1, math.ceil(self.window_seconds - (current - hits[0])))
            if len(hits) >= self.limit
            else 0
        )

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            return self._retry_after(hits, current)

    def consume(self, key: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            retry_after = self._retry_after(hits, current)
            if retry_after:
                return retry_after
            hits.append(current)
            return 0

    def allow(self, key: str, *, now: float | None = None) -> bool:
        return self.consume(key, now=now) == 0
