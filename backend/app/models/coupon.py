"""Coupons. The PLATFORM funds every discount, never the restaurant — see
`app/services/coupons.py` for the commission math this implies.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import CouponDiscountType


class Coupon(Base, TimestampMixin):
    __tablename__ = "coupons"
    __table_args__ = (UniqueConstraint("code", name="uq_coupons_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Always stored upper-cased (see app/services/coupons.py) so "save20" and "SAVE20"
    # hit the same row without a case-insensitive index.
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    discount_type: Mapped[CouponDiscountType] = mapped_column(
        Enum(CouponDiscountType, name="coupon_discount_type"), nullable=False
    )
    # Percentage points (10.00 = 10%) for PERCENTAGE, a Rupee amount for FIXED.
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Null = platform-wide; set = redeemable only at this one restaurant. Restaurants
    # are never hard-deleted from live data, but if one ever were, its own coupon
    # should go with it rather than silently becoming platform-wide.
    restaurant_id: Mapped[int | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )

    min_order_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    # Cap on the discount a PERCENTAGE coupon can give. Ignored for FIXED coupons.
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Null = unlimited redemptions across all customers.
    usage_limit: Mapped[int | None] = mapped_column(Integer)

    # Plain dates, not timestamps: "valid until 14 July" means valid through the END
    # of 14 July in Asia/Karachi, not UTC midnight — see services/coupons.py, which
    # is the only place these are ever compared against "today".
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    restaurant: Mapped["Restaurant | None"] = relationship()  # noqa: F821
    redemptions: Mapped[list["CouponRedemption"]] = relationship(
        back_populates="coupon", cascade="all, delete-orphan"
    )


class CouponRedemption(Base, TimestampMixin):
    """One customer's use of one coupon on one order.

    This is what enforces "once per customer" (the unique constraint) and the total
    usage cap (count of rows for the coupon). `order_id` is RESTRICT, not CASCADE or
    SET NULL: an order is never deleted in this system, and the redemption row must
    outlive anything that might try to delete the coupon out from under it — see the
    coupon delete guard in app/api/admin.py.
    """

    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        UniqueConstraint(
            "coupon_id", "customer_id", name="uq_coupon_redemptions_coupon_customer"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    coupon_id: Mapped[int] = mapped_column(
        ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # What this specific order's discount actually came to — snapshot, same reasoning
    # as order_items.price_at_order: a later change to the coupon must not rewrite
    # what a past redemption actually gave away.
    amount_discounted: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    coupon: Mapped["Coupon"] = relationship(back_populates="redemptions")
    order: Mapped["Order"] = relationship(back_populates="coupon_redemptions")  # noqa: F821
