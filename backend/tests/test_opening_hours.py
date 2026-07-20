"""Opening hours.

This logic decides whether a restaurant is visible to customers at all, so a bug here
either sends orders into a dark kitchen or makes a paying restaurant vanish from
WhatsApp. It is evaluated in Karachi time, and it has to cope with split shifts and
kitchens that run past midnight.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.models import RestaurantWorkingHours
from app.services import tools
from app.services.opening_hours import PAKISTAN_TZ, is_open

MONDAY = 0
FRIDAY = 4


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in Karachi. 2026-07-13 is a Monday."""
    return datetime(2026, 7, 13 + day, hour, minute, tzinfo=PAKISTAN_TZ)


@pytest.fixture
def scheduled(db, always_open, pizza):
    """Lunch 12:00–15:30 every day; dinner 18:30–23:30 Mon–Thu; Fri 18:30–01:30.

    Depends on `always_open` explicitly so the conftest fixture that wipes schedules has
    definitely run before we install ours — otherwise the order is luck.
    """
    pizza.working_hours.clear()
    db.flush()

    for day in range(7):
        pizza.working_hours.append(
            RestaurantWorkingHours(
                day_of_week=day, opens_at=time(12, 0), closes_at=time(15, 30)
            )
        )
    for day in range(0, 4):
        pizza.working_hours.append(
            RestaurantWorkingHours(
                day_of_week=day, opens_at=time(18, 30), closes_at=time(23, 30)
            )
        )
    pizza.working_hours.append(
        RestaurantWorkingHours(
            day_of_week=FRIDAY,
            opens_at=time(18, 30),
            closes_at=time(1, 30),
            crosses_midnight=True,
        )
    )
    db.flush()
    return pizza


class TestIsOpen:
    @pytest.mark.parametrize(
        "moment,expected,why",
        [
            (at(MONDAY, 13, 0), True, "lunch"),
            (at(MONDAY, 16, 30), False, "between shifts"),
            (at(MONDAY, 20, 0), True, "dinner"),
            (at(MONDAY, 3, 0), False, "dead of night"),
            (at(MONDAY, 11, 59), False, "one minute before opening"),
            (at(MONDAY, 15, 30), False, "closing time is exclusive"),
        ],
    )
    def test_schedule(self, scheduled, moment, expected, why):
        assert is_open(scheduled, moment) is expected, why

    def test_friday_night_runs_past_midnight(self, scheduled):
        assert is_open(scheduled, at(FRIDAY, 23, 59)) is True
        assert is_open(scheduled, at(FRIDAY + 1, 1, 0)) is True, "Friday's shift, Saturday 1am"
        assert is_open(scheduled, at(FRIDAY + 1, 2, 0)) is False, "after it ended"

    def test_evaluated_in_karachi_not_utc(self, scheduled):
        """08:00 UTC is 13:00 in Karachi — lunchtime. Getting this wrong shifts every
        restaurant's hours by five hours."""
        utc_0800 = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("UTC"))
        assert is_open(scheduled, utc_0800) is True

    def test_manual_pause_beats_the_schedule(self, db, scheduled):
        scheduled.is_accepting_orders = False
        db.flush()
        assert is_open(scheduled, at(MONDAY, 13, 0)) is False

    def test_no_schedule_means_always_closed(self, db, pizza):
        """Flipped default: a restaurant with no working_hours rows is a data bug
        (unfinished stub / never-onboarded merchant) rather than a 24/7 kitchen.
        The old "no hours = open" default let an empty-menu stub restaurant become
        the only "open" option during other restaurants' shift gaps and dead-ended
        customers trying to order from it."""
        pizza.working_hours.clear()
        db.flush()
        assert is_open(pizza, at(MONDAY, 3, 0)) is False


class TestClosedRestaurantsAreUnorderable:
    def test_hidden_from_list(self, db, conversation, scheduled):
        scheduled.is_accepting_orders = False
        db.flush()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert "Pizza Junction" not in names

    def test_menu_refuses(self, db, conversation, scheduled):
        scheduled.is_accepting_orders = False
        db.flush()
        assert tools.get_menu(db, conversation, restaurant_id=scheduled.id)["error"] == "closed"

    def test_cannot_order_from_a_closed_kitchen(self, db, cart_with_pizza, pizza):
        """A kitchen can close between building the cart and confirming it."""
        pizza.is_accepting_orders = False
        db.flush()

        result = tools.place_order(db, cart_with_pizza, delivery_address="House 1")
        assert "error" in result


class TestWorkingHoursApi:
    def test_read(self, client, pizza_headers):
        assert client.get("/restaurant/working-hours", headers=pizza_headers).status_code == 200

    def test_replace(self, client, pizza_headers):
        response = client.put(
            "/restaurant/working-hours",
            headers=pizza_headers,
            json={
                "periods": [
                    {
                        "day_of_week": 0,
                        "opens_at": "12:00:00",
                        "closes_at": "23:00:00",
                        "crosses_midnight": False,
                    }
                ]
            },
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_overlapping_periods_rejected(self, client, pizza_headers):
        response = client.put(
            "/restaurant/working-hours",
            headers=pizza_headers,
            json={
                "periods": [
                    {"day_of_week": 0, "opens_at": "12:00:00", "closes_at": "15:00:00",
                     "crosses_midnight": False},
                    {"day_of_week": 0, "opens_at": "14:00:00", "closes_at": "20:00:00",
                     "crosses_midnight": False},
                ]
            },
        )
        assert response.status_code == 400

    def test_zero_length_period_rejected(self, client, pizza_headers):
        response = client.put(
            "/restaurant/working-hours",
            headers=pizza_headers,
            json={
                "periods": [
                    {"day_of_week": 0, "opens_at": "12:00:00", "closes_at": "12:00:00",
                     "crosses_midnight": False}
                ]
            },
        )
        assert response.status_code == 422

    def test_invalid_weekday_rejected(self, client, pizza_headers):
        response = client.put(
            "/restaurant/working-hours",
            headers=pizza_headers,
            json={
                "periods": [
                    {"day_of_week": 7, "opens_at": "12:00:00", "closes_at": "15:00:00",
                     "crosses_midnight": False}
                ]
            },
        )
        assert response.status_code == 422
