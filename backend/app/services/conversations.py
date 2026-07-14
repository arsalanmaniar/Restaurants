"""Customer + conversation lookup, and the message audit log."""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, ConversationState, Customer, MessageDirection, MessageLog

# A conversation that has been quiet this long is treated as finished; the next
# message starts a fresh one rather than resuming a stale half-built cart.
CONVERSATION_IDLE_TIMEOUT = timedelta(hours=6)


def get_or_create_customer(db: Session, whatsapp_number: str) -> Customer:
    customer = db.scalar(select(Customer).where(Customer.whatsapp_number == whatsapp_number))
    if customer is None:
        customer = Customer(whatsapp_number=whatsapp_number)
        db.add(customer)
        db.flush()
    return customer


def get_or_create_conversation(db: Session, customer: Customer) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.customer_id == customer.id)
        .order_by(Conversation.last_message_at.desc())
        .limit(1)
    )

    if conversation is not None and not _is_stale(conversation):
        return conversation

    conversation = Conversation(
        customer_id=customer.id,
        state=ConversationState.GREETING,
        cart={"items": []},
        context={},
    )
    db.add(conversation)
    db.flush()
    return conversation


def _is_stale(conversation: Conversation) -> bool:
    # A conversation parked in HUMAN_HANDOFF stays open until a human closes it.
    if conversation.state == ConversationState.HUMAN_HANDOFF:
        return False
    last = conversation.last_message_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > CONVERSATION_IDLE_TIMEOUT


def already_processed(db: Session, provider_message_id: str) -> bool:
    """UltraMsg retries webhook deliveries; without this, a retry re-runs the AI
    and can place the same order twice."""
    return (
        db.scalar(
            select(MessageLog.id)
            .where(MessageLog.provider_message_id == provider_message_id)
            .limit(1)
        )
        is not None
    )


def log_message(
    db: Session,
    conversation: Conversation,
    direction: MessageDirection,
    content: str | None,
    provider_message_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> MessageLog:
    entry = MessageLog(
        conversation_id=conversation.id,
        direction=direction,
        content=content,
        provider_message_id=provider_message_id,
        meta=meta,
    )
    db.add(entry)
    conversation.last_message_at = datetime.now(timezone.utc)
    db.flush()
    return entry


def recent_history(db: Session, conversation: Conversation, limit: int = 12) -> list[MessageLog]:
    """Last N messages, oldest-first, for replaying into the AI's context."""
    rows = db.scalars(
        select(MessageLog)
        .where(MessageLog.conversation_id == conversation.id)
        .order_by(MessageLog.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))
