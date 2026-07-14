from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import PaymentAttemptStatus, PaymentProviderName


class Payment(Base, TimestampMixin):
    """One ATTEMPT at paying for an order.

    An order has many payments: the customer's card is declined, they try again with a
    wallet, that times out, they retry. Modelling this as one row per order would lose
    that history — and that history is the only thing that can settle a "but I paid!"
    dispute, which is why `raw_request` / `raw_response` are kept verbatim.
    """

    __tablename__ = "payments"
    __table_args__ = (
        # Our own reference, sent to the gateway as its transaction id. Must be unique:
        # it is the idempotency key we de-dupe replayed callbacks on.
        UniqueConstraint("txn_ref", name="uq_payments_txn_ref"),
        Index("ix_payments_status_expires", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )

    provider: Mapped[PaymentProviderName] = mapped_column(
        Enum(PaymentProviderName, name="payment_provider"), nullable=False
    )
    # Ours, e.g. "AB-4F2K9C-1". Sent as the gateway's transaction reference.
    txn_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Theirs, returned in the callback (JazzCash's pp_RetreivalReferenceNo, etc).
    provider_ref: Mapped[str | None] = mapped_column(String(128), index=True)

    # The amount we EXPECT. The callback's amount is compared against this and rejected
    # if it differs — never trust the number the gateway hands back.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    status: Mapped[PaymentAttemptStatus] = mapped_column(
        Enum(PaymentAttemptStatus, name="payment_attempt_status"),
        default=PaymentAttemptStatus.INITIATED,
        nullable=False,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)

    raw_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # After this, the attempt is dead and the reconciliation job cancels the order.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="payments")  # noqa: F821
