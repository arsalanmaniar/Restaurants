from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import CouponDiscountType, OrderStatus, RefundStatus, RestaurantStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str
    restaurant_id: int | None = None
    restaurant_name: str | None = None


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #


class CategoryOut(ORMModel):
    id: int
    name: str
    sort_order: int
    is_active: bool


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class MenuItemOut(ORMModel):
    id: int
    category_id: int | None
    name: str
    description: str | None
    price: Decimal
    image_url: str | None
    is_available: bool
    sort_order: int


class MenuItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    price: Decimal = Field(gt=0, le=Decimal("999999.99"))
    category_id: int | None = None
    image_url: str | None = None
    is_available: bool = True
    sort_order: int = 0


class MenuItemPatch(BaseModel):
    """All-optional: the dashboard's availability toggle patches one field."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    price: Decimal | None = Field(default=None, gt=0, le=Decimal("999999.99"))
    category_id: int | None = None
    image_url: str | None = None
    is_available: bool | None = None
    sort_order: int | None = None


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #


class OrderItemOut(ORMModel):
    id: int
    item_name: str
    quantity: int
    price_at_order: Decimal
    line_total: Decimal
    notes: str | None


class OrderOut(ORMModel):
    id: int
    order_number: str
    status: OrderStatus
    subtotal: Decimal
    delivery_fee: Decimal
    total_amount: Decimal
    commission_amount: Decimal
    delivery_address_text: str | None
    notes: str | None
    placed_at: datetime
    items: list[OrderItemOut]


class OrderWithRestaurantOut(OrderOut):
    restaurant_id: int
    restaurant_name: str
    customer_number: str


class OrderStatusUpdate(BaseModel):
    status: OrderStatus
    note: str | None = None


# --------------------------------------------------------------------------- #
# Restaurants
# --------------------------------------------------------------------------- #


class RestaurantOut(ORMModel):
    id: int
    name: str
    description: str | None
    phone: str
    address: str | None
    cuisine_type: str | None
    status: RestaurantStatus
    commission_rate: Decimal
    delivery_fee: Decimal
    min_order_amount: Decimal
    is_accepting_orders: bool
    subscription_plan_id: int | None
    created_at: datetime


class RestaurantSummaryOut(RestaurantOut):
    order_count: int
    total_revenue: Decimal
    total_commission: Decimal


# --------------------------------------------------------------------------- #
# Working hours
# --------------------------------------------------------------------------- #


class WorkingHoursPeriod(BaseModel):
    day_of_week: int = Field(ge=0, le=6, description="0 = Monday … 6 = Sunday")
    opens_at: time
    closes_at: time
    crosses_midnight: bool = False

    @model_validator(mode="after")
    def _check_range(self) -> "WorkingHoursPeriod":
        # Equal times would mean a zero-length (or accidentally 24h) window. Force the
        # restaurant to say which they meant.
        if self.opens_at == self.closes_at:
            raise ValueError("Opening and closing time cannot be the same")
        if self.closes_at < self.opens_at and not self.crosses_midnight:
            raise ValueError(
                "Closing time is before opening time — set crosses_midnight if the "
                "kitchen runs past midnight"
            )
        return self


class WorkingHoursOut(WorkingHoursPeriod):
    model_config = ConfigDict(from_attributes=True)
    id: int


class WorkingHoursReplace(BaseModel):
    """Full replace, not a patch — the UI edits the whole week at once, and diffing
    individual periods would be needless complexity."""

    periods: list[WorkingHoursPeriod] = Field(default_factory=list, max_length=42)


# --------------------------------------------------------------------------- #
# Restaurant self-service settings
# --------------------------------------------------------------------------- #


class RestaurantSettingsPatch(BaseModel):
    """What a restaurant may change about ITSELF.

    Deliberately excludes commission_rate and status — those are the platform's to
    set, not the restaurant's. A restaurant editing its own commission would be a
    direct revenue leak.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    address: str | None = None
    cuisine_type: str | None = Field(default=None, max_length=64)
    logo_url: str | None = Field(default=None, max_length=512)
    delivery_fee: Decimal | None = Field(default=None, ge=0, le=Decimal("99999.99"))
    min_order_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("99999.99"))
    is_accepting_orders: bool | None = None


