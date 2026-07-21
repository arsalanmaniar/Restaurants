"""Restaurant-run promotions with a fixed date window.

Distinct from Coupon deliberately:
  - Coupon is a *code the customer types* ("SAVE20"). Its lifecycle is claim
    driven, its scope can be platform-wide, and it can enforce once-per-customer.
  - Promotion is a *browsable deal a restaurant sets on their own menu* ("50%
    off biryani Fri-Sun"). It's always restaurant-scoped, always browsable,
    always date-bounded, and never gates on a code the customer has to know.

The same table would have conflated two very different customer experiences —
so this is its own model. For MVP the promotion is INFORMATIONAL: the AI
mentions it in conversation when relevant. Auto-applying at place_order (the
"real" discount pathway) is a follow-up so this phase doesn't touch the
commission / coupon math that already works.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import CouponDiscountType


class Promotion(Base, TimestampMixin):
    __tablename__ = "promotions"
    __table_args__ = (
        # A promotion whose start is after its end is a data bug; catch it in
        # the DB rather than trusting every writer to remember.
        CheckConstraint("valid_from <= valid_to", name="ck_promotions_valid_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Always restaurant-scoped — no platform-wide promotions. CASCADE because
    # a deleted restaurant's promotions have no meaning.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Short customer-facing headline the AI will quote verbatim: "50% off Biryani
    # this weekend". Kept small on purpose — long marketing copy belongs in
    # `description`.
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Reuses CouponDiscountType — same PERCENTAGE/FIXED semantics. New enum
    # would have duplicated the schema for no gain.
    discount_type: Mapped[CouponDiscountType] = mapped_column(
        Enum(CouponDiscountType, name="coupon_discount_type", create_type=False),
        nullable=False,
    )
    # Percentage points (10.00 = 10%) for PERCENTAGE, a Rupee amount for FIXED.
    discount_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Empty list = the promo covers the whole menu ("15% off everything").
    # A populated list restricts it ("50% off item ids [42, 43]"). Stored as
    # JSONB so we can index-search "which promos apply to menu_item 42?" later
    # without a join table.
    applicable_menu_item_ids: Mapped[list[int]] = mapped_column(
        JSONB, default=list, nullable=False
    )

    min_order_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    # Cap on a PERCENTAGE promo's discount ("50% off, up to Rs. 500"). Ignored
    # for FIXED promos — the fixed amount is already the cap.
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # Plain dates, not timestamps: interpreted in Asia/Karachi, same as
    # Coupon.valid_from/valid_to. "Valid through 20 July" means valid through
    # the END of 20 July local time.
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)

    # Manual off-switch for the restaurant staff — beats the date window in
    # both directions (they can retire a promo early, or a future one stays
    # dormant until they set is_active=True on go-live day).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    restaurant: Mapped["Restaurant"] = relationship()  # noqa: F821
