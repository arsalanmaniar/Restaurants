"""Admin dashboard: approvals, plans, cross-restaurant views, platform revenue."""

from decimal import Decimal

from sqlalchemy import select

from app.core.security import verify_password
from app.models import Restaurant, RestaurantStaff, RestaurantStatus
from app.services import tools


class TestRestaurantApproval:
    def test_pending_restaurant_is_hidden_from_customers(self, db, conversation):
        """Approval must actually gate ordering, not just set a flag."""
        signup = Restaurant(
            name="New Signup Kebab House",
            phone="923004440009",
            cuisine_type="BBQ",
            status=RestaurantStatus.PENDING,
            commission_rate=Decimal("15.00"),
        )
        db.add(signup)
        db.flush()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert signup.name not in names

    def test_approving_makes_it_orderable(self, db, client, conversation, admin_headers):
        signup = Restaurant(
            name="New Signup Kebab House",
            phone="923004440009",
            cuisine_type="BBQ",
            status=RestaurantStatus.PENDING,
            commission_rate=Decimal("15.00"),
        )
        db.add(signup)
        db.flush()

        response = client.patch(
            f"/admin/restaurants/{signup.id}", headers=admin_headers, json={"status": "active"}
        )
        assert response.status_code == 200

        db.expire_all()
        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert signup.name in names

    def test_suspending_hides_it_again(self, db, client, conversation, admin_headers, pizza):
        client.patch(
            f"/admin/restaurants/{pizza.id}", headers=admin_headers, json={"status": "suspended"}
        )
        db.expire_all()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert "Pizza Junction" not in names

    def test_pending_count_appears_in_stats(self, db, client, admin_headers):
        db.add(
            Restaurant(
                name="Another Signup",
                phone="923004440010",
                status=RestaurantStatus.PENDING,
                commission_rate=Decimal("15.00"),
            )
        )
        db.flush()

        stats = client.get("/admin/stats", headers=admin_headers).json()
        assert stats["pending_approval"] >= 1


def _onboarding_payload(**overrides):
    payload = {
        "name": "Onboarding Test Kitchen",
        "phone": "923001112222",
        "address": "12 Test Street, Karachi",
        "email": "owner@onboardingtest.pk",
        "password": "correct-horse-battery",
    }
    payload.update(overrides)
    return payload


