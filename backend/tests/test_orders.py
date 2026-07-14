"""The order state machine and the restaurant's orders board."""

import pytest
from decimal import Decimal

from app.models import OrderStatus, PaymentMethod, PaymentStatus


class TestStateMachine:
    def test_happy_path(self, client, cod_order, pizza_headers):
        for status in ("accepted", "preparing", "ready", "out_for_delivery", "delivered"):
            response = client.patch(
                f"/restaurant/orders/{cod_order.id}/status",
                headers=pizza_headers,
                json={"status": status},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == status

    @pytest.mark.parametrize("illegal", ["ready", "delivered", "out_for_delivery"])
    def test_cannot_skip_ahead(self, client, cod_order, pizza_headers, illegal):
        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": illegal},
        )
        assert response.status_code == 409

    def test_cannot_go_backwards(self, client, cod_order, pizza_headers):
        """A mis-click must not send a delivered order back to 'preparing'."""
        for status in ("accepted", "preparing", "ready"):
            client.patch(
                f"/restaurant/orders/{cod_order.id}/status",
                headers=pizza_headers,
                json={"status": status},
            )

        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "accepted"},
        )
        assert response.status_code == 409

    def test_delivered_is_terminal(self, client, delivered_order, pizza_headers):
        response = client.patch(
            f"/restaurant/orders/{delivered_order.id}/status",
            headers=pizza_headers,
            json={"status": "cancelled"},
        )
        assert response.status_code == 409

    def test_history_records_who_changed_it(self, db, client, cod_order, pizza_headers):
        client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "accepted"},
        )
        db.refresh(cod_order)

        latest = cod_order.status_history[-1]
        assert latest.status == OrderStatus.ACCEPTED
        assert latest.changed_by.startswith("staff:")


class TestCodPayment:
    def test_delivered_cod_order_counts_as_paid(self, db, delivered_order):
        """The rider took the cash at the door. Nothing recorded that, so COD orders sat
        UNPAID forever and could never be refunded."""
        assert delivered_order.payment_method == PaymentMethod.COD
        assert delivered_order.payment_status == PaymentStatus.PAID

    def test_undelivered_cod_order_is_not_paid(self, db, cod_order):
        assert cod_order.payment_status == PaymentStatus.UNPAID


class TestOrdersBoard:
    def test_lists_own_orders(self, client, cod_order, pizza_headers):
        orders = client.get("/restaurant/orders", headers=pizza_headers).json()
        assert cod_order.order_number in [o["order_number"] for o in orders]

    def test_active_only_filter_excludes_finished_orders(
        self, client, delivered_order, pizza_headers
    ):
        active = client.get(
            "/restaurant/orders?active_only=true", headers=pizza_headers
        ).json()
        assert delivered_order.order_number not in [o["order_number"] for o in active]

    def test_stats(self, client, cod_order, pizza_headers):
        stats = client.get("/restaurant/stats", headers=pizza_headers).json()
        assert stats["active_orders"] >= 1
        assert Decimal(stats["revenue_24h"]) >= cod_order.total_amount
