import base64
import hashlib
import hmac

from app.security import RateLimiter, hash_secret, issue_session, opaque_rate_limit_key, verify_secret, verify_session


def test_pbkdf2_round_trip_and_wrong_value() -> None:
    encoded = hash_secret("ABCD-1234", iterations=1_000, salt=b"fixed-test-salt")
    assert verify_secret("ABCD-1234", encoded)
    assert not verify_secret("WXYZ-9999", encoded)
    assert not verify_secret("ABCD-1234", "not-a-hash")


def test_pbkdf2_accepts_deploy_studio_format() -> None:
    value = "AIDP-2026"
    salt = "deploy-studio-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 1_000)
    encoded = f"pbkdf2_sha256$1000${salt}${base64.b64encode(digest).decode()}"
    assert verify_secret(value, encoded)
    assert not verify_secret("WRONG-0000", encoded)


def test_session_rejects_tampering_and_expiry() -> None:
    token = issue_session(b"k" * 32, "admin", now=100, ttl=10)
    assert verify_session(token, b"k" * 32, now=109) == "admin"
    assert verify_session(token + "x", b"k" * 32, now=109) is None
    assert verify_session(token, b"k" * 32, now=110) is None


def test_logins_in_same_second_are_isolated_and_legacy_cookies_still_verify() -> None:
    from app.territorial.agent_gateway import scoped_session
    key = b"k" * 32
    first, second = [issue_session(key, "admin", now=100, ttl=10) for _ in range(2)]
    assert first != second
    assert verify_session(first, key, now=109) == verify_session(second, key, now=109) == "admin"
    conversation = {"session_id": "105dbb52-bba9-45a2-b40d-357a31db8f13"}
    assert scoped_session(conversation, first, key) != scoped_session(conversation, second, key)
    body = base64.urlsafe_b64encode(b'{"exp":110,"sub":"admin"}').decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.digest(key, body.encode(), "sha256")).decode().rstrip("=")
    assert verify_session(body + "." + signature, key, now=109) == "admin"


def test_rate_limiter_is_windowed() -> None:
    limiter = RateLimiter(limit=2, window_seconds=10)
    assert limiter.allow("ip", now=0)
    assert limiter.allow("ip", now=1)
    assert not limiter.allow("ip", now=2)
    assert limiter.retry_after("ip", now=2) == 8
    assert limiter.allow("ip", now=10)


def test_rate_limit_keys_are_stable_and_opaque() -> None:
    first = opaque_rate_limit_key(b"k" * 32, " Ada@Example.com ")
    assert first == opaque_rate_limit_key(b"k" * 32, "ada@example.com")
    assert first != opaque_rate_limit_key(b"k" * 32, "grace@example.com")
    assert "ada" not in first
