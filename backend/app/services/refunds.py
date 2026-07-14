"""Refunds. Admin-only.

The rules that stop us handing back money we never took:

  * you cannot refund an unpaid order
  * you cannot refund more than was actually paid, ever, across ALL refunds combined
  * every refund names the admin who issued it and why
  * pushing the money back through the gateway is a SEPARATE step from recording the
    decision — a gateway failure must not lose the record that a refund was authorised
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AdminUser,
    Order,
    OrderStatus,
    Payment,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentStatus,
    Refund,
    RefundStatus,
)

logger = logging.getLogger(__name__)


class RefundError(Exception):
    """Safe to show an admin."""


def amount_paid(order: Order) -> Decimal:
    """What the customer actually handed over.

    COD counts once the order is DELIVERED — the money changed hands at the door even
    though no gateway was involved. Anything else, only settled gateway payments count.
    """
    if order.payment_method == PaymentMethod.COD:
        if order.status == OrderStatus.DELIVERED or order.payment_status == PaymentStatus.PAID:
            return order.total_amount
        return Decimal("0.00")

    return sum(
        (p.amount for p in order.payments if p.status == PaymentAttemptStatus.PAID),
        Decimal("0.00"),
    )


def amount_refunded(db: Session, order: Order) -> Decimal:
    """Everything already given back, including refunds still pending. A pending refund
    is money we have committed to returning — counting only completed ones would let an
    admin authorise the same refund twice while the first is in flight."""
    rows = db.scalars(
        select(Refund).where(
            Refund.order_id == order.id,
            Refund.status != RefundStatus.FAILED,
        )
    ).all()
    return sum((r.amount for r in rows), Decimal("0.00"))


def refundable(db: Session, order: Order) -> Decimal:
    return max(amount_paid(order) - amount_refunded(db, order), Decimal("0.00"))


def issue_refund(
    db: Session,
    *,
    order: Order,
    admin: AdminUser,
    amount: Decimal | None,
    reason: str,
) -> Refund:
    """Record a refund. Does NOT contact the gateway — see `push_to_gateway`.

    `amount=None` means refund everything still refundable.
    """
    if not reason or not reason.strip():
        # A refund with no stated reason is unauditable, which defeats the point.
        raise RefundError("A reason is required for every refund.")

    available = refundable(db, order)

    if available <= 0:
        paid = amount_paid(order)
        if paid <= 0:
            raise RefundError(
                f"Order {order.order_number} has not been paid — there is nothing to refund."
            )
        raise RefundError(f"Order {order.order_number} has already been fully refunded.")

    value = available if amount is None else Decimal(amount).quantize(Decimal("0.01"))

    if value <= 0:
        raise RefundError("Refund amount must be greater than zero.")

    # The check that matters. Without it, repeated partial refunds could hand back more
    # than the customer ever paid.
    if value > available:
        raise RefundError(
            f"Cannot refund Rs. {value}: only Rs. {available} of "
            f"order {order.order_number} is still refundable."
        )

    # Attach to the settled payment, if there was one (COD has none).
    paid_attempt = next(
        (p for p in order.payments if p.status == PaymentAttemptStatus.PAID), None
    )

    refund = Refund(
        payment_id=paid_attempt.id if paid_attempt else None,
        amount=value,
        reason=reason.strip(),
        status=RefundStatus.PENDING,
        issued_by=admin.id,
    )
    # Same reasoning as start_payment: append to the relationship so `order.refunds` is
    # never stale in the session that just wrote to it.
    order.refunds.append(refund)
    db.flush()

    logger.info(
        "refund %s authorised: Rs. %s on order %s by admin %s (%s)",
        refund.id,
        value,
        order.order_number,
        admin.id,
        reason.strip(),
    )
    return refund


def mark_completed(
    db: Session, refund: Refund, *, provider_ref: str | None = None
) -> Refund:
    """The money is confirmed back with the customer.

    For COD this is an admin ticking it off after handing over cash. For a gateway it is
    the provider's refund API confirming.
    """
    refund.status = RefundStatus.COMPLETED
    refund.provider_ref = provider_ref
    refund.completed_at = datetime.now(timezone.utc)

    order = refund.order
    # Only a FULL refund flips the order's payment status; a partial one leaves it PAID,
    # because the customer is still out of pocket for the rest.
    if amount_refunded(db, order) >= amount_paid(order) > 0:
        order.payment_status = PaymentStatus.REFUNDED

    db.flush()
    return refund


def mark_failed(db: Session, refund: Refund, why: str) -> Refund:
    refund.status = RefundStatus.FAILED
    refund.failure_reason = why
    db.flush()
    return refund
