from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

# bcrypt silently ignores anything past 72 bytes, so we truncate explicitly rather
# than let a long password hash to the same value as its 72-byte prefix.
BCRYPT_MAX_BYTES = 72


def _prepare(plain: str) -> bytes:
    return plain.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prepare(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash in the DB — treat as a failed login, not a 500.
        return False


def create_access_token(subject: str, role: str, restaurant_id: int | None = None) -> str:
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,  # "admin" | "restaurant"
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    if restaurant_id is not None:
        payload["restaurant_id"] = restaurant_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
