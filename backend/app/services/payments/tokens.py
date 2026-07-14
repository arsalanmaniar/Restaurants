"""Signed payment-link tokens.

The link we send over WhatsApp must not contain the order id or the amount — a customer
who can edit `?order=123&amount=50` in a URL will. The token is a signed, expiring
reference to a payment ATTEMPT and carries no other authority.
"""

from datetime import datetime

from jose import JWTError, jwt

from app.core.config import settings

PURPOSE = "pay"


def make_pay_token(payment_id: int, expires_at: datetime) -> str:
    return jwt.encode(
        {"sub": str(payment_id), "purpose": PURPOSE, "exp": expires_at},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def read_pay_token(token: str) -> int | None:
    """Payment id, or None if the token is invalid, expired, or not a payment token."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None

    # A login token must never be usable as a payment link, or vice versa.
    if payload.get("purpose") != PURPOSE:
        return None

    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None
