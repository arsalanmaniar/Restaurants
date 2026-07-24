"""Wassender outbound client.

Deliberately thin and provider-shaped: `send_text` is the only thing the rest of
the app calls. When we migrate to Meta's official Cloud API (V1→V2 in the plan),
this module is the only thing that changes — with one caveat worth remembering:

Meta only permits free-form messages within 24h of the customer's last inbound
message. Anything outside that window (late order-status updates, broadcasts)
needs a pre-approved template. Wassender has no such restriction, so nothing here
enforces it today; `send_text` is where that check will have to live.

Sync on purpose: the AI engine runs in a background thread, and FastAPI runs sync
background tasks in a threadpool, so there is no event loop to await on.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

WASSENDER_SEND_URL = "https://api.wasenderapi.com/api/send-message"

# WhatsApp hard-caps message bodies; keep well under it.
MAX_BODY_CHARS = 4000


class WhatsAppError(Exception):
    pass


def _normalize_number(number: str) -> str:
    """Strip formatting to just digits — the caller adds any prefix (e.g. '+' for E.164)."""
    cleaned = "".join(ch for ch in number if ch.isdigit())
    if not cleaned:
        raise WhatsAppError(f"unusable WhatsApp number: {number!r}")
    return cleaned


def send_text(to: str, body: str) -> dict:
    if not settings.wassender_api_key:
        # Keeps local dev and tests runnable without live credentials.
        logger.warning("Wassender not configured; would send to %s: %s", to, body)
        return {"sent": False, "reason": "not_configured"}

    headers = {"Authorization": f"Bearer {settings.wassender_api_key}"}
    # Wassender expects E.164 format with a leading '+'.
    phone = _normalize_number(to)
    if not phone.startswith("+"):
        phone = f"+{phone}"
    payload = {
        "to": phone,
        "text": body[:MAX_BODY_CHARS],
    }

    logger.info(f"Wassender request body: {payload}")

    try:
        response = httpx.post(WASSENDER_SEND_URL, json=payload, headers=headers, timeout=20.0)
    except httpx.RequestError as exc:
        logger.error("Wassender request failed: %s", exc)
        raise WhatsAppError(str(exc)) from exc

    # A 200 from Wassender does not guarantee the message reached WhatsApp — their
    # API can queue-then-fail. Log status + raw body every time so we see
    # 'success: false' / queued-status / HTML error pages hidden behind a 200.
    logger.info(f"Wassender response status: {response.status_code}")
    logger.info(f"Wassender response body: {response.text}")

    if response.status_code >= 400:
        raise WhatsAppError(f"Wassender returned {response.status_code}: {response.text}")

    try:
        return response.json()
    except Exception:
        return {}


def send_image(to: str, image_url: str, caption: str | None = None) -> dict:
    """Send an image by public URL, with an optional caption.

    Same Wassender endpoint as send_text — the only difference is the `imageUrl`
    field (verified against Wassender's send-image API). The image must be a
    PUBLICLY reachable JPEG/PNG under 5MB; we pass the URL through untouched and
    never host the bytes ourselves.
    """
    if not settings.wassender_api_key:
        logger.warning(
            "Wassender not configured; would send image to %s: %s", to, image_url
        )
        return {"sent": False, "reason": "not_configured"}

    headers = {"Authorization": f"Bearer {settings.wassender_api_key}"}
    phone = _normalize_number(to)
    if not phone.startswith("+"):
        phone = f"+{phone}"
    payload: dict = {"to": phone, "imageUrl": image_url}
    if caption:
        payload["text"] = caption[:MAX_BODY_CHARS]

    logger.info(f"Wassender image request body: {payload}")

    try:
        response = httpx.post(WASSENDER_SEND_URL, json=payload, headers=headers, timeout=20.0)
    except httpx.RequestError as exc:
        logger.error("Wassender image request failed: %s", exc)
        raise WhatsAppError(str(exc)) from exc

    logger.info(f"Wassender image response status: {response.status_code}")
    logger.info(f"Wassender image response body: {response.text}")

    if response.status_code >= 400:
        raise WhatsAppError(f"Wassender returned {response.status_code}: {response.text}")

    try:
        return response.json()
    except Exception:
        return {}
