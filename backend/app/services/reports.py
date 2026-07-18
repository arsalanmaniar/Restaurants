"""Financial reports — date-range revenue summaries.

Shared by three endpoints (admin platform-wide, admin per-restaurant, restaurant
self-service) so "what counts as revenue" is defined in exactly one place.

Dates in, dates out: callers pass `date` objects meaning calendar days in
Asia/Karachi (the business's timezone — see services/opening_hours.py), not UTC.
`end_date` is inclusive, so a report for 2026-07-01..2026-07-01 covers that whole
Karachi day.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    MenuCategory,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)
from app.services.opening_hours import PAKISTAN_TZ

# The dashboard has no use for "give me every order the platform has ever taken" in
# one query — protect the DB from that. 366 covers a full leap year's worth of range.
MAX_RANGE_DAYS = 366

UNCATEGORISED = "Uncategorised"


class ReportRangeError(ValueError):
    """start_date/end_date fail validation. Callers turn this into a 422."""


@dataclass
class ReportData:
    gross_sales: Decimal
    cancelled_amount: Decimal
    net_sales: Decimal
    cash_amount: Decimal
    online_amount: Decimal
    order_count: int
    customer_count: int
    avg_order_amount: Decimal
    delivery_count: int
    pickup_count: int
    top_categories: list[dict]
    top_items: list[dict]


def validate_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ReportRangeError("end_date must not be before start_date")
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise ReportRangeError(
            f"Date range too large: max {MAX_RANGE_DAYS} days, got "
            f"{(end_date - start_date).days + 1}"
        )


def build_report(db: Session, restaurant_id: int | None, start_date: date, end_date: date) -> ReportData:
    validate_range(start_date, end_date)

    start_dt = datetime.combine(start_date, time.min, tzinfo=PAKISTAN_TZ)
    # end_date is inclusive; the exclusive upper bound is midnight AFTER end_date.
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=PAKISTAN_TZ)

    scope = [Order.placed_at >= start_dt, Order.placed_at < end_dt]
    if restaurant_id is not None:
        scope.append(Order.restaurant_id == restaurant_id)

    # Awaiting-payment orders are invisible everywhere else in the app (nobody has
    # paid, the restaurant never saw them) — a financial report must not count them
    # either, in gross sales or anywhere downstream.
    real_orders = [*scope, Order.status != OrderStatus.AWAITING_PAYMENT]
    # "Counted" = actually happened: not cancelled, not a ghost awaiting-payment cart.
    counted = [*scope, Order.status.notin_([OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED])]

    # Gross sales = every real order placed in the window, cancelled or not. Net sales
    # backs the cancelled amount back out. This matches standard POS "gross / voided /
    # net" reporting rather than treating cancelled orders as if they never existed.
    net_sales, cancelled_amount = db.execute(
        select(
            func.coalesce(
                func.sum(case((Order.status != OrderStatus.CANCELLED, Order.total_amount), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((Order.status == OrderStatus.CANCELLED, Order.total_amount), else_=0)),
                0,
            ),
        ).where(*real_orders)
    ).one()
    gross_sales = net_sales + cancelled_amount

    order_count, cash_amount, online_amount = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(
                func.sum(case((Order.payment_method == PaymentMethod.COD, Order.total_amount), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((Order.payment_method != PaymentMethod.COD, Order.total_amount), else_=0)),
                0,
            ),
        ).where(*counted)
    ).one()

    customer_count = db.scalar(
        select(func.count(func.distinct(Order.customer_id))).where(*counted)
    ) or 0

    avg_order_amount = (net_sales / order_count) if order_count else Decimal("0")

    # Postgres requires the GROUP BY / ORDER BY expressions to be the exact same
    # bound-parameter node as the SELECT expression, not merely an equal-looking one —
    # two separate `func.coalesce(..., "Uncategorised")` calls bind two different
    # anonymous parameters and Postgres rejects that as "not in GROUP BY". Building the
    # expression once and reusing the same object everywhere avoids it.
    category_name = func.coalesce(MenuCategory.name, UNCATEGORISED)
    category_revenue = func.coalesce(func.sum(OrderItem.line_total), 0)
    category_rows = db.execute(
        select(
            category_name.label("name"),
            category_revenue.label("revenue"),
            func.count(func.distinct(Order.id)).label("order_count"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .outerjoin(MenuItem, OrderItem.menu_item_id == MenuItem.id)
        .outerjoin(MenuCategory, MenuItem.category_id == MenuCategory.id)
        .where(*counted)
        .group_by(category_name)
        .order_by(category_revenue.desc())
        .limit(3)
    ).all()

    item_revenue = func.coalesce(func.sum(OrderItem.line_total), 0)
    item_rows = db.execute(
        select(
            OrderItem.item_name.label("name"),
            item_revenue.label("revenue"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity_sold"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*counted)
        .group_by(OrderItem.item_name)
        .order_by(item_revenue.desc())
        .limit(3)
    ).all()

    return ReportData(
        gross_sales=gross_sales,
        cancelled_amount=cancelled_amount,
        net_sales=net_sales,
        cash_amount=cash_amount,
        online_amount=online_amount,
        order_count=order_count,
        customer_count=customer_count,
        avg_order_amount=avg_order_amount,
        # No delivery-vs-pickup field exists anywhere in the schema (Order only ever
        # carries a delivery_address_text — there is no fulfilment-type column). Rather
        # than invent one, both counts are always 0. See the feature handoff notes.
        delivery_count=0,
        pickup_count=0,
        top_categories=[
            {"name": row.name, "revenue": row.revenue, "order_count": row.order_count}
            for row in category_rows
        ],
        top_items=[
            {"name": row.name, "revenue": row.revenue, "quantity_sold": row.quantity_sold}
            for row in item_rows
        ],
    )
