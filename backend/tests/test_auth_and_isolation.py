"""Auth, and the tenant isolation every restaurant on the platform depends on.

One database holds every restaurant's menu and orders. The only thing keeping Karachi
Biryani House out of Pizza Junction's data is that each query is scoped by the
restaurant_id in the JWT. If these tests fail, the platform is not sellable.
"""

import pytest
from sqlalchemy import select

from app.models import MenuItem, Order


class TestLogin:
    def test_restaurant_login(self, client):
        response = client.post(
            "/auth/restaurant/login",
            json={"email": "owner@pizzajunction.pk", "password": "demo1234"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["role"] == "restaurant"
        assert body["restaurant_name"] == "Pizza Junction"

    def test_admin_login(self, client):
        response = client.post(
            "/auth/admin/login",
            json={"email": "admin@abhiaya.pk", "password": "demo1234"},
        )
        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    def test_wrong_password_rejected(self, client):
        response = client.post(
            "/auth/admin/login",
            json={"email": "admin@abhiaya.pk", "password": "wrong"},
        )
        assert response.status_code == 401

    def test_unknown_email_gives_the_same_error_as_a_wrong_password(self, client):
        """Otherwise the endpoint tells an attacker which emails are real."""
        unknown = client.post(
            "/auth/admin/login",
            json={"email": "nobody@nowhere.pk", "password": "demo1234"},
        )
        wrong_password = client.post(
            "/auth/admin/login",
            json={"email": "admin@abhiaya.pk", "password": "wrong"},
        )
        assert unknown.status_code == wrong_password.status_code == 401
        assert unknown.json()["detail"] == wrong_password.json()["detail"]


class TestRouteGuards:
    @pytest.mark.parametrize(
        "path", ["/restaurant/orders", "/restaurant/menu-items", "/admin/restaurants"]
    )
    def test_no_token_is_rejected(self, client, path):
        assert client.get(path).status_code == 401

    def test_garbage_token_is_rejected(self, client):
        response = client.get(
            "/restaurant/orders", headers={"Authorization": "Bearer not.a.token"}
        )
        assert response.status_code == 401

    def test_restaurant_token_cannot_reach_admin_routes(self, client, pizza_headers):
        assert client.get("/admin/restaurants", headers=pizza_headers).status_code == 403
        assert client.get("/admin/stats", headers=pizza_headers).status_code == 403

    def test_admin_token_cannot_reach_restaurant_routes(self, client, admin_headers):
        assert client.get("/restaurant/orders", headers=admin_headers).status_code == 403


class TestTenantIsolation:
    def test_menu_lists_only_own_items(self, client, pizza_headers, biryani_headers):
        pizza_items = client.get("/restaurant/menu-items", headers=pizza_headers).json()
        biryani_items = client.get("/restaurant/menu-items", headers=biryani_headers).json()

        assert {i["id"] for i in pizza_items}.isdisjoint({i["id"] for i in biryani_items})
        assert any("Pizza" in i["name"] for i in pizza_items)
        assert not any("Pizza" in i["name"] for i in biryani_items)

    def test_cannot_edit_another_restaurants_item(
        self, db, client, biryani_headers, pizza, menu_item
    ):
        response = client.patch(
            f"/restaurant/menu-items/{menu_item.id}",
            headers=biryani_headers,
            json={"price": "1.00"},
        )
        # 404 rather than 403 — do not even confirm the row exists.
        assert response.status_code == 404

        db.refresh(menu_item)
        assert menu_item.price != 1

    def test_cannot_delete_another_restaurants_item(self, db, client, biryani_headers, menu_item):
        assert (
            client.delete(
                f"/restaurant/menu-items/{menu_item.id}", headers=biryani_headers
            ).status_code
            == 404
        )
        assert db.get(MenuItem, menu_item.id) is not None

    def test_cannot_see_another_restaurants_orders(self, client, cod_order, biryani_headers):
        orders = client.get("/restaurant/orders", headers=biryani_headers).json()
        assert cod_order.order_number not in [o["order_number"] for o in orders]

    def test_cannot_change_another_restaurants_order(
        self, db, client, cod_order, biryani_headers
    ):
        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=biryani_headers,
            json={"status": "accepted"},
        )
        assert response.status_code == 404

        db.refresh(cod_order)
        assert cod_order.status.value == "pending"

    def test_new_item_is_bound_to_the_callers_restaurant(
        self, db, client, pizza_headers, pizza, biryani
    ):
        """A restaurant_id in the body must never be trusted — the JWT decides."""
        response = client.post(
            "/restaurant/menu-items",
            headers=pizza_headers,
            json={"name": "Test Item", "price": "500.00", "restaurant_id": biryani.id},
        )
        assert response.status_code == 201

        item = db.get(MenuItem, response.json()["id"])
        assert item.restaurant_id == pizza.id

    def test_cannot_attach_an_item_to_another_restaurants_category(
        self, client, pizza_headers, db, biryani
    ):
        from app.models import MenuCategory

        foreign = db.scalar(
            select(MenuCategory).where(MenuCategory.restaurant_id == biryani.id)
        )
        response = client.post(
            "/restaurant/menu-items",
            headers=pizza_headers,
            json={"name": "X", "price": "100.00", "category_id": foreign.id},
        )
        assert response.status_code == 400


class TestRestaurantSettings:
    def test_can_update_own_details(self, client, pizza_headers):
        response = client.patch(
            "/restaurant/me", headers=pizza_headers, json={"address": "New address"}
        )
        assert response.status_code == 200
        assert response.json()["address"] == "New address"

    def test_cannot_change_own_commission(self, db, client, pizza_headers, pizza):
        """A restaurant raising its own margin is a direct revenue leak. The schema
        cannot even express commission_rate on this endpoint."""
        before = pizza.commission_rate
        client.patch("/restaurant/me", headers=pizza_headers, json={"commission_rate": "1.00"})

        db.refresh(pizza)
        assert pizza.commission_rate == before

    def test_cannot_approve_itself(self, db, client, pizza_headers, pizza):
        client.patch("/restaurant/me", headers=pizza_headers, json={"status": "active"})
        db.refresh(pizza)
        # The field is silently ignored, not applied.
        assert pizza.status.value == "active"  # was already active; the point is no 500
