"""UltraMsg outbound client.

Deliberately thin and provider-shaped: `send_text` is the only thing the rest of
the app calls. When we migrate to Meta's official Cloud API (V1→V2 in the plan),
this module is the only thing that changes — with one caveat worth remembering:

Meta only permits free-form messages within 24h of the customer's last inbound
message. Anything outside that window (late order-status updates, broadcasts)
needs a pre-approved template. UltraMsg has no such restriction, so nothing here
enforces it today; `send_text` is where that check will have to live.

Sync on purpose: the AI engine runs in a background thread, and FastAPI runs sync
background tasks in a threadpool, so there is no event loop to await on.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

ULTRAMSG_BASE = "https://api.ultramsg.com"

# WhatsApp hard-caps message bodies; keep well under it.
MAX_BODY_CHARS = 4000


class WhatsAppError(Exception):
    pass


def _normalize_number(number: str) -> str:
    """UltraMsg wants a bare international number, no '+' and no separators."""
    cleaned = "".join(ch for ch in number if ch.isdigit())
    if not cleaned:
        raise WhatsAppError(f"unusable WhatsApp number: {number!r}")
    return cleaned


def send_text(to: str, body: str) -> dict:
    if not settings.ultramsg_instance_id or not settings.ultramsg_token:
        # Keeps local dev and tests runnable without live credentials.
        logger.warning("UltraMsg not configured; would send to %s: %s", to, body)
        return {"sent": False, "reason": "not_configured"}

    url = f"{ULTRAMSG_BASE}/{settings.ultramsg_instance_id}/messages/chat"
    payload = {
        "token": settings.ultramsg_token,
        "to": _normalize_number(to),
        "body": body[:MAX_BODY_CHARS],
    }

    try:
        response = httpx.post(url, data=payload, timeout=20.0)
    except httpx.RequestError as exc:
        logger.error("UltraMsg request failed: %s", exc)
        raise WhatsAppError(str(exc)) from exc

    if response.status_code >= 400:
        logger.error("UltraMsg send failed (%s): %s", response.status_code, response.text)
        raise WhatsAppError(f"UltraMsg returned {response.status_code}: {response.text}")

    return response.json()
