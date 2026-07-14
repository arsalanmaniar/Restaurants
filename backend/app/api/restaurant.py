"""Restaurant-facing dashboard API.

Every handler scopes by `principal.restaurant_id`, which comes from the JWT. A
restaurant_id in a path or body is only ever used *together* with that scope, so
staff at restaurant A cannot read or mutate restaurant B's rows by guessing ids.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentStaff, DbSession
from app.models import (
    Customer,
    MenuCategory,
    MenuItem,
    Order,
    OrderRating,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
    PaymentStatus,
    Restaurant,
    RestaurantWorkingHours,
)
from app.schemas import (
    CategoryIn,
    CategoryOut,
    MenuItemIn,
    MenuItemOut,
    MenuItemPatch,
    OrderOut,
    OrderStatusUpdate,
    RatingOut,
    RatingSummary,
    RestaurantOut,
    RestaurantSettingsPatch,
    WorkingHoursOut,
    WorkingHoursPeriod,
    WorkingHoursReplace,
)
from app.services.opening_hours import is_open

router = APIRouter(prefix="/restaurant", tags=["restaurant"])

# A restaurant may only move an order forward, and only along these edges. Without
# this, a mis-click could send a delivered order back to "preparing".
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    # A restaurant can do NOTHING with an unpaid order — it should never have seen it.
    # Only the payment callback (or the reconciliation job) moves an order out of
    # AWAITING_PAYMENT. See services/payments/service.py.
    OrderStatus.AWAITING_PAYMENT: set(),
    OrderStatus.PENDING: {OrderStatus.ACCEPTED, OrderStatus.CANCELLED},
    OrderStatus.ACCEPTED: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED},
    OrderStatus.OUT_FOR_DELIVERY: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}

ACTIVE_STATUSES = [
    OrderStatus.PENDING,
    OrderStatus.ACCEPTED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.OUT_FOR_DELIVERY,
]

# Unpaid orders are invisible to the restaurant, full stop. Excluded from every query in
# this module — not merely hidden in the UI, because a restaurant that can see an unpaid
# order will cook it.
INVISIBLE_TO_RESTAURANT = [OrderStatus.AWAITING_PAYMENT]


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


@router.get("/me", response_model=RestaurantOut)
def get_my_restaurant(principal: CurrentStaff, db: DbSession) -> Restaurant:
    return db.get(Restaurant, principal.restaurant_id)


@router.patch("/me", response_model=RestaurantOut)
def update_my_restaurant(
    payload: RestaurantSettingsPatch, principal: CurrentStaff, db: DbSession
) -> Restaurant:
    restaurant = db.get(Restaurant, principal.restaurant_id)

    # RestaurantSettingsPatch cannot express commission_rate or status, so a
    # restaurant cannot raise its own margin or approve itself. That's the point.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(restaurant, field, value)

    db.commit()
    db.refresh(restaurant)
    return restaurant


@router.get("/me/open")
def am_i_open(principal: CurrentStaff, db: DbSession) -> dict:
    """What customers see right now — the schedule and the manual switch combined."""
    restaurant = db.get(Restaurant, principal.restaurant_id)
    return {
        "is_open": is_open(restaurant),
        "is_accepting_orders": restaurant.is_accepting_orders,
        "has_schedule": bool(restaurant.working_hours),
    }


# --------------------------------------------------------------------------- #
# Working hours
# --------------------------------------------------------------------------- #


@router.get("/working-hours", response_model=list[WorkingHoursOut])
def get_working_hours(
    principal: CurrentStaff, db: DbSession
) -> list[RestaurantWorkingHours]:
    return list(
        db.scalars(
            select(RestaurantWorkingHours)
            .where(RestaurantWorkingHours.restaurant_id == principal.restaurant_id)
            .order_by(RestaurantWorkingHours.day_of_week, RestaurantWorkingHours.opens_at)
        ).all()
    )


@router.put("/working-hours", response_model=list[WorkingHoursOut])
def replace_working_hours(
    payload: WorkingHoursReplace, principal: CurrentStaff, db: DbSession
) -> list[RestaurantWorkingHours]:
    """Replace the whole week in one transaction.

    Note for the UI: an empty list means "no schedule", which is treated as ALWAYS
    OPEN, not always closed — see services/opening_hours.py. Closing permanently is
    the is_accepting_orders switch, not an empty schedule.
    """
    _reject_overlaps(payload.periods)

    db.execute(
        delete(RestaurantWorkingHours).where(
            RestaurantWorkingHours.restaurant_id == principal.restaurant_id
        )
    )
    for period in payload.periods:
        db.add(
            RestaurantWorkingHours(
                restaurant_id=principal.restaurant_id, **period.model_dump()
            )
        )
    db.commit()

    return get_working_hours(principal, db)


def _reject_overlaps(periods: list[WorkingHoursPeriod]) -> None:
    """Two overlapping periods on one day are always a data-entry mistake, and they
    make 'is this place open?' ambiguous. Catch it at the door."""
    by_day: dict[int, list[WorkingHoursPeriod]] = {}
    for period in periods:
        by_day.setdefault(period.day_of_week, []).append(period)

    for day, day_periods in by_day.items():
        # Overnight periods legitimately wrap, so only compare same-day windows.
        simple = sorted(
            (p for p in day_periods if not p.crosses_midnight),
            key=lambda p: p.opens_at,
        )
        for earlier, later in zip(simple, simple[1:]):
            if later.opens_at < earlier.closes_at:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Overlapping opening hours on day {day}: "
                    f"{earlier.opens_at}-{earlier.closes_at} and "
                    f"{later.opens_at}-{later.closes_at}",
                )


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #


@router.get("/ratings", response_model=list[RatingOut])
def list_ratings(
    principal: CurrentStaff, db: DbSession, limit: int = Query(default=50, le=200)
) -> list[RatingOut]:
    rows = db.execute(
        select(OrderRating, Order.order_number, Customer.whatsapp_number)
        .join(Order, OrderRating.order_id == Order.id)
        .join(Customer, OrderRating.customer_id == Customer.id)
        .where(OrderRating.restaurant_id == principal.restaurant_id)
        .order_by(OrderRating.id.desc())
        .limit(limit)
    ).all()

    return [
        RatingOut(
            id=rating.id,
            order_id=rating.order_id,
            rating=rating.rating,
            comment=rating.comment,
            source=rating.source,
            created_at=rating.created_at,
            order_number=order_number,
            customer_number=number,
        )
        for rating, order_number, number in rows
    ]


@router.get("/ratings/summary", response_model=RatingSummary)
def rating_summary(principal: CurrentStaff, db: DbSession) -> RatingSummary:
    rows = db.execute(
        select(OrderRating.rating, func.count(OrderRating.id))
        .where(OrderRating.restaurant_id == principal.restaurant_id)
        .group_by(OrderRating.rating)
    ).all()

    breakdown = {score: 0 for score in range(1, 6)}
    for score, count in rows:
        breakdown[score] = count

    total = sum(breakdown.values())
    average = (
        sum(score * count for score, count in breakdown.items()) / total if total else None
    )

    return RatingSummary(
        average=round(average, 2) if average is not None else None,
        count=total,
        breakdown=breakdown,
    )


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #


@router.get("/categories", response_model=list[CategoryOut])
def list_categories(principal: CurrentStaff, db: DbSession) -> list[MenuCategory]:
    return list(
        db.scalars(
            select(MenuCategory)
            .where(MenuCategory.restaurant_id == principal.restaurant_id)
            .order_by(MenuCategory.sort_order, MenuCategory.id)
        ).all()
    )


@router.post("/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryIn, principal: CurrentStaff, db: DbSession) -> MenuCategory:
    category = MenuCategory(restaurant_id=principal.restaurant_id, **payload.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@router.get("/menu-items", response_model=list[MenuItemOut])
def list_menu_items(principal: CurrentStaff, db: DbSession) -> list[MenuItem]:
    return list(
        db.scalars(
            select(MenuItem)
            .outerjoin(MenuCategory, MenuItem.category_id == MenuCategory.id)
            .where(MenuItem.restaurant_id == principal.restaurant_id)
            .order_by(
                MenuCategory.sort_order.nulls_last(),
                MenuCategory.id.nulls_last(),
                MenuItem.sort_order,
                MenuItem.name,
            )
        ).all()
    )


def _validate_category(db: DbSession, category_id: int | None, restaurant_id: int) -> None:
    if category_id is None:
        return
    category = db.get(MenuCategory, category_id)
    if category is None or category.restaurant_id != restaurant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown category")


@router.post("/menu-items", response_model=MenuItemOut, status_code=status.HTTP_201_CREATED)
def create_menu_item(payload: MenuItemIn, principal: CurrentStaff, db: DbSession) -> MenuItem:
    _validate_category(db, payload.category_id, principal.restaurant_id)

    item = MenuItem(restaurant_id=principal.restaurant_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _owned_item(db: DbSession, item_id: int, restaurant_id: int) -> MenuItem:
    item = db.get(MenuItem, item_id)
    # 404 rather than 403 on someone else's item: don't confirm it exists.
    if item is None or item.restaurant_id != restaurant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Menu item not found")
    return item


@router.patch("/menu-items/{item_id}", response_model=MenuItemOut)
def update_menu_item(
    item_id: int, payload: MenuItemPatch, principal: CurrentStaff, db: DbSession
) -> MenuItem:
    item = _owned_item(db, item_id, principal.restaurant_id)

    updates = payload.model_dump(exclude_unset=True)
    if "category_id" in updates:
        _validate_category(db, updates["category_id"], principal.restaurant_id)

    for field, value in updates.items():
        setattr(item, field, value)

    db.commit()
    db.refresh(item)
    return item


@router.delete("/menu-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_menu_item(item_id: int, principal: CurrentStaff, db: DbSession) -> None:
    item = _owned_item(db, item_id, principal.restaurant_id)
    # order_items.menu_item_id is ON DELETE SET NULL and carries its own name/price
    # snapshot, so deleting a menu item never damages order history.
    db.delete(item)
    db.commit()


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    principal: CurrentStaff,
    db: DbSession,
    active_only: bool = Query(default=False, description="Only orders still in progress"),
    limit: int = Query(default=50, le=200),
) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.restaurant_id == principal.restaurant_id,
            Order.status.notin_(INVISIBLE_TO_RESTAURANT),
        )
        .options(selectinload(Order.items))
        .order_by(Order.placed_at.desc())
        .limit(limit)
    )
    if active_only:
        stmt = stmt.where(Order.status.in_(ACTIVE_STATUSES))

    return list(db.scalars(stmt).all())


@router.patch("/orders/{order_id}/status", response_model=OrderOut)
def update_order_status(
    order_id: int, payload: OrderStatusUpdate, principal: CurrentStaff, db: DbSession
) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.restaurant_id != principal.restaurant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    # 404, not 409: as far as the restaurant is concerned this order does not exist yet.
    if order.status in INVISIBLE_TO_RESTAURANT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    if payload.status not in ALLOWED_TRANSITIONS[order.status]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot move an order from {order.status.value} to {payload.status.value}",
        )

    order.status = payload.status
    order.status_history.append(
        OrderStatusHistory(
            status=payload.status,
            changed_by=f"staff:{principal.staff.id}",
            note=payload.note,
        )
    )

    # A delivered COD order HAS been paid — the rider took the cash at the door. Nothing
    # was recording that, so COD orders sat UNPAID forever and refund logic had to infer
    # payment from the status instead of being told.
    if (
        payload.status == OrderStatus.DELIVERED
        and order.payment_method == PaymentMethod.COD
        and order.payment_status == PaymentStatus.UNPAID
    ):
        order.payment_status = PaymentStatus.PAID

    db.commit()
    db.refresh(order)
    # TODO(V1): notify the customer on WhatsApp here. Blocked on the 24h-window /
    # template question — see the note in services/whatsapp.py.
    return order


@router.get("/stats")
def today_stats(principal: CurrentStaff, db: DbSession) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    row = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0),
        ).where(
            Order.restaurant_id == principal.restaurant_id,
            Order.placed_at >= since,
            Order.status != OrderStatus.CANCELLED,
            # An unpaid order is not revenue. Counting it would show a restaurant money
            # it has not earned and may never earn.
            Order.status.notin_(INVISIBLE_TO_RESTAURANT),
        )
    ).one()

    active = db.scalar(
        select(func.count(Order.id)).where(
            Order.restaurant_id == principal.restaurant_id,
            Order.status.in_(ACTIVE_STATUSES),
        )
    )

    return {
        "orders_24h": row[0],
        "revenue_24h": str(row[1]),
        "active_orders": active,
    }
