"""Wassender inbound webhook.

Wassender POSTs a `messages.received` event with the message nested two levels
deep under `data.messages` (verified against real webhook deliveries):

    {"event": "messages.received",
     "data": {
       "messages": {
         "id": "3AFE...",
         "messageBody": "hi",
         "message": {"conversation": "hi", ...},   # WhatsApp-native repr, unused
         "key": {
           "id": "3AFE...",                        # same value as messages.id
           "cleanedSenderPn": "923001234567",      # phone, already stripped
           "fromMe": false
         }
       }
     }}

Two things this endpoint must get right:
  * `fromMe: true` events are our OWN outbound messages echoed back. Replying to
    them would make the bot talk to itself in a loop.
  * Wassender retries on non-2xx and can double-deliver. We de-dupe on the
    provider message id, and we always return 200 so a bug in our AI never turns
    into an infinite retry storm.

Non-text messages (images/audio/etc.) simply arrive without a `messageBody` and
are dropped silently by the empty-body guard below. If we ever want to reply
"text only please" for media, we need to identify which field carries the message
type in Wassender's real payload (not in the spec yet).
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


def _dig(source, *keys, default=None):
    """Walk a nested dict safely; return default if any intermediate key misses.
    Wassender wraps its message payload two levels deep (`data.messages.key.*`),
    and one missing intermediate would otherwise raise AttributeError on None.
    """
    node = source
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return node if node is not None else default


@router.post("/webhooks/wassender")
async def wassender_webhook(
    request: Request,
    background: BackgroundTasks,
    secret: str = Query(default=""),
):
    # Wassender does not sign its webhooks, so the only thing standing between the
    # public internet and our AI is this shared secret in the URL. Configure the
    # webhook as: https://<host>/webhooks/wassender?secret=<WASSENDER_WEBHOOK_SECRET>
    if not settings.wassender_webhook_secret:
        # An unset secret used to mean "let everyone in", which is a public endpoint
        # that spends money on Groq calls and writes to our database. In debug we
        # allow it (local testing); in production we refuse to serve at all.
        if not settings.debug:
            logger.error("WASSENDER_WEBHOOK_SECRET is not set; refusing webhook traffic")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook is not configured"
            )
        logger.warning("WASSENDER_WEBHOOK_SECRET unset — accepting unauthenticated webhook (debug)")
    elif not secrets.compare_digest(secret, settings.wassender_webhook_secret):
        # Constant-time: a plain != leaks the secret one character at a time to
        # anyone willing to measure response latency.
        logger.warning("rejected webhook call with bad secret")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook secret")

    logger.info(f"Wassender webhook body received: {await request.body()}")

    try:
        payload = await request.json()
    except (ValueError, JSONDecodeError):
        # Malformed body: nothing to process, and returning 4xx would make Wassender
        # retry a payload that will never parse.
        logger.warning("webhook received a non-JSON body")
        return {"status": "ignored", "reason": "invalid JSON"}

    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "unexpected payload shape"}

    if payload.get("event") != "messages.received":
        return {"status": "ignored", "reason": "not a message event"}

    if _dig(payload, "data", "messages", "key", "fromMe"):
        return {"status": "ignored", "reason": "own message"}

    sender = _dig(payload, "data", "messages", "key", "cleanedSenderPn", default="")
    body = (_dig(payload, "data", "messages", "messageBody") or "").strip()
    message_id = _dig(payload, "data", "messages", "id") or _dig(payload, "data", "messages", "key", "id")

    if not sender or not body:
        return {"status": "ignored", "reason": "empty message"}

    # Cap the inbound text. WhatsApp permits very long messages and every character
    # is fed to Groq — an oversized paste is a direct hit to the token bill, and no
    # genuine food order needs this much room.
    if len(body) > MAX_INBOUND_CHARS:
        logger.warning("truncating oversized message (%s chars) from %s", len(body), sender)
        body = body[:MAX_INBOUND_CHARS]

    # Do the AI work off the request path: Wassender times out fast, and a slow
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
