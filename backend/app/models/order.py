from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import OrderStatus, PaymentMethod, PaymentStatus


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_orders_order_number"),
        # Restaurant dashboard's main query: this restaurant's orders, newest first.
        Index("ix_orders_restaurant_created", "restaurant_id", "created_at"),
        Index("ix_orders_restaurant_status", "restaurant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Short human-readable reference the customer sees on WhatsApp, e.g. "AB-4F2K9C".
    order_number: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    address_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_addresses.id", ondelete="SET NULL")
    )
    # Snapshot: the address as it was at order time, even if the customer edits it later.
    delivery_address_text: Mapped[str | None] = mapped_column(Text)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        default=OrderStatus.PENDING,
        nullable=False,
        index=True,
    )

    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Frozen at order time — the restaurant's rate may change later, but this order's
    # commission must not.
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method"), default=PaymentMethod.COD, nullable=False
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.UNPAID, nullable=False
    )

    notes: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # NULL for single-restaurant orders (the common case). Populated when
    # the customer explicitly linked this order to a previous one via
    # place_order's `link_to_order_number` param — see services/tools.py.
    # All orders in one conversation that share this id are "linked" for
    # dashboard grouping; they remain fully independent otherwise (own
    # totals, own payments, own status transitions).
    order_group_id: Mapped[str | None] = mapped_column(String(32), index=True)

    customer: Mapped["Customer"] = relationship(back_populates="orders")  # noqa: F821
    restaurant: Mapped["Restaurant"] = relationship(back_populates="orders")  # noqa: F821
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderStatusHistory.id"
    )
    rating: Mapped["OrderRating | None"] = relationship(  # noqa: F821
        back_populates="order", cascade="all, delete-orphan", uselist=False
    )
    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        back_populates="order", cascade="all, delete-orphan", order_by="Payment.id"
    )
    refunds: Mapped[list["Refund"]] = relationship(  # noqa: F821
        back_populates="order", order_by="Refund.id"
    )
    coupon_redemptions: Mapped[list["CouponRedemption"]] = relationship(  # noqa: F821
        back_populates="order", order_by="CouponRedemption.id"
    )


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable + SET NULL: a deleted menu item must not delete order history.
    menu_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("menu_items.id", ondelete="SET NULL")
    )
    # Snapshots, so renaming or repricing the menu never rewrites a past order.
    item_name: Mapped[str] = mapped_column(String(160), nullable=False)
    price_at_order: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), nullable=False
    )
    # Free-form actor: "ai", "customer", "staff:12", "admin:3".
    changed_by: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped["Order"] = relationship(back_populates="status_history")
