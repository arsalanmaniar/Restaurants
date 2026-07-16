"""Admin dashboard API — cross-restaurant, no tenant scoping."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentAdmin, DbSession
from app.core.security import hash_password
from app.models import (
    Coupon,
    CouponRedemption,
    Customer,
    Order,
    OrderStatus,
    Refund,
    RefundStatus,
    Restaurant,
    RestaurantStaff,
    RestaurantStatus,
    StaffRole,
    SubscriptionPlan,
)
from app.schemas import (
    CouponIn,
    CouponOut,
    CouponPatch,
    CouponSummaryOut,
    OrderRefundState,
    OrderWithRestaurantOut,
    OwnerCreated,
    RefundIn,
    RefundOut,
    RestaurantCreate,
    RestaurantCreateOut,
    RestaurantOut,
    RestaurantPatch,
    RestaurantSummaryOut,
    SubscriptionPlanIn,
    SubscriptionPlanOut,
    SubscriptionPlanPatch,
)
from app.services import refunds as refunds_service

# The business runs in Pakistan; "today" must mean today in Karachi, not in UTC.
PAKISTAN_TZ = ZoneInfo("Asia/Karachi")

# Neither a cancelled order nor an unpaid one is revenue. Counting AWAITING_PAYMENT
# would inflate platform earnings with money nobody has handed over.
NON_REVENUE_STATUSES = [OrderStatus.CANCELLED, OrderStatus.AWAITING_PAYMENT]

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/restaurants", response_model=list[RestaurantSummaryOut])
def list_restaurants(admin: CurrentAdmin, db: DbSession) -> list[RestaurantSummaryOut]:
    # Aggregate in one query rather than N+1'ing per restaurant.
    totals = (
        select(
            Order.restaurant_id.label("rid"),
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total_amount), 0).label("total_revenue"),
            func.coalesce(func.sum(Order.commission_amount), 0).label("total_commission"),
        )
        .where(Order.status.notin_(NON_REVENUE_STATUSES))
        .group_by(Order.restaurant_id)
        .subquery()
    )

    rows = db.execute(
        select(
            Restaurant,
            func.coalesce(totals.c.order_count, 0),
            func.coalesce(totals.c.total_revenue, 0),
            func.coalesce(totals.c.total_commission, 0),
        )
        .outerjoin(totals, totals.c.rid == Restaurant.id)
        .order_by(Restaurant.name)
    ).all()

    return [
        RestaurantSummaryOut(
            **RestaurantOut.model_validate(restaurant).model_dump(),
            order_count=count,
            total_revenue=revenue,
            total_commission=commission,
        )
        for restaurant, count, revenue, commission in rows
    ]


@router.post(
    "/restaurants", response_model=RestaurantCreateOut, status_code=status.HTTP_201_CREATED
)
def create_restaurant(
    payload: RestaurantCreate, admin: CurrentAdmin, db: DbSession
) -> RestaurantCreateOut:
    """Onboard a restaurant: create the restaurant row plus its first staff account
    (the owner), so the admin can hand over a working login in one step instead of the
    restaurant waiting on a separate signup flow.

    The admin supplies the owner's email + password directly — nothing is generated,
    and the password is never echoed back or logged; only its bcrypt hash lives in
    the database.
    """
    if db.scalar(select(Restaurant).where(Restaurant.name == payload.name)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A restaurant with that name already exists"
        )

    if db.scalar(select(RestaurantStaff).where(RestaurantStaff.email == payload.email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with that email already exists"
        )

    restaurant = Restaurant(
        name=payload.name,
        phone=payload.phone,
        address=payload.address,
        cuisine_type="Other",
        status=RestaurantStatus.ACTIVE,
        commission_rate=Decimal("15.00"),
        delivery_fee=Decimal("100.00"),
        min_order_amount=Decimal("500.00"),
        is_accepting_orders=True,
    )
    db.add(restaurant)
    db.flush()  # populate restaurant.id for the owner account below

    owner = RestaurantStaff(
        restaurant_id=restaurant.id,
        name=f"{restaurant.name} Owner",
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=StaffRole.OWNER,
        is_active=True,
    )
    db.add(owner)
    db.commit()
    db.refresh(restaurant)

    return RestaurantCreateOut(
        restaurant=RestaurantOut.model_validate(restaurant),
        owner=OwnerCreated(email=owner.email),
    )


@router.delete("/restaurants/{restaurant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_restaurant(restaurant_id: int, admin: CurrentAdmin, db: DbSession) -> None:
    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restaurant not found")

    order_count = db.scalar(
        select(func.count(Order.id)).where(Order.restaurant_id == restaurant_id)
    )
    if order_count:
        # Orders.restaurant_id is ON DELETE RESTRICT for exactly this reason — real
        # customer order history must never be silently orphaned by a delete here.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: {order_count} order(s) reference this restaurant. "
            "Deactivate instead.",
        )

    db.delete(restaurant)  # cascades to staff/categories/menu_items/working_hours
    db.commit()


@router.patch("/restaurants/{restaurant_id}", response_model=RestaurantOut)
def update_restaurant(
    restaurant_id: int, payload: RestaurantPatch, admin: CurrentAdmin, db: DbSession
) -> Restaurant:
    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restaurant not found")

    updates = payload.model_dump(exclude_unset=True)

    # Validate the FK ourselves — otherwise a bad id surfaces as a 500 from Postgres
    # rather than a 400 the dashboard can show.
    plan_id = updates.get("subscription_plan_id")
    if plan_id is not None and db.get(SubscriptionPlan, plan_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown subscription plan")

    for field, value in updates.items():
        setattr(restaurant, field, value)

    db.commit()
    db.refresh(restaurant)
    return restaurant


@router.get("/subscription-plans", response_model=list[SubscriptionPlanOut])
def list_plans(admin: CurrentAdmin, db: DbSession) -> list[SubscriptionPlanOut]:
    counts = dict(
        db.execute(
            select(Restaurant.subscription_plan_id, func.count(Restaurant.id))
            .where(Restaurant.subscription_plan_id.isnot(None))
            .group_by(Restaurant.subscription_plan_id)
        ).all()
    )

    plans = db.scalars(
        select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order, SubscriptionPlan.id)
    ).all()

    return [
        SubscriptionPlanOut(
            **SubscriptionPlanOut.model_validate(plan).model_dump(
                exclude={"restaurant_count"}
            ),
            restaurant_count=counts.get(plan.id, 0),
        )
        for plan in plans
    ]


@router.post(
    "/subscription-plans", response_model=SubscriptionPlanOut, status_code=status.HTTP_201_CREATED
)
def create_plan(
    payload: SubscriptionPlanIn, admin: CurrentAdmin, db: DbSession
) -> SubscriptionPlan:
    if db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A plan with that name already exists")

    plan = SubscriptionPlan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.patch("/subscription-plans/{plan_id}", response_model=SubscriptionPlanOut)
def update_plan(
    plan_id: int, payload: SubscriptionPlanPatch, admin: CurrentAdmin, db: DbSession
) -> SubscriptionPlan:
    plan = db.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates:
        clash = db.scalar(
            select(SubscriptionPlan).where(
                SubscriptionPlan.name == updates["name"], SubscriptionPlan.id != plan_id
            )
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "A plan with that name already exists")

    for field, value in updates.items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/subscription-plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: int, admin: CurrentAdmin, db: DbSession) -> None:
    plan = db.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")

    in_use = db.scalar(
        select(func.count(Restaurant.id)).where(Restaurant.subscription_plan_id == plan_id)
    )
    if in_use:
        # Deleting would silently unassign live restaurants. Make the admin move them
        # or deactivate the plan instead.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{in_use} restaurant(s) are on this plan. Move them off it first, "
            "or deactivate the plan instead of deleting it.",
        )

    db.delete(plan)
    db.commit()


# --------------------------------------------------------------------------- #
# Coupons
# --------------------------------------------------------------------------- #
#
# Admin-only, on purpose: a coupon is a lever on platform revenue (the platform, not
# the restaurant, funds every discount — see app/services/coupons.py), so a
# restaurant must never be able to create or edit one. There is deliberately no
# equivalent route on the restaurant router.


def _coupon_summary(coupon: Coupon, redemption_counts: dict[int, int]) -> CouponSummaryOut:
    return CouponSummaryOut(
        **CouponOut.model_validate(coupon).model_dump(),
        restaurant_name=coupon.restaurant.name if coupon.restaurant else None,
        times_redeemed=redemption_counts.get(coupon.id, 0),
    )


@router.get("/coupons", response_model=list[CouponSummaryOut])
def list_coupons(
    admin: CurrentAdmin,
    db: DbSession,
    restaurant_id: int | None = Query(default=None),
) -> list[CouponSummaryOut]:
    stmt = select(Coupon).order_by(Coupon.id.desc())
    if restaurant_id is not None:
        stmt = stmt.where(Coupon.restaurant_id == restaurant_id)
    coupons = db.scalars(stmt).all()

    counts = dict(
        db.execute(
            select(CouponRedemption.coupon_id, func.count(CouponRedemption.id))
            .where(CouponRedemption.coupon_id.in_({c.id for c in coupons} or {0}))
            .group_by(CouponRedemption.coupon_id)
        ).all()
    )

    return [_coupon_summary(coupon, counts) for coupon in coupons]


@router.post("/coupons", response_model=CouponSummaryOut, status_code=status.HTTP_201_CREATED)
def create_coupon(payload: CouponIn, admin: CurrentAdmin, db: DbSession) -> CouponSummaryOut:
    if payload.restaurant_id is not None and db.get(Restaurant, payload.restaurant_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown restaurant")

    if db.scalar(select(Coupon).where(Coupon.code == payload.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A coupon with that code already exists")

    coupon = Coupon(**payload.model_dump())
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return _coupon_summary(coupon, {})


@router.patch("/coupons/{coupon_id}", response_model=CouponSummaryOut)
def update_coupon(
    coupon_id: int, payload: CouponPatch, admin: CurrentAdmin, db: DbSession
) -> CouponSummaryOut:
    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coupon not found")

    updates = payload.model_dump(exclude_unset=True)

    if "restaurant_id" in updates and updates["restaurant_id"] is not None:
        if db.get(Restaurant, updates["restaurant_id"]) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown restaurant")

    if "code" in updates:
        clash = db.scalar(
            select(Coupon).where(Coupon.code == updates["code"], Coupon.id != coupon_id)
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "A coupon with that code already exists")

    for field, value in updates.items():
        setattr(coupon, field, value)

    db.commit()
    db.refresh(coupon)

    count = db.scalar(
        select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon.id)
    )
    return _coupon_summary(coupon, {coupon.id: count})


@router.delete("/coupons/{coupon_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_coupon(coupon_id: int, admin: CurrentAdmin, db: DbSession) -> None:
    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coupon not found")

    in_use = db.scalar(
        select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon_id)
    )
    if in_use:
        # Deleting would destroy the record of who has already redeemed this coupon,
        # which is the only thing enforcing "once per customer". Deactivate instead.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{in_use} redemption(s) exist for this coupon. Deactivate it instead of deleting.",
        )

    db.delete(coupon)
    db.commit()


@router.get("/orders", response_model=list[OrderWithRestaurantOut])
def list_all_orders(
    admin: CurrentAdmin,
    db: DbSession,
    restaurant_id: int | None = Query(default=None),
    order_status: OrderStatus | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> list[OrderWithRestaurantOut]:
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.restaurant))
        .order_by(Order.placed_at.desc())
        .limit(limit)
    )
    if restaurant_id is not None:
        stmt = stmt.where(Order.restaurant_id == restaurant_id)
    if order_status is not None:
        stmt = stmt.where(Order.status == order_status)

    orders = db.scalars(stmt).all()

    # customer_number in one lookup rather than a join per order.
    numbers = dict(
        db.execute(
            select(Customer.id, Customer.whatsapp_number).where(
                Customer.id.in_({o.customer_id for o in orders} or {0})
            )
        ).all()
    )

    return [
        OrderWithRestaurantOut(
            **{
                field: getattr(order, field)
                for field in (
                    "id",
                    "order_number",
                    "status",
                    "subtotal",
                    "delivery_fee",
                    "total_amount",
                    "commission_amount",
                    "delivery_address_text",
                    "notes",
                    "placed_at",
                    "items",
                )
            },
            restaurant_id=order.restaurant_id,
            restaurant_name=order.restaurant.name,
            customer_number=numbers.get(order.customer_id, "unknown"),
        )
        for order in orders
    ]


def _totals_since(db: DbSession, since: datetime | None) -> tuple[int, Decimal, Decimal]:
    """(orders, gross revenue, platform commission) for a window. Cancelled orders
    never count — nobody paid for them."""
    stmt = select(
        func.count(Order.id),
        func.coalesce(func.sum(Order.total_amount), 0),
        func.coalesce(func.sum(Order.commission_amount), 0),
    ).where(Order.status.notin_(NON_REVENUE_STATUSES))

    if since is not None:
        stmt = stmt.where(Order.placed_at >= since)

    count, revenue, commission = db.execute(stmt).one()
    return count, revenue, commission


@router.get("/orders/{order_id}/refunds", response_model=OrderRefundState)
def order_refund_state(order_id: int, admin: CurrentAdmin, db: DbSession) -> OrderRefundState:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    return OrderRefundState(
        order_number=order.order_number,
        total_amount=order.total_amount,
        amount_paid=refunds_service.amount_paid(order),
        amount_refunded=refunds_service.amount_refunded(db, order),
        refundable=refunds_service.refundable(db, order),
        refunds=[RefundOut.model_validate(r) for r in order.refunds],
    )


@router.post(
    "/orders/{order_id}/refunds", response_model=RefundOut, status_code=status.HTTP_201_CREATED
)
def create_refund(
    order_id: int, payload: RefundIn, admin: CurrentAdmin, db: DbSession
) -> Refund:
    """Issue a refund. ADMIN ONLY — there is deliberately no equivalent route on the
    restaurant router, so restaurant staff cannot reach this at all."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    try:
        refund = refunds_service.issue_refund(
            db, order=order, admin=admin, amount=payload.amount, reason=payload.reason
        )
    except refunds_service.RefundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # TODO: once merchant credentials exist, push this to the provider's refund API here
    # and mark it completed on success. Until then it stays PENDING and a human moves the
    # money (which is also the correct flow for COD, where there is no gateway at all).
    db.commit()
    db.refresh(refund)
    return refund


