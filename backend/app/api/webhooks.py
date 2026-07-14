"""UltraMsg inbound webhook.

UltraMsg POSTs a payload shaped like:

    {"event_type": "message_received",
     "instanceId": "12345",
     "data": {"id": "true_923...@c.us_3EB0...", "from": "923001234567@c.us",
              "to": "923009999999@c.us", "body": "hi", "type": "chat",
              "fromMe": false, ...}}

Two things this endpoint must get right:
  * `fromMe: true` events are our OWN outbound messages echoed back. Replying to
    them would make the bot talk to itself in a loop.
  * UltraMsg retries on non-2xx and can double-deliver. We de-dupe on the
    provider message id, and we always return 200 so a bug in our AI never turns
    into an infinite retry storm.
"""

import logging
import secrets
from json import JSONDecodeError

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import MessageDirection
from app.services import conversations as convo
from app.services.agent import handle_incoming_message
from app.services.whatsapp import send_text

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])

# Generous for a food order, small enough that a paste-bomb can't run up the bill.
MAX_INBOUND_CHARS = 1500


def _extract_number(jid: str) -> str:
    """'923001234567@c.us' -> '923001234567'"""
    return jid.split("@", 1)[0]


@router.post("/webhooks/ultramsg")
async def ultramsg_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str = Query(default=""),
):
    # UltraMsg does not sign its webhooks, so the only thing standing between the
    # public internet and our AI is this shared secret in the URL. Configure the
    # webhook as: https://<host>/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>
    if not settings.ultramsg_webhook_secret:
        # An unset secret used to mean "let everyone in", which is a public endpoint
        # that spends money on Groq calls and writes to our database. In debug we
        # allow it (local testing); in production we refuse to serve at all.
        if not settings.debug:
            logger.error("ULTRAMSG_WEBHOOK_SECRET is not set; refusing webhook traffic")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook is not configured"
            )
        logger.warning("ULTRAMSG_WEBHOOK_SECRET unset — accepting unauthenticated webhook (debug)")
    elif not secrets.compare_digest(secret, settings.ultramsg_webhook_secret):
        # Constant-time: a plain != leaks the secret one character at a time to
        # anyone willing to measure response latency.
        logger.warning("rejected webhook call with bad secret")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook secret")

    try:
        payload = await request.json()
    except (ValueError, JSONDecodeError):
        # Malformed body: nothing to process, and returning 4xx would make UltraMsg
        # retry a payload that will never parse.
        logger.warning("webhook received a non-JSON body")
        return {"status": "ignored", "reason": "invalid JSON"}

    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "unexpected payload shape"}

    if payload.get("event_type") != "message_received":
        return {"status": "ignored", "reason": "not a message event"}

    data = payload.get("data") or {}

    if data.get("fromMe"):
        return {"status": "ignored", "reason": "own message"}

    # Phase 0 is text-only. Images/audio/location get a polite nudge rather than
    # silence, so the customer isn't left staring at a dead chat.
    if data.get("type") != "chat":
        sender = _extract_number(data.get("from", ""))
        if sender:
            background.add_task(
                send_text, sender, "Sorry, I can only read text messages right now 🙏"
            )
        return {"status": "ignored", "reason": f"unsupported type {data.get('type')}"}

    sender = _extract_number(data.get("from", ""))
    body = (data.get("body") or "").strip()
    message_id = data.get("id")

    if not sender or not body:
        return {"status": "ignored", "reason": "empty message"}

    # Cap the inbound text. WhatsApp permits very long messages and every character
    # is fed to Groq — an oversized paste is a direct hit to the token bill, and no
    # genuine food order needs this much room.
    if len(body) > MAX_INBOUND_CHARS:
        logger.warning("truncating oversized message (%s chars) from %s", len(body), sender)
        body = body[:MAX_INBOUND_CHARS]

    # Do the AI work off the request path: UltraMsg times out fast, and a slow
    # Groq call would otherwise trigger a retry (and a duplicate reply).
    background.add_task(_process_message, sender, body, message_id)
    return {"status": "accepted"}


def _process_message(sender: str, body: str, message_id: str | None) -> None:
    db: Session = SessionLocal()
    try:
        if message_id and convo.already_processed(db, message_id):
            logger.info("skipping duplicate delivery of %s", message_id)
            return

        customer = convo.get_or_create_customer(db, sender)

        # is_blocked existed on the model but nothing enforced it. Blocked numbers are
        # dropped silently — replying "you are blocked" just invites an argument.
        if customer.is_blocked:
            logger.info("dropping message from blocked customer %s", sender)
            db.commit()
            return

        conversation = convo.get_or_create_conversation(db, customer)

        convo.log_message(
            db,
            conversation,
            MessageDirection.INBOUND,
            body,
            provider_message_id=message_id,
        )
        db.commit()

        handle_incoming_message(db, conversation, body)
    except Exception:
        db.rollback()
        logger.exception("failed handling message from %s", sender)
    finally:
        db.close()