class TestRestaurantOnboarding:
    def test_creates_restaurant_and_owner_with_admin_supplied_credentials(
        self, db, client, admin_headers
    ):
        response = client.post(
            "/admin/restaurants", headers=admin_headers, json=_onboarding_payload()
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["restaurant"]["name"] == "Onboarding Test Kitchen"
        assert body["restaurant"]["status"] == "active"
        assert body["restaurant"]["is_accepting_orders"] is True

        restaurant_id = body["restaurant"]["id"]
        assert body["owner"] == {"email": "owner@onboardingtest.pk"}
        assert "temp_password" not in body["owner"]
        assert "username" not in body["owner"]

        staff = db.scalar(
            select(RestaurantStaff).where(RestaurantStaff.restaurant_id == restaurant_id)
        )
        assert staff is not None
        assert staff.role.value == "owner"
        assert staff.email == "owner@onboardingtest.pk"
        assert verify_password("correct-horse-battery", staff.password_hash)

    def test_admin_supplied_password_works_at_login(self, client, admin_headers):
        created = client.post(
            "/admin/restaurants", headers=admin_headers, json=_onboarding_payload()
        ).json()
        restaurant_id = created["restaurant"]["id"]

        login = client.post(
            "/auth/restaurant/login",
            json={"email": "owner@onboardingtest.pk", "password": "correct-horse-battery"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["restaurant_id"] == restaurant_id

    def test_duplicate_name_rejected(self, client, admin_headers, pizza):
        response = client.post(
            "/admin/restaurants",
            headers=admin_headers,
            json=_onboarding_payload(name=pizza.name),
        )
        assert response.status_code == 409

    def test_duplicate_email_returns_409(self, client, admin_headers):
        first = client.post(
            "/admin/restaurants", headers=admin_headers, json=_onboarding_payload()
        )
        assert first.status_code == 201, first.text

        second = client.post(
            "/admin/restaurants",
            headers=admin_headers,
            json=_onboarding_payload(name="A Different Restaurant"),
        )
        assert second.status_code == 409
        assert "email" in second.json()["detail"].lower()

    def test_short_password_returns_422(self, client, admin_headers):
        response = client.post(
            "/admin/restaurants",
            headers=admin_headers,
            json=_onboarding_payload(password="1234567"),
        )
        assert response.status_code == 422

    def test_malformed_email_returns_422(self, client, admin_headers):
        response = client.post(
            "/admin/restaurants",
            headers=admin_headers,
            json=_onboarding_payload(email="notanemail"),
        )
        assert response.status_code == 422

    def test_staff_cannot_create_restaurants(self, client, pizza_headers):
        response = client.post(
            "/admin/restaurants",
            headers=pizza_headers,
            json=_onboarding_payload(name="Sneaky Kitchen", email="sneaky@example.com"),
        )
        assert response.status_code == 403

    def test_deletes_restaurant_and_its_staff(self, db, client, admin_headers):
        created = client.post(
            "/admin/restaurants",
            headers=admin_headers,
            json=_onboarding_payload(
                name="Deletable Kitchen", email="owner@deletable.pk"
            ),
        ).json()
        restaurant_id = created["restaurant"]["id"]

        response = client.delete(
            f"/admin/restaurants/{restaurant_id}", headers=admin_headers
        )
        assert response.status_code == 204

        assert db.get(Restaurant, restaurant_id) is None
        assert (
            db.scalar(
                select(RestaurantStaff).where(RestaurantStaff.restaurant_id == restaurant_id)
            )
            is None
        )

    def test_cannot_delete_restaurant_with_orders(self, client, admin_headers, pizza, cod_order):
        response = client.delete(f"/admin/restaurants/{pizza.id}", headers=admin_headers)
        assert response.status_code == 409
        assert "order" in response.json()["detail"].lower()

    def test_deleting_unknown_restaurant_is_404(self, client, admin_headers):
        response = client.delete("/admin/restaurants/999999", headers=admin_headers)
        assert response.status_code == 404

    def test_staff_cannot_delete_restaurants(self, client, pizza_headers, biryani):
        response = client.delete(f"/admin/restaurants/{biryani.id}", headers=pizza_headers)
        assert response.status_code == 403


class TestCommission:
    def test_admin_can_override_a_restaurants_rate(self, db, client, admin_headers, pizza):
        response = client.patch(
            f"/admin/restaurants/{pizza.id}",
            headers=admin_headers,
            json={"commission_rate": "20.00"},
        )
        assert response.status_code == 200
        assert response.json()["commission_rate"] == "20.00"

    def test_commission_is_frozen_on_the_order(self, db, client, cod_order, admin_headers,
                                               pizza):
        """Changing a restaurant's rate must not rewrite what past orders earned."""
        original = cod_order.commission_amount

        client.patch(
            f"/admin/restaurants/{pizza.id}",
            headers=admin_headers,
            json={"commission_rate": "50.00"},
        )
        db.refresh(cod_order)

        assert cod_order.commission_amount == original


class TestSubscriptionPlans:
    def test_seeded_plans_are_listed(self, client, admin_headers):
        plans = client.get("/admin/subscription-plans", headers=admin_headers).json()
        assert {p["name"] for p in plans} >= {"Starter", "Growth", "Pro"}

    def test_duplicate_name_rejected(self, client, admin_headers):
        response = client.post(
            "/admin/subscription-plans",
            headers=admin_headers,
            json={"name": "Starter", "monthly_fee": "0"},
        )
        assert response.status_code == 409

    def test_cannot_delete_a_plan_in_use(self, client, admin_headers):
        plans = client.get("/admin/subscription-plans", headers=admin_headers).json()
        in_use = next(p for p in plans if p["restaurant_count"] > 0)

        response = client.delete(
            f"/admin/subscription-plans/{in_use['id']}", headers=admin_headers
        )
        assert response.status_code == 409

    def test_unknown_plan_id_is_a_clean_400(self, client, admin_headers, pizza):
        """Not a 500 with a Postgres stack trace."""
        response = client.patch(
            f"/admin/restaurants/{pizza.id}",
            headers=admin_headers,
            json={"subscription_plan_id": 999999},
        )
        assert response.status_code == 400

    def test_staff_cannot_see_plans(self, client, pizza_headers):
        assert client.get(
            "/admin/subscription-plans", headers=pizza_headers
        ).status_code == 403


class TestAllOrdersView:
    def test_lists_orders_across_restaurants(self, client, cod_order, admin_headers):
        orders = client.get("/admin/orders", headers=admin_headers).json()
        match = next(o for o in orders if o["order_number"] == cod_order.order_number)

        assert match["restaurant_name"] == "Pizza Junction"
        assert match["customer_number"] == "923001234567"

    def test_filter_by_restaurant(self, client, cod_order, admin_headers, pizza):
        orders = client.get(
            f"/admin/orders?restaurant_id={pizza.id}", headers=admin_headers
        ).json()
        assert all(o["restaurant_id"] == pizza.id for o in orders)

    def test_filter_by_status(self, client, cod_order, admin_headers):
        orders = client.get("/admin/orders?order_status=pending", headers=admin_headers).json()
        assert all(o["status"] == "pending" for o in orders)

    def test_invalid_status_rejected(self, client, admin_headers):
        assert client.get(
            "/admin/orders?order_status=nonsense", headers=admin_headers
        ).status_code == 422


class TestPlatformStats:
    def test_reports_commission(self, client, cod_order, admin_headers):
        stats = client.get("/admin/stats", headers=admin_headers).json()
        assert Decimal(stats["platform_commission"]) >= cod_order.commission_amount

    def test_cancelled_orders_are_not_revenue(self, db, client, cod_order, admin_headers,
                                              pizza_headers):
        before = Decimal(
            client.get("/admin/stats", headers=admin_headers).json()["gross_revenue"]
        )

        client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "cancelled"},
        )

        after = Decimal(
            client.get("/admin/stats", headers=admin_headers).json()["gross_revenue"]
        )
        assert after == before - cod_order.total_amount


class TestRatings:
    def test_summary_is_empty_for_a_new_restaurant(self, client, pizza_headers):
        summary = client.get("/restaurant/ratings/summary", headers=pizza_headers).json()
        assert "average" in summary and "breakdown" in summary

    def test_only_a_delivered_order_can_be_rated(self, db, cod_order, customer):
        from app.services import ratings

        try:
            ratings.record_rating(db, order=cod_order, customer_id=customer.id, rating=5)
            raise AssertionError("an undelivered order must not be rateable")
        except ratings.RatingError:
            pass

    def test_delivered_order_can_be_rated_once(self, db, delivered_order, customer):
        from app.services import ratings

        entry = ratings.record_rating(
            db, order=delivered_order, customer_id=customer.id, rating=5, comment="Great!"
        )
        assert entry.rating == 5

        try:
            ratings.record_rating(
                db, order=delivered_order, customer_id=customer.id, rating=1
            )
            raise AssertionError("the same order must not be rateable twice")
        except ratings.RatingError:
            pass

    def test_cannot_rate_someone_elses_order(self, db, delivered_order):
        from app.services import ratings

        try:
            ratings.record_rating(
                db, order=delivered_order, customer_id=999999, rating=1
            )
            raise AssertionError("must not be able to rate another customer's order")
        except ratings.RatingError:
            pass