@router.post("/refunds/{refund_id}/complete", response_model=RefundOut)
def complete_refund(refund_id: int, admin: CurrentAdmin, db: DbSession) -> Refund:
    """Confirm the money is actually back with the customer.

    Separate from issuing, on purpose: recording the DECISION to refund and confirming
    the money MOVED are different facts, and conflating them means a failed gateway call
    silently looks like a completed refund.
    """
    refund = db.get(Refund, refund_id)
    if refund is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Refund not found")

    if refund.status == RefundStatus.COMPLETED:
        return refund
    if refund.status == RefundStatus.FAILED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This refund failed — issue a new one instead."
        )

    refunds_service.mark_completed(db, refund, provider_ref=f"manual:admin:{admin.id}")
    db.commit()
    db.refresh(refund)
    return refund


@router.get("/stats")
def platform_stats(admin: CurrentAdmin, db: DbSession) -> dict:
    now = datetime.now(timezone.utc)
    # "Today" = since local midnight in Pakistan, which is what an admin in Karachi
    # means by the word — not midnight UTC (that's 5am for them).
    today_start = (
        now.astimezone(PAKISTAN_TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    week_start = now - timedelta(days=7)

    all_orders, all_revenue, all_commission = _totals_since(db, None)
    today_orders, today_revenue, today_commission = _totals_since(db, today_start)
    week_orders, week_revenue, week_commission = _totals_since(db, week_start)

    active_restaurants = db.scalar(
        select(func.count(Restaurant.id)).where(Restaurant.status == RestaurantStatus.ACTIVE)
    )
    pending_approval = db.scalar(
        select(func.count(Restaurant.id)).where(Restaurant.status == RestaurantStatus.PENDING)
    )
    customers = db.scalar(select(func.count(Customer.id)))

    return {
        "total_orders": all_orders,
        "gross_revenue": str(all_revenue),
        "platform_commission": str(all_commission),
        "orders_today": today_orders,
        "revenue_today": str(today_revenue),
        "commission_today": str(today_commission),
        "orders_7d": week_orders,
        "revenue_7d": str(week_revenue),
        "commission_7d": str(week_commission),
        "active_restaurants": active_restaurants,
        "pending_approval": pending_approval,
        "total_customers": customers,
    }
