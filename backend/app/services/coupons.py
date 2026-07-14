"""Coupons. The rules that stop a discount from becoming a revenue leak:

  * the PLATFORM absorbs every discount — the restaurant is always paid on the FULL,
    undiscounted subtotal, and only the platform's own commission shrinks
  * platform commission is never allowed to go negative: if the discount is bigger
    than the commission on this order, the platform is choosing to pay to acquire the
    order, and commission clamps to zero rather than the order "costing" the platform
    money in the ledger
  * every limit (usage cap, once-per-customer, minimum order, date range) is
    re-checked here, server-side, from the DB — the AI only ever passes a code
  * "today" for the valid_from/valid_to range is today in Asia/Karachi, never UTC —
    see app/services/opening_hours.py, which this mirrors
"""

from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Coupon, CouponDiscountType, CouponRedemption, Order
from app.services.opening_hours import PAKISTAN_TZ


class CouponError(Exception):
    """Safe to show a customer — always says why, never leaks internals."""


class CouponApplication(NamedTuple):
    coupon: Coupon
    discount_amount: Decimal


def _today(at: datetime | None) -> date:
    return (at or datetime.now(PAKISTAN_TZ)).astimezone(PAKISTAN_TZ).date()


def _find_coupon(db: Session, code: str) -> Coupon | None:
    normalized = code.strip().upper()
    if not normalized:
        return None
    return db.scalar(select(Coupon).where(Coupon.code == normalized))


def _usage_count(db: Session, coupon_id: int) -> int:
    return db.scalar(
        select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon_id)
    )


def _already_redeemed(db: Session, coupon_id: int, customer_id: int) -> bool:
    return (
        db.scalar(
            select(CouponRedemption.id).where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.customer_id == customer_id,
            )
        )
        is not None
    )


def _compute_discount(coupon: Coupon, subtotal: Decimal) -> Decimal:
    if coupon.discount_type == CouponDiscountType.PERCENTAGE:
        raw = subtotal * coupon.value / Decimal("100")
        if coupon.max_discount_amount is not None:
            raw = min(raw, coupon.max_discount_amount)
    else:
        raw = coupon.value

    # A discount can never exceed the subtotal it applies to — a Rs. 5000 fixed
    # coupon on a Rs. 700 order gives Rs. 700 off, not a negative total.
    return min(raw, subtotal).quantize(Decimal("0.01"))


def validate_coupon(
    db: Session,
    *,
    code: str,
    restaurant_id: int,
    customer_id: int,
    subtotal: Decimal,
    at: datetime | None = None,
) -> CouponApplication:
    """Look up a coupon and check every rule. Raises CouponError with a message safe
    to relay to the customer verbatim; returns the computed discount on success.

    `subtotal` must be the full, undiscounted cart subtotal from the DB-priced cart —
    never a number the model invented.
    """
    coupon = _find_coupon(db, code)
    if coupon is None:
        raise CouponError(f"'{code}' is not a valid coupon code.")

    if not coupon.is_active:
        raise CouponError(f"Coupon '{coupon.code}' is no longer active.")

    if coupon.restaurant_id is not None and coupon.restaurant_id != restaurant_id:
        raise CouponError(f"Coupon '{coupon.code}' is not valid at this restaurant.")

    today = _today(at)
    if coupon.valid_from is not None and today < coupon.valid_from:
        raise CouponError(f"Coupon '{coupon.code}' is not valid yet.")
    if coupon.valid_to is not None and today > coupon.valid_to:
        raise CouponError(f"Coupon '{coupon.code}' has expired.")

    if subtotal < coupon.min_order_amount:
        raise CouponError(
            f"Coupon '{coupon.code}' needs a minimum order of Rs. {coupon.min_order_amount:.2f} "
            f"(this order is Rs. {subtotal:.2f})."
        )

    if coupon.usage_limit is not None and _usage_count(db, coupon.id) >= coupon.usage_limit:
        raise CouponError(f"Coupon '{coupon.code}' has reached its usage limit.")

    if _already_redeemed(db, coupon.id, customer_id):
        raise CouponError(f"Coupon '{coupon.code}' has already been used on this account.")

    discount = _compute_discount(coupon, subtotal)
    if discount <= 0:
        raise CouponError(f"Coupon '{coupon.code}' does not apply to this order.")

    return CouponApplication(coupon=coupon, discount_amount=discount)


def record_redemption(
    db: Session,
    *,
    coupon: Coupon,
    order: Order,
    customer_id: int,
    amount_discounted: Decimal,
) -> CouponRedemption:
    """Record that a coupon was spent on an order. Caller is responsible for having
    just validated it with `validate_coupon` inside the same transaction.

    Appended to BOTH parent relationships rather than setting `coupon_id`/`order_id`
    directly — same reasoning as `order.payments.append(p)` elsewhere: setting a FK
    by hand leaves an already-loaded collection on either parent stale in this
    session.
    """
    redemption = CouponRedemption(customer_id=customer_id, amount_discounted=amount_discounted)
    coupon.redemptions.append(redemption)
    order.coupon_redemptions.append(redemption)
    db.flush()
    return redemption
