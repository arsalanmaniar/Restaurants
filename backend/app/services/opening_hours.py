"""Is a restaurant open right now?

Evaluated in Asia/Karachi, not UTC — a schedule of "11:00-23:00" means 11am local,
and at UTC a Karachi restaurant would appear to open at 4pm.

Rules, in order:
  1. `is_accepting_orders = False` -> closed, always. It's the manual "stop taking
     orders" switch and must beat any schedule.
  2. No working hours configured at all -> treated as CLOSED. A restaurant with no
     hours is a data bug, not a 24/7 kitchen: the old "no hours = open" default
     let an unfinished stub restaurant (empty menu + no hours) become the only
     "open" option during the gap between other restaurants' lunch/dinner windows,
     and the customer dead-ended trying to order from it.
  3. Otherwise -> open only inside a configured period for the current weekday.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.models import Restaurant, RestaurantWorkingHours

PAKISTAN_TZ = ZoneInfo("Asia/Karachi")


def _within(period: RestaurantWorkingHours, now: time) -> bool:
    if period.crosses_midnight or period.closes_at <= period.opens_at:
        # e.g. 19:00 -> 02:00 : open if after opening OR before closing.
        return now >= period.opens_at or now < period.closes_at
    return period.opens_at <= now < period.closes_at


def is_open(restaurant: Restaurant, at: datetime | None = None) -> bool:
    if not restaurant.is_accepting_orders:
        return False

    hours = list(restaurant.working_hours)
    if not hours:
        return False

    moment = (at or datetime.now(PAKISTAN_TZ)).astimezone(PAKISTAN_TZ)
    today = moment.weekday()
    yesterday = (today - 1) % 7
    now = moment.time()

    for period in hours:
        if period.day_of_week == today and _within(period, now):
            return True
        # A period that began yesterday and runs past midnight still covers us now.
        if (
            period.day_of_week == yesterday
            and (period.crosses_midnight or period.closes_at <= period.opens_at)
            and now < period.closes_at
        ):
            return True

    return False


def open_restaurant_ids(restaurants: list[Restaurant], at: datetime | None = None) -> set[int]:
    return {r.id for r in restaurants if is_open(r, at)}
