"""Password hashing, token generation and session signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

_hasher = PasswordHasher()

# Excludes look-alike characters so a generated root password can be read off a
# screen and typed without ambiguity.
_PASSWORD_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#%^*_-+="
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def generate_password(length: int = 20) -> str:
    """Generate a root password with at least one character from each class."""
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(not c.isalnum() for c in candidate)
        ):
            return candidate


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash a bearer token for storage.

    Plain SHA-256 rather than Argon2: API tokens are full-entropy random values,
    so there is nothing to brute-force, and auth must stay cheap enough to run on
    every request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_api_token() -> tuple[str, str, str]:
    """Return `(plaintext, hash, prefix)` for a new API token."""
    settings = get_settings()
    raw = generate_token(32)
    plaintext = f"{settings.api_token_prefix}_{raw}"
    return plaintext, hash_token(plaintext), plaintext[:12]


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sign_webhook(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_webhook(payload, secret), signature)


def ssh_key_fingerprint(public_key: str) -> str:
    """SHA256 fingerprint in the format `ssh-keygen -lf` prints."""
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise ValueError("Public key must look like '<type> <base64> [comment]'")
    key_type, b64 = parts[0], parts[1]
    if not key_type.startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError(f"Unsupported key type: {key_type}")
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError("Public key body is not valid base64") from exc
    if not blob:
        raise ValueError("Public key body is empty")
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


# --- signed session cookies -------------------------------------------------


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt)


def sign_session(user_id: str) -> str:
    return _serializer("session").dumps({"uid": user_id})


def load_session(token: str, max_age: int | None = None) -> str | None:
    settings = get_settings()
    try:
        data = _serializer("session").loads(
            token, max_age=max_age or settings.session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) else None


def sign_value(value: str, salt: str) -> str:
    return _serializer(salt).dumps(value)


def load_value(token: str, salt: str, max_age: int) -> str | None:
    try:
        value = _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return value if isinstance(value, str) else None


def utcnow() -> datetime:
    return datetime.now(UTC)


def expires_in(seconds: int) -> datetime:
    return utcnow() + timedelta(seconds=seconds)
