from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import RefundStatus


class Refund(Base, TimestampMixin):
    """Money given back.

    Admin-only by design (approved decision): a refund button in the restaurant
    dashboard is a way to lose money to a mistake or a disgruntled employee.

    Every refund names the admin who issued it and the reason. Refunds are the most
    abusable operation in the system, so the audit trail is not optional — `issued_by`
    is NOT NULL and there is no code path that creates a Refund without it.
    """

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # The attempt being reversed. Null for a COD order — there was no gateway payment,
    # so the money goes back in cash and this row is just the record of that decision.
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="refund_status"),
        default=RefundStatus.PENDING,
        nullable=False,
        index=True,
    )
    # RESTRICT, not SET NULL: deleting an admin must never orphan the record of who
    # authorised a refund.
    issued_by: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    provider_ref: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="refunds")  # noqa: F821
    admin: Mapped["AdminUser"] = relationship()  # noqa: F821
