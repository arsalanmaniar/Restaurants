"""Refunds — admin only, and never more than was actually paid."""

from decimal import Decimal

from sqlalchemy import select

from app.models import PaymentStatus, Refund, RefundStatus


class TestOnlyAdminsCanRefund:
    def test_restaurant_staff_cannot_issue_a_refund(self, client, delivered_order,
                                                    pizza_headers):
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=pizza_headers,
            json={"amount": "100.00", "reason": "test"},
        )
        assert response.status_code == 403

    def test_restaurant_staff_cannot_even_view_refunds(self, client, delivered_order,
                                                       pizza_headers):
        response = client.get(
            f"/admin/orders/{delivered_order.id}/refunds", headers=pizza_headers
        )
        assert response.status_code == 403

    def test_unauthenticated_refund_rejected(self, client, delivered_order):
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            json={"amount": "100.00", "reason": "test"},
        )
        assert response.status_code == 401

    def test_staff_cannot_complete_a_refund(self, client, delivered_order, admin_headers,
                                            pizza_headers):
        created = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": "100.00", "reason": "cold food"},
        ).json()

        response = client.post(
            f"/admin/refunds/{created['id']}/complete", headers=pizza_headers
        )
        assert response.status_code == 403


class TestCannotRefundWhatWasNeverPaid:
    def test_unpaid_order_has_nothing_to_refund(self, client, cod_order, admin_headers):
        state = client.get(
            f"/admin/orders/{cod_order.id}/refunds", headers=admin_headers
        ).json()
        assert Decimal(state["amount_paid"]) == 0

        response = client.post(
            f"/admin/orders/{cod_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "customer complained"},
        )
        assert response.status_code == 400


class TestRefundCeiling:
    def test_delivered_cod_is_fully_refundable(self, client, delivered_order, admin_headers):
        state = client.get(
            f"/admin/orders/{delivered_order.id}/refunds", headers=admin_headers
        ).json()
        assert Decimal(state["amount_paid"]) == delivered_order.total_amount
        assert Decimal(state["refundable"]) == delivered_order.total_amount

    def test_cannot_refund_more_than_was_paid(self, client, delivered_order, admin_headers):
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": str(delivered_order.total_amount + 1), "reason": "over-refund"},
        )
        assert response.status_code == 400

    def test_pending_refunds_count_against_the_ceiling(self, client, delivered_order,
                                                       admin_headers):
        """Otherwise an admin could authorise the same money twice while the first
        refund is still in flight."""
        total = delivered_order.total_amount
        half = (total / 2).quantize(Decimal("0.01"))

        first = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": str(half), "reason": "one pizza was cold"},
        )
        assert first.status_code == 201
        assert first.json()["status"] == "pending"

        second = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": str(total), "reason": "second bite"},
        )
        assert second.status_code == 400

    def test_partial_refunds_sum_to_the_total(self, client, delivered_order, admin_headers):
        total = delivered_order.total_amount
        half = (total / 2).quantize(Decimal("0.01"))

        client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": str(half), "reason": "partial"},
        )
        # amount omitted -> refund whatever is left
        remainder = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "the rest"},
        ).json()

        assert Decimal(remainder["amount"]) == total - half

        state = client.get(
            f"/admin/orders/{delivered_order.id}/refunds", headers=admin_headers
        ).json()
        assert Decimal(state["amount_refunded"]) == total
        assert Decimal(state["refundable"]) == 0

    def test_fully_refunded_order_refuses_more(self, client, delivered_order, admin_headers):
        client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "full refund"},
        )
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": "1.00", "reason": "one more"},
        )
        assert response.status_code == 400

    def test_reason_is_required(self, client, delivered_order, admin_headers):
        """An unexplained refund is unauditable, which defeats the point of the record."""
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": "100.00"},
        )
        assert response.status_code == 422

    def test_zero_amount_rejected(self, client, delivered_order, admin_headers):
        response = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": "0", "reason": "nothing"},
        )
        assert response.status_code == 422


class TestCompletingARefund:
    def test_order_stays_paid_while_a_refund_is_pending(self, db, client, delivered_order,
                                                        admin_headers):
        client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "full refund"},
        )
        db.refresh(delivered_order)
        assert delivered_order.payment_status == PaymentStatus.PAID

    def test_full_refund_flips_the_order_to_refunded(self, db, client, delivered_order,
                                                     admin_headers):
        created = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "full refund"},
        ).json()

        completed = client.post(
            f"/admin/refunds/{created['id']}/complete", headers=admin_headers
        ).json()
        assert completed["status"] == "completed"

        db.refresh(delivered_order)
        assert delivered_order.payment_status == PaymentStatus.REFUNDED

    def test_partial_refund_leaves_the_order_paid(self, db, client, delivered_order,
                                                  admin_headers):
        """The customer is still out of pocket for the rest."""
        half = (delivered_order.total_amount / 2).quantize(Decimal("0.01"))
        created = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"amount": str(half), "reason": "partial"},
        ).json()
        client.post(f"/admin/refunds/{created['id']}/complete", headers=admin_headers)

        db.refresh(delivered_order)
        assert delivered_order.payment_status == PaymentStatus.PAID

    def test_completing_twice_is_idempotent(self, client, delivered_order, admin_headers):
        created = client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "full"},
        ).json()

        first = client.post(f"/admin/refunds/{created['id']}/complete", headers=admin_headers)
        second = client.post(f"/admin/refunds/{created['id']}/complete", headers=admin_headers)

        assert first.json()["status"] == second.json()["status"] == "completed"


class TestAuditTrail:
    def test_every_refund_names_an_admin_and_a_reason(self, db, client, delivered_order,
                                                      admin_headers, admin):
        client.post(
            f"/admin/orders/{delivered_order.id}/refunds",
            headers=admin_headers,
            json={"reason": "food arrived cold"},
        )

        refunds = db.scalars(
            select(Refund).where(Refund.order_id == delivered_order.id)
        ).all()
        assert len(refunds) == 1
        assert refunds[0].issued_by == admin.id
        assert refunds[0].reason == "food arrived cold"
        assert refunds[0].status == RefundStatus.PENDING
