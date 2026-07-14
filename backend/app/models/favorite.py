from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CustomerFavorite(Base, TimestampMixin):
    """A customer's bookmarked restaurant. Purely a convenience list — favoriting a
    restaurant implies nothing about whether it is currently open or still active."""

    __tablename__ = "customer_favorites"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "restaurant_id", name="uq_customer_favorites_customer_restaurant"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    customer: Mapped["Customer"] = relationship()  # noqa: F821
    restaurant: Mapped["Restaurant"] = relationship()  # noqa: F821
