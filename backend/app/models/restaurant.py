from decimal import Decimal

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import RestaurantStatus, StaffRole


class Restaurant(Base, TimestampMixin):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    cuisine_type: Mapped[str | None] = mapped_column(String(64), index=True)
    logo_url: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[RestaurantStatus] = mapped_column(
        Enum(RestaurantStatus, name="restaurant_status"),
        default=RestaurantStatus.PENDING,
        nullable=False,
        index=True,
    )
    # Percentage of order subtotal taken by the platform, e.g. 15.00 = 15%.
    commission_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("15.00"), nullable=False
    )
    delivery_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    min_order_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    # Master switch the restaurant flips when it stops taking orders right now.
    # This is a manual override and always wins over the working-hours schedule.
    is_accepting_orders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    subscription_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="SET NULL"), index=True
    )

    staff: Mapped[list["RestaurantStaff"]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    categories: Mapped[list["MenuCategory"]] = relationship(  # noqa: F821
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    menu_items: Mapped[list["MenuItem"]] = relationship(  # noqa: F821
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(back_populates="restaurant")  # noqa: F821
    working_hours: Mapped[list["RestaurantWorkingHours"]] = relationship(  # noqa: F821
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    subscription_plan: Mapped["SubscriptionPlan | None"] = relationship(  # noqa: F821
        back_populates="restaurants"
    )


class RestaurantStaff(Base, TimestampMixin):
    __tablename__ = "restaurant_staff"
    __table_args__ = (UniqueConstraint("email", name="uq_restaurant_staff_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        Enum(StaffRole, name="staff_role"), default=StaffRole.OWNER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    restaurant: Mapped["Restaurant"] = relationship(back_populates="staff")


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"
    __table_args__ = (UniqueConstraint("email", name="uq_admin_users_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
