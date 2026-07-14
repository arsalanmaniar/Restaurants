"""Working hours, subscription plans, and order ratings."""

from datetime import time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RestaurantWorkingHours(Base, TimestampMixin):
    """One row per open period per weekday.

    Modelled as multiple rows rather than one open/close pair because split shifts
    are normal here — a place open 12:00-15:00 then 19:00-23:30 cannot be expressed
    as a single range.
    """

    __tablename__ = "restaurant_working_hours"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_working_hours_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 0 = Monday .. 6 = Sunday (matches Python's date.weekday()).
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    opens_at: Mapped[time] = mapped_column(Time, nullable=False)
    closes_at: Mapped[time] = mapped_column(Time, nullable=False)
    # True when the period runs past midnight (e.g. 19:00 -> 02:00). Stored rather
    # than inferred so the intent is explicit in the data.
    crosses_midnight: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    restaurant: Mapped["Restaurant"] = relationship(  # noqa: F821
        back_populates="working_hours"
    )


class SubscriptionPlan(Base, TimestampMixin):
    __tablename__ = "subscription_plans"
    __table_args__ = (UniqueConstraint("name", name="uq_subscription_plans_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    monthly_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # The commission rate this plan implies. Nullable = plan does not override the
    # restaurant's own rate.
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    features: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    restaurants: Mapped[list["Restaurant"]] = relationship(  # noqa: F821
        back_populates="subscription_plan"
    )


class OrderRating(Base, TimestampMixin):
    __tablename__ = "order_ratings"
    __table_args__ = (
        # One rating per order — a customer cannot stuff the ballot by replying twice.
        UniqueConstraint("order_id", name="uq_order_ratings_order"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_order_ratings_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised so the restaurant's ratings list doesn't need a join through orders
    # on every read, and so ratings survive if an order is ever archived.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    # Where it came from — "whatsapp" once the AI collects it, "admin" if entered by hand.
    source: Mapped[str] = mapped_column(String(32), default="whatsapp", nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    order: Mapped["Order"] = relationship(back_populates="rating")  # noqa: F821
