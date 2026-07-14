from sqlalchemy import Boolean, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("whatsapp_number", name="uq_customers_whatsapp_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored E.164 without the '+', matching UltraMsg's format (e.g. 923001234567).
    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    addresses: Mapped[list["CustomerAddress"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")  # noqa: F821
    conversations: Mapped[list["Conversation"]] = relationship(  # noqa: F821
        back_populates="customer", cascade="all, delete-orphan"
    )


class CustomerAddress(Base, TimestampMixin):
    __tablename__ = "customer_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(64))  # "Home", "Office"
    address_text: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="addresses")
