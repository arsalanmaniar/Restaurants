"""Cheap, deterministic pre-filter that runs BEFORE the Groq call.

The goal is to keep obviously-not-a-food-order traffic from burning tokens.
Every function here is pure Python or a single indexed Postgres query — no
LLM, no HTTP calls, no third-party services. The trade-off is intentional:
we err on the side of a false-positive redirect (which is cheap and easy
for the customer to recover from) over letting broadcast forwards, YouTube
links, MLM spam, or a repeat-spam attack drain the daily Groq quota.

Real incidents this prevents (all observed in production traffic):
    conv#644  MLM investment forward, ~400 chars of *bold* markers
    conv#645  auto-forwarded job listings with URLs
    conv#647  news forward (Iran/Trump, Urdu, *bold* headlines)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Conversation, MessageLog
from app.models.enums import MessageDirection


OFFTOPIC_REDIRECT = (
    "I'm here to help you order food! What would you like to eat today? 🍴"
)
RATE_LIMITED_REPLY = (
    "You're sending messages very fast — please slow down for a moment 🙂"
)

# 15/minute is a comfortable ceiling: a real ordering conversation is 5–10
# turns, and even an anxious customer resending the same message a few times
# stays well under this.
MAX_MESSAGES_PER_MINUTE = 15
RATE_LIMIT_WINDOW_SECONDS = 60

# Real customer messages we have seen top out around 200 chars (long delivery
# instructions with landmarks). 500 is generous enough to leave those alone
# while catching news forwards and MLM broadcasts.
MAX_LEGITIMATE_LENGTH = 500

# Broadcast forwards on Pakistani WhatsApp overwhelmingly use *bold* markers
# around every "headline" — three or more in one message is essentially never
# a customer typing to order.
_BOLD_MARKER_RE = re.compile(r"\*[^*\n]+\*")

# Any URL in the body: customers order food by typing what they want, not by
# pasting links. YouTube, careerjoin.com, MLM sign-up links — all caught.
# Google Maps location links are a possible false positive but rare (WhatsApp
# has native location share for that, handled separately).
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def is_offtopic(text: str | None) -> bool:
    """True if the message looks like it is not a food-ordering attempt."""
    if not text:
        return False
    if _URL_RE.search(text):
        return True
    if len(text) > MAX_LEGITIMATE_LENGTH:
        return True
    if len(_BOLD_MARKER_RE.findall(text)) >= 3:
        return True
    return False


def recent_inbound_count(db: Session, conversation_id: int, window_seconds: int) -> int:
    """How many INBOUND messages this conversation has logged in the window."""
    since = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    stmt = select(func.count(MessageLog.id)).where(
        MessageLog.conversation_id == conversation_id,
        MessageLog.direction == MessageDirection.INBOUND,
        MessageLog.created_at >= since,
    )
    return db.scalar(stmt) or 0


def is_rate_limited(db: Session, conversation: Conversation) -> bool:
    """True when the customer has exceeded MAX_MESSAGES_PER_MINUTE in the
    rate-limit window. The current inbound is already logged upstream, so it
    counts toward the total."""
    return (
        recent_inbound_count(db, conversation.id, RATE_LIMIT_WINDOW_SECONDS)
        > MAX_MESSAGES_PER_MINUTE
    )


def already_notified_rate_limit(db: Session, conversation: Conversation) -> bool:
    """True if the most recent OUTBOUND on this conversation was the
    rate-limit notice. Used to keep us from replying "please slow down" once
    per inbound in a sustained spam burst — the customer heard it the first
    time, and repeating it just adds our own noise to theirs."""
    last_out = db.scalar(
        select(MessageLog)
        .where(
            MessageLog.conversation_id == conversation.id,
            MessageLog.direction == MessageDirection.OUTBOUND,
        )
        .order_by(MessageLog.id.desc())
        .limit(1)
    )
    return last_out is not None and last_out.content == RATE_LIMITED_REPLY