# --------------------------------------------------------------------------- #
# Subscription plans
# --------------------------------------------------------------------------- #


class SubscriptionPlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    monthly_fee: Decimal = Field(ge=0, le=Decimal("9999999.99"))
    commission_rate: Decimal | None = Field(default=None, ge=0, le=100)
    features: list[str] = Field(default_factory=list, max_length=20)
    is_active: bool = True
    sort_order: int = 0


class SubscriptionPlanPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    monthly_fee: Decimal | None = Field(default=None, ge=0, le=Decimal("9999999.99"))
    commission_rate: Decimal | None = Field(default=None, ge=0, le=100)
    features: list[str] | None = Field(default=None, max_length=20)
    is_active: bool | None = None
    sort_order: int | None = None


class SubscriptionPlanOut(ORMModel):
    id: int
    name: str
    description: str | None
    monthly_fee: Decimal
    commission_rate: Decimal | None
    features: list[str]
    is_active: bool
    sort_order: int
    restaurant_count: int = 0


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #


class RatingIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="whatsapp", max_length=32)


class RatingOut(ORMModel):
    id: int
    order_id: int
    rating: int
    comment: str | None
    source: str
    created_at: datetime
    order_number: str
    customer_number: str


class RatingSummary(BaseModel):
    average: float | None
    count: int
    # How many 1s, 2s … 5s — the shape matters more than the mean. Four 5s and two 1s
    # averages 3.7 and hides two furious customers.
    breakdown: dict[int, int]


# --------------------------------------------------------------------------- #
# Refunds (admin only)
# --------------------------------------------------------------------------- #


class RefundIn(BaseModel):
    # None = refund everything still refundable.
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("999999.99"))
    # Required, and not blank. An unexplained refund is unauditable.
    reason: str = Field(min_length=3, max_length=500)


class RefundOut(ORMModel):
    id: int
    order_id: int
    payment_id: int | None
    amount: Decimal
    reason: str
    status: RefundStatus
    issued_by: int
    provider_ref: str | None
    failure_reason: str | None
    created_at: datetime
    completed_at: datetime | None


class OrderRefundState(BaseModel):
    order_number: str
    total_amount: Decimal
    amount_paid: Decimal
    amount_refunded: Decimal
    refundable: Decimal
    refunds: list[RefundOut]


