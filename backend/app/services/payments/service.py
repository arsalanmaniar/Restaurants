"""Payment orchestration.

Every rule that protects money lives here, not in the routes:

  * the callback's amount is NEVER trusted — it is compared against what we recorded
  * a replayed callback must not double-apply (gateways retry)
  * a payment can only settle the order it belongs to
  * marking an order paid is the ONLY thing that makes it visible to a restaurant
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Order,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentAttemptStatus,
    PaymentProviderName,
    PaymentStatus,
)
from app.services.payments.base import CallbackResult, Checkout, CheckoutRequest
from app.services.payments.registry import get_provider
from app.services.payments.tokens import make_pay_token

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Something is wrong with the payment itself — safe to surface to a customer."""


def _new_txn_ref(order: Order, attempt: int) -> str:
    # Unique per ATTEMPT, not per order: gateways reject a reused transaction reference,
    # and a customer retrying after a decline must get a fresh one. The random suffix
    # avoids a collision if two attempts are created in the same instant.
    return f"{order.order_number}-{attempt}-{secrets.token_hex(2).upper()}"


def start_payment(
    db: Session, order: Order, provider_name: PaymentProviderName
) -> tuple[Payment, str]:
    """Create a payment attempt and return it with the link to send the customer."""
    if order.payment_status == PaymentStatus.PAID:
        raise PaymentError(f"Order {order.order_number} has already been paid.")

    attempt = len(order.payments) + 1
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.payment_expiry_minutes)

    payment = Payment(
        provider=provider_name,
        txn_ref=_new_txn_ref(order, attempt),
        amount=order.total_amount,
        status=PaymentAttemptStatus.INITIATED,
        expires_at=expires_at,
    )
    # Append to the relationship rather than setting order_id by hand. Setting the FK
    # directly leaves an already-loaded `order.payments` stale in this session, and the
    # attempt counter above reads exactly that collection — so a retry could reuse a
    # transaction reference, which gateways reject.
    order.payments.append(payment)
    db.flush()

    # The link carries a signed token, not the order id or the amount. A customer must
    # never be able to edit a URL and change what they owe.
    token = make_pay_token(payment.id, expires_at)
    return payment, f"{settings.public_base_url.rstrip('/')}/pay/{token}"


def build_checkout(db: Session, payment: Payment) -> Checkout:
    """Build the gateway form for an attempt (called when the customer opens the link)."""
    if payment.status != PaymentAttemptStatus.INITIATED:
        raise PaymentError("This payment link has already been used.")

    if payment.expires_at <= datetime.now(timezone.utc):
        raise PaymentError("This payment link has expired.")

    provider = get_provider(payment.provider)
    checkout = provider.create_checkout(
        CheckoutRequest(
            txn_ref=payment.txn_ref,
            amount=payment.amount,
            description=f"AbhiAya order {payment.order.order_number}",
            return_url=(
                f"{settings.public_base_url.rstrip('/')}"
                f"/webhooks/payments/{payment.provider.value}/callback"
            ),
            customer_number=payment.order.customer.whatsapp_number,
        )
    )

    payment.raw_request = checkout.raw_request
    db.flush()
    return checkout


def apply_callback(db: Session, result: CallbackResult) -> Payment:
    """Settle a payment from a VERIFIED callback.

    The caller must already have checked the signature — by the time we get here the
    message is known to come from the gateway. Everything below guards against the
    gateway (or a replay of its message) telling us something inconsistent.
    """
    payment = db.scalar(select(Payment).where(Payment.txn_ref == result.txn_ref))
    if payment is None:
        # Not ours. Could be a misrouted callback or someone probing the endpoint.
        raise PaymentError(f"Unknown transaction reference {result.txn_ref!r}")

    # Idempotency. Gateways retry callbacks, and a replay must not re-run any of this.
    if payment.status != PaymentAttemptStatus.INITIATED:
        logger.info(
            "ignoring duplicate callback for %s (already %s)",
            payment.txn_ref,
            payment.status.value,
        )
        return payment

    payment.raw_response = result.raw
    payment.provider_ref = result.provider_ref

    if not result.successful:
        payment.status = PaymentAttemptStatus.FAILED
        payment.failure_reason = result.failure_reason
        db.flush()
        # The order stays AWAITING_PAYMENT so the customer can retry with a new link.
        return payment

    # Amount check. The gateway saying "paid" is not enough — it must have collected the
    # amount we asked for. A mismatch means either a bug or someone tampering, and either
    # way we must not release the order to the kitchen.
    if result.amount is not None and result.amount != payment.amount:
        logger.error(
            "AMOUNT MISMATCH on %s: expected Rs. %s, gateway reported Rs. %s",
            payment.txn_ref,
            payment.amount,
            result.amount,
        )
        payment.status = PaymentAttemptStatus.FAILED
        payment.failure_reason = (
            f"amount mismatch: expected {payment.amount}, gateway said {result.amount}"
        )
        db.flush()
        # Deliberately NOT an exception. Raising here made the caller roll back — which
        # erased the record of the tampering, the one thing worth keeping. The order is
        # left unpaid and unreleased; the caller reports the FAILED status.
        return payment

    payment.status = PaymentAttemptStatus.PAID
    payment.paid_at = datetime.now(timezone.utc)

    _release_order(db, payment.order, payment)
    db.flush()
    return payment


def _release_order(db: Session, order: Order, payment: Payment) -> None:
    """Mark the order paid and hand it to the restaurant.

    This is the moment the kitchen first sees the order. Nothing else in the codebase
    may move an order out of AWAITING_PAYMENT.
    """
    order.payment_status = PaymentStatus.PAID

    if order.status == OrderStatus.AWAITING_PAYMENT:
        order.status = OrderStatus.PENDING
        order.status_history.append(
            OrderStatusHistory(
                status=OrderStatus.PENDING,
                changed_by=f"payment:{payment.provider.value}",
                note=f"Paid Rs. {payment.amount} (ref {payment.provider_ref or payment.txn_ref})",
            )
        )
        logger.info("order %s paid and released to the restaurant", order.order_number)


def expire_payment(db: Session, payment: Payment, *, cancel_order: bool = True) -> None:
    """A payment ran out of time. Kill the attempt, and cancel the order unless another
    attempt is still alive."""
    payment.status = PaymentAttemptStatus.EXPIRED
    payment.failure_reason = "not paid before expiry"

    order = payment.order
    if not cancel_order or order.status != OrderStatus.AWAITING_PAYMENT:
        db.flush()
        return

    live = [
        p
        for p in order.payments
        if p.id != payment.id and p.status == PaymentAttemptStatus.INITIATED
    ]
    if live:
        # The customer asked for a fresh link and may still pay on it.
        db.flush()
        return

    order.status = OrderStatus.CANCELLED
    order.status_history.append(
        OrderStatusHistory(
            status=OrderStatus.CANCELLED,
            changed_by="system",
            note="Cancelled — payment was not completed in time",
        )
    )
    logger.info("order %s cancelled: payment expired", order.order_number)
    db.flush()


def total_paid(order: Order) -> Decimal:
    return sum(
        (p.amount for p in order.payments if p.status == PaymentAttemptStatus.PAID),
        Decimal("0.00"),
    )
