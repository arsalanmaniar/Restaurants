"""Restaurant-facing dashboard: self-scoped financial reports (/restaurant/reports).

Menu/orders/ratings/working-hours endpoints on this router are already covered
elsewhere (test_orders.py, test_opening_hours.py, test_admin.py's TestRatings). This
file is specifically the restaurant-side counterpart to
test_admin.py::TestFinancialReports.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models import MenuItem, OrderStatus, PaymentMethod, Restaurant


def _item(db, restaurant: Restaurant, name_substring: str) -> MenuItem:
    return db.scalar(
        select(MenuItem).where(
            MenuItem.restaurant_id == restaurant.id,
            MenuItem.name.ilike(f"%{name_substring}%"),
        )
    )


class TestFinancialReports:
    def test_happy_path(self, db, client, make_order, pizza_headers, pizza):
        chicken_tikka_pizza = _item(db, pizza, "Chicken Tikka Pizza")
        pepperoni_pizza = _item(db, pizza, "Pepperoni Pizza")
        loaded_fries = _item(db, pizza, "Loaded Fries")

        make_order(
            pizza,
            customer_number="923006660001",
            items=[(chicken_tikka_pizza, 2)],
            payment_method=PaymentMethod.COD,
            status=OrderStatus.DELIVERED,
            placed_on=date(2024, 5, 10),
        )
        make_order(
            pizza,
            customer_number="923006660002",
            items=[(pepperoni_pizza, 1), (loaded_fries, 1)],
            payment_method=PaymentMethod.JAZZCASH,
            status=OrderStatus.DELIVERED,
            placed_on=date(2024, 5, 15),
        )
        make_order(
            pizza,
            customer_number="923006660003",
            items=[(chicken_tikka_pizza, 1)],
            status=OrderStatus.CANCELLED,
            placed_on=date(2024, 5, 20),
        )

        response = client.get(
            "/restaurant/reports?start_date=2024-05-01&end_date=2024-05-31",
            headers=pizza_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()

        # chicken tikka 1150*2=2300 + delivery 100 = 2400
        # pepperoni 1250 + fries 380 + delivery 100 = 1730
        # cancelled: chicken tikka 1150 + delivery 100 = 1250
        assert Decimal(body["gross_sales"]) == Decimal("5380.00")
        assert Decimal(body["cancelled_amount"]) == Decimal("1250.00")
        assert Decimal(body["net_sales"]) == Decimal("4130.00")
        assert Decimal(body["cash_amount"]) == Decimal("2400.00")
        assert Decimal(body["online_amount"]) == Decimal("1730.00")
        assert body["order_count"] == 2
        assert body["customer_count"] == 2
        assert Decimal(body["avg_order_amount"]) == Decimal("2065.00")
        assert body["delivery_count"] == 0
        assert body["pickup_count"] == 0

        item_names = [i["name"] for i in body["top_items"]]
        assert "Chicken Tikka Pizza (Medium)" in item_names
        assert "Pepperoni Pizza (Medium)" in item_names
        assert "Loaded Fries" in item_names

        category_names = {c["name"] for c in body["top_categories"]}
        assert category_names == {"Pizza", "Sides"}

    def test_scoped_to_own_restaurant_only(
        self, db, client, make_order, pizza_headers, biryani_headers, pizza, biryani
    ):
        """A restaurant's report must never include another restaurant's revenue —
        and unlike the admin endpoint there is no restaurant_id to even try passing."""
        chicken_tikka_pizza = _item(db, pizza, "Chicken Tikka Pizza")
        chicken_biryani = _item(db, biryani, "Chicken Biryani")

        make_order(
            pizza,
            customer_number="923006661001",
            items=[(chicken_tikka_pizza, 1)],
            status=OrderStatus.DELIVERED,
            placed_on=date(2024, 6, 1),
        )
        make_order(
            biryani,
            customer_number="923006661002",
            items=[(chicken_biryani, 5)],
            status=OrderStatus.DELIVERED,
            placed_on=date(2024, 6, 2),
        )

        pizza_report = client.get(
            "/restaurant/reports?start_date=2024-06-01&end_date=2024-06-30",
            headers=pizza_headers,
        ).json()
        biryani_report = client.get(
            "/restaurant/reports?start_date=2024-06-01&end_date=2024-06-30",
            headers=biryani_headers,
        ).json()

        # Pizza's order: 1150 + 100 delivery = 1250. Biryani's: 450*5 + 80 = 2330.
        assert Decimal(pizza_report["gross_sales"]) == Decimal("1250.00")
        assert Decimal(biryani_report["gross_sales"]) == Decimal("2330.00")
        assert pizza_report["order_count"] == 1
        assert biryani_report["order_count"] == 1

    def test_empty_range_is_all_zero(self, client, pizza_headers):
        response = client.get(
            "/restaurant/reports?start_date=2019-01-01&end_date=2019-01-31",
            headers=pizza_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert Decimal(body["gross_sales"]) == Decimal("0")
        assert Decimal(body["cancelled_amount"]) == Decimal("0")
        assert Decimal(body["net_sales"]) == Decimal("0")
        assert Decimal(body["cash_amount"]) == Decimal("0")
        assert Decimal(body["online_amount"]) == Decimal("0")
        assert Decimal(body["avg_order_amount"]) == Decimal("0")
        assert body["order_count"] == 0
        assert body["customer_count"] == 0
        assert body["top_categories"] == []
        assert body["top_items"] == []

    def test_end_before_start_is_422(self, client, pizza_headers):
        response = client.get(
            "/restaurant/reports?start_date=2024-02-01&end_date=2024-01-01",
            headers=pizza_headers,
        )
        assert response.status_code == 422

    def test_range_over_366_days_is_422_with_helpful_message(self, client, pizza_headers):
        response = client.get(
            "/restaurant/reports?start_date=2020-01-01&end_date=2023-01-01",
            headers=pizza_headers,
        )
        assert response.status_code == 422
        assert "366" in response.json()["detail"]

    def test_admin_cannot_call_restaurant_report(self, client, admin_headers):
        response = client.get(
            "/restaurant/reports?start_date=2024-01-01&end_date=2024-01-31",
            headers=admin_headers,
        )
        assert response.status_code == 403

    def test_requires_auth(self, client):
        response = client.get(
            "/restaurant/reports?start_date=2024-01-01&end_date=2024-01-31",
        )
        assert response.status_code == 401

    def test_cancelled_excluded_from_net_but_included_in_cancelled_amount(
        self, db, client, make_order, pizza_headers, pizza
    ):
        chicken_tikka_pizza = _item(db, pizza, "Chicken Tikka Pizza")
        delivered = make_order(
            pizza,
            customer_number="923006662001",
            items=[(chicken_tikka_pizza, 1)],
            status=OrderStatus.DELIVERED,
            placed_on=date(2024, 7, 5),
        )
        cancelled = make_order(
            pizza,
            customer_number="923006662002",
            items=[(chicken_tikka_pizza, 1)],
            status=OrderStatus.CANCELLED,
            placed_on=date(2024, 7, 6),
        )

        body = client.get(
            "/restaurant/reports?start_date=2024-07-01&end_date=2024-07-31",
            headers=pizza_headers,
        ).json()

        assert Decimal(body["net_sales"]) == delivered.total_amount
        assert Decimal(body["cancelled_amount"]) == cancelled.total_amount
        assert Decimal(body["gross_sales"]) == delivered.total_amount + cancelled.total_amount
        assert body["order_count"] == 1