# --------------------------------------------------------------------------- #
# Coupons (admin only)
# --------------------------------------------------------------------------- #


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    discount_type: CouponDiscountType
    value: Decimal = Field(gt=0)
    # None = platform-wide.
    restaurant_id: int | None = None
    min_order_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    # None = unlimited.
    usage_limit: int | None = Field(default=None, gt=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check(self) -> "CouponIn":
        self.code = self.code.strip().upper()
        if len(self.code) < 3:
            raise ValueError("Coupon code must be at least 3 characters")
        # A percentage over 100 is never intentional, and would hand back more than
        # the order is worth before the subtotal clamp even runs.
        if self.discount_type == CouponDiscountType.PERCENTAGE and self.value > 100:
            raise ValueError("A percentage discount cannot exceed 100")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to cannot be before valid_from")
        return self


class CouponPatch(BaseModel):
    code: str | None = Field(default=None, min_length=3, max_length=40)
    discount_type: CouponDiscountType | None = None
    value: Decimal | None = Field(default=None, gt=0)
    restaurant_id: int | None = None
    min_order_amount: Decimal | None = Field(default=None, ge=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    usage_limit: int | None = Field(default=None, gt=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "CouponPatch":
        if self.code is not None:
            self.code = self.code.strip().upper()
            if len(self.code) < 3:
                raise ValueError("Coupon code must be at least 3 characters")
        if (
            self.discount_type == CouponDiscountType.PERCENTAGE
            and self.value is not None
            and self.value > 100
        ):
            raise ValueError("A percentage discount cannot exceed 100")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to cannot be before valid_from")
        return self


class CouponOut(ORMModel):
    id: int
    code: str
    discount_type: CouponDiscountType
    value: Decimal
    restaurant_id: int | None
    min_order_amount: Decimal
    max_discount_amount: Decimal | None
    usage_limit: int | None
    valid_from: date | None
    valid_to: date | None
    is_active: bool
    created_at: datetime


class CouponSummaryOut(CouponOut):
    restaurant_name: str | None
    times_redeemed: int


class RestaurantPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    phone: str | None = None
    address: str | None = None
    cuisine_type: str | None = None
    status: RestaurantStatus | None = None
    commission_rate: Decimal | None = Field(default=None, ge=0, le=100)
    delivery_fee: Decimal | None = Field(default=None, ge=0)
    min_order_amount: Decimal | None = Field(default=None, ge=0)
    is_accepting_orders: bool | None = None
    subscription_plan_id: int | None = None


# --------------------------------------------------------------------------- #
# Restaurant onboarding (admin creates restaurant + owner account in one go)
# --------------------------------------------------------------------------- #


class RestaurantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=8)


class OwnerCreated(BaseModel):
    """The admin supplied and already knows the password — it is deliberately not
    echoed back here, so it never has a second place to leak from (e.g. logs)."""

    email: str


class RestaurantCreateOut(BaseModel):
    restaurant: RestaurantOut
    owner: OwnerCreated


class RestaurantUpdate(BaseModel):
    """Deliberately narrow: only the fields the admin's edit form asks for. Anything
    else (status, commission, email/password, ...) belongs to a different flow and is
    stripped by simply not being a field here — extra keys in the request body are
    ignored by pydantic, not silently applied."""

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Financial reports (admin platform-wide, admin per-restaurant, restaurant self)
# --------------------------------------------------------------------------- #
#
# One shared shape for all three endpoints — a restaurant's own report and the
# admin's view of that same restaurant must never drift apart into two different
# definitions of "revenue".


class ReportCategoryOut(BaseModel):
    name: str
    revenue: Decimal
    order_count: int


class ReportItemOut(BaseModel):
    name: str
    revenue: Decimal
    quantity_sold: int


class ReportOut(BaseModel):
    gross_sales: Decimal
    cancelled_amount: Decimal
    net_sales: Decimal
    cash_amount: Decimal
    online_amount: Decimal
    order_count: int
    customer_count: int
    avg_order_amount: Decimal
    # No delivery/pickup distinction exists anywhere in the schema today — every order
    # is a delivery (see Order.delivery_address_text). Both fields are always 0 rather
    # than guessing; see the report handoff notes for the flag on this.
    delivery_count: int
    pickup_count: int
    top_categories: list[ReportCategoryOut]
    top_items: list[ReportItemOut]


# --------------------------------------------------------------------------- #
# Promotions (restaurant-run time-bound deals — distinct from Coupon)
# --------------------------------------------------------------------------- #


class PromotionIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    discount_type: CouponDiscountType
    discount_value: Decimal = Field(gt=0)
    # Empty list ⇒ promotion applies to the whole menu.
    applicable_menu_item_ids: list[int] = Field(default_factory=list)
    min_order_amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    valid_from: date
    valid_to: date
    is_active: bool = True

    @model_validator(mode="after")
    def _check(self) -> "PromotionIn":
        if (
            self.discount_type == CouponDiscountType.PERCENTAGE
            and self.discount_value > 100
        ):
            raise ValueError("A percentage discount cannot exceed 100")
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot be before valid_from")
        return self


class PromotionPatch(BaseModel):
    """All-optional. The restaurant's dashboard typically flips is_active or
    edits the date window; other fields are here for completeness."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    discount_type: CouponDiscountType | None = None
    discount_value: Decimal | None = Field(default=None, gt=0)
    applicable_menu_item_ids: list[int] | None = None
    min_order_amount: Decimal | None = Field(default=None, ge=0)
    max_discount_amount: Decimal | None = Field(default=None, gt=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "PromotionPatch":
        if (
            self.discount_type == CouponDiscountType.PERCENTAGE
            and self.discount_value is not None
            and self.discount_value > 100
        ):
            raise ValueError("A percentage discount cannot exceed 100")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to cannot be before valid_from")
        return self


class PromotionOut(ORMModel):
    id: int
    restaurant_id: int
    title: str
    description: str | None
    discount_type: CouponDiscountType
    discount_value: Decimal
    applicable_menu_item_ids: list[int]
    min_order_amount: Decimal
    max_discount_amount: Decimal | None
    valid_from: date
    valid_to: date
    is_active: bool
    created_at: datetime
