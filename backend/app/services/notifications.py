"""Outbound WhatsApp notifications the platform INITIATES.

Everything else that sends a WhatsApp message does so as a REPLY to a customer
message, inside the webhook → agent path. This module is the first thing that
talks to a customer without being spoken to: when a restaurant accepts or
cancels an order from its dashboard, we reach out.

Design:
  * The message is composed here from the order's stored amounts — never
    recomputed. This is a READ of subtotal/delivery_fee/total_amount, so it
    carries no financial risk (the tax work in a later phase changes how those
    are computed, not how they are read).
  * The send + log runs off the request path in a background task with its OWN
    DB session — the dashboard's request session is gone by the time it fires,
    and a slow Wassender call must not block the dashboard response.
  * Every notification is logged to messages_log as an OUTBOUND, exactly like an
    AI reply, so the conversation transcript stays complete. A `meta.notification`
    tag distinguishes it from a conversational reply.
"""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import Conversation, Customer, MessageDirection, Order, OrderStatus
from app.services import conversations as convo
from app.services.whatsapp import WhatsAppError, send_text

logger = logging.getLogger(__name__)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _bill_message(order: Order) -> str:
    """The itemised bill sent when a restaurant ACCEPTS an order — this is the
    customer's 'confirmed' moment. Reads the order's stored amounts verbatim."""
    lines = [
        f"{item.quantity}x {item.item_name} — Rs. {_money(item.line_total)}"
        for item in order.items
    ]
    body = [
        f"Aap ka order confirm ho gaya hai — {order.restaurant.name} ne accept kar liya hai.",
        "",
        f"Order: {order.order_number}",
        "",
        *lines,
        "",
        f"Subtotal: Rs. {_money(order.subtotal)}",
    ]
    if order.discount_amount and order.discount_amount > 0:
        body.append(f"Discount: -Rs. {_money(order.discount_amount)}")
    if order.tax_amount and order.tax_amount > 0:
        body.append(f"Tax ({order.tax_rate:.0f}%): Rs. {_money(order.tax_amount)}")
    body.append(f"Delivery: Rs. {_money(order.delivery_fee)}")
    body.append(f"Total: Rs. {_money(order.total_amount)}")
    body.append("")
    body.append(f"Payment: {order.payment_method.value}")
    if order.delivery_address_text:
        body.append(f"Address: {order.delivery_address_text}")
    body.append("")
    body.append("Restaurant ne taiyari shuru kar di hai. Shukriya!")
    return "\n".join(body)


def _cancellation_message(order: Order) -> str:
    """Sent when a restaurant CANCELS an order from the dashboard."""
    return (
        f"Maaf kijiye, aap ka order {order.order_number} ({order.restaurant.name}) "
        "cancel kar diya gaya hai. Agar is par koi charge hua tha to wo wapas kar "
        "diya jayega. Aap dobara order karna chahein to bata dijiye — hum madad "
        "kar denge."
    )


# Status → message builder. A status with no entry here produces no customer
# notification (e.g. PREPARING / READY are internal-only for now). Keyed on the
# order's NEW status, so adding later statuses is a one-line change.
_BUILDERS = {
    OrderStatus.ACCEPTED: _bill_message,
    OrderStatus.CANCELLED: _cancellation_message,
}


def build_notification(order: Order) -> str | None:
    """The customer-facing message for this order's CURRENT status, or None if
    this status is not one the customer is notified about."""
    builder = _BUILDERS.get(order.status)
    return builder(order) if builder is not None else None


def _conversation_for_notification(db: Session, customer: Customer) -> Conversation:
    """The customer's most recent conversation, or a fresh one if they have none.

    Unlike the webhook path we deliberately do NOT apply the 6-hour staleness
    rule: a notification belongs in the thread the customer already knows, even
    if they went quiet for a day after ordering. Reusing an old conversation is
    harmless — the notification is only logged, it does not run the AI, and the
    customer's next inbound still goes through the normal staleness check.
    """
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.customer_id == customer.id)
        .order_by(Conversation.last_message_at.desc())
        .limit(1)
    )
    if conversation is None:
        conversation = convo.get_or_create_conversation(db, customer)
    return conversation


def send_order_notification(db: Session, order: Order) -> bool:
    """Compose, send, and log the notification for `order`'s current status.

    Returns True if a message was sent (or attempted), False if this status has
    no customer notification. Takes an explicit session so it is testable
    directly; the background entry point below supplies one.
    """
    message = build_notification(order)
    if message is None:
        return False

    customer = order.customer
    conversation = _conversation_for_notification(db, customer)

    try:
        send_text(customer.whatsapp_number, message)
    except WhatsAppError:
        # Log the attempt anyway — the transcript should show what we tried to
        # say even when Wassender was unreachable.
        logger.exception(
            "could not deliver %s notification for order %s",
            order.status.value,
            order.order_number,
        )

    convo.log_message(
        db,
        conversation,
        MessageDirection.OUTBOUND,
        message,
        meta={"notification": order.status.value, "order_number": order.order_number},
    )
    db.commit()
    return True


def notify_order_status(order_id: int) -> None:
    """Background-task entry point: open a fresh session and notify.

    Called via BackgroundTasks from the dashboard status endpoint — the request's
    own session is already closed by the time this runs.
    """
    db = SessionLocal()
    try:
        order = db.get(Order, order_id)
        if order is None:
            logger.warning("notify_order_status: order %s vanished", order_id)
            return
        send_order_notification(db, order)
    except Exception:
        db.rollback()
        logger.exception("failed to notify customer for order %s", order_id)
    finally:
        db.close()
