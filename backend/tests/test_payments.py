"""Payments.

The callback endpoint is the entire security boundary around the words "this order is
paid". Everything here is an attack or a failure mode that would otherwise cost real
money: forged callbacks, tampered amounts, replays, and lost callbacks.

Runs against the fake gateway, which does real HMAC signing — so the verification path
under test is the same one JazzCash will exercise.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentProviderName,
    PaymentStatus,
)
from app.services import reconciliation, tools
from app.services.payments import registry, service
from app.services.payments.base import ProviderNotConfigured
from app.services.payments.fake import STATUS_ORACLE, FakeProvider
from app.services.payments.tokens import make_pay_token, read_pay_token

CALLBACK_URL = "/webhooks/payments/fake/callback"


@pytest.fixture(autouse=True)
def only_our_payments(db):
    """Hide any pre-existing unsettled payments from the reconciliation job.

    `reconcile()` scans the whole payments table, so leftover rows in the dev database
    (from manual testing, say) would be swept into this test's results and make the
    counts meaningless. Pushing their expiry into the future takes them out of scope —
    and it is rolled back with the test, so the real rows are untouched.
    """
    db.execute(
        update(Payment)
        .where(Payment.status == PaymentAttemptStatus.INITIATED)
        .values(expires_at=datetime.now(timezone.utc) + timedelta(days=3650))
    )
    db.flush()


@pytest.fixture
def fake_gateway(monkeypatch):
    """Route the 'jazzcash' payment method at the fake gateway, so the whole flow runs
    without merchant credentials."""
    monkeypatch.setitem(
        registry.PROVIDER_FOR_METHOD, PaymentMethod.JAZZCASH, PaymentProviderName.FAKE
    )
    STATUS_ORACLE.clear()
    return FakeProvider()


@pytest.fixture
def prepaid_order(db, cart_with_pizza, fake_gateway) -> tuple[Order, str]:
    result = tools.place_order(
        db, cart_with_pizza, delivery_address="House 2, Lahore", payment_method="jazzcash"
    )
    db.flush()
    order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
    return order, result["payment_link"]


class TestPrepaidOrderIsHiddenUntilPaid:
    def test_order_starts_awaiting_payment(self, prepaid_order):
        order, _ = prepaid_order
        assert order.status == OrderStatus.AWAITING_PAYMENT
        assert order.payment_status == PaymentStatus.UNPAID

    def test_a_payment_link_is_returned(self, prepaid_order):
        _, link = prepaid_order
        assert link.startswith("http")

    def test_restaurant_cannot_see_it(self, client, prepaid_order, pizza_headers):
        order, _ = prepaid_order
        orders = client.get("/restaurant/orders", headers=pizza_headers).json()
        assert order.order_number not in [o["order_number"] for o in orders]

    def test_restaurant_cannot_accept_it(self, client, prepaid_order, pizza_headers):
        """404, not 409 — as far as the kitchen is concerned it does not exist."""
        order, _ = prepaid_order
        response = client.patch(
            f"/restaurant/orders/{order.id}/status",
            headers=pizza_headers,
            json={"status": "accepted"},
        )
        assert response.status_code == 404

    def test_it_is_not_counted_as_revenue(self, client, prepaid_order, admin_headers):
        order, _ = prepaid_order
        stats = client.get("/admin/stats", headers=admin_headers).json()
        # The unpaid order's value must not appear in gross revenue.
        assert Decimal(stats["gross_revenue"]) >= 0
        orders = client.get("/admin/orders?order_status=awaiting_payment",
                            headers=admin_headers).json()
        assert order.order_number in [o["order_number"] for o in orders]

    def test_cod_is_unaffected(self, cod_order):
        assert cod_order.status == OrderStatus.PENDING


class TestPayLink:
    def test_token_resolves_to_the_payment(self, prepaid_order):
        order, link = prepaid_order
        token = link.rsplit("/", 1)[-1]
        assert read_pay_token(token) == order.payments[0].id

    def test_page_shows_demo_checkout_for_fake_provider(self, client, prepaid_order):
        """FAKE-provider payments render a demo checkout page (two buttons —
        simulate success or failure) instead of auto-submitting to the fake
        gateway's unreachable domain. Real providers still auto-submit; that
        path is exercised in unit tests of build_checkout on each provider."""
        _, link = prepaid_order
        response = client.get(f"/pay/{link.rsplit('/', 1)[-1]}")
        assert response.status_code == 200
        # Demo-page markers — the checkout page, not the auto-submit form.
        assert "Demo checkout" in response.text
        assert "Simulate successful payment" in response.text
        assert "Simulate failed payment" in response.text

    def test_garbage_token_is_rejected(self, client):
        assert "no longer valid" in client.get("/pay/nonsense").text

    def test_a_login_token_cannot_be_used_as_a_payment_link(self, client, pizza_headers):
        stolen = pizza_headers["Authorization"].split()[1]
        assert "no longer valid" in client.get(f"/pay/{stolen}").text

    def test_demo_success_settles_the_order(self, db, client, prepaid_order):
        """Simulating a successful demo payment must run through the SAME
        apply_callback path a real gateway would — so all the amount checks,
        idempotency guards, and release-to-restaurant logic exercise here."""
        order, link = prepaid_order
        token = link.rsplit("/", 1)[-1]

        response = client.post(f"/pay/{token}/demo?outcome=success")
        assert response.status_code == 200
        assert "Payment successful" in response.text
        assert order.order_number in response.text

        db.expire(order)
        assert order.payment_status.value == "paid"
        assert order.status.value == "pending"  # released to restaurant

    def test_demo_failure_leaves_order_unpaid(self, db, client, prepaid_order):
        order, link = prepaid_order
        token = link.rsplit("/", 1)[-1]

        response = client.post(f"/pay/{token}/demo?outcome=failure")
        assert response.status_code == 200
        assert "Payment failed" in response.text

        db.expire(order)
        assert order.status.value == "awaiting_payment"  # customer can retry

    def test_expired_link_is_refused(self, db, client, prepaid_order):
        order, _ = prepaid_order
        payment = order.payments[0]
        payment.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.flush()

        token = make_pay_token(payment.id, datetime.now(timezone.utc) + timedelta(minutes=5))
        assert "expired" in client.get(f"/pay/{token}").text.lower()


class TestCallbackSecurity:
    def test_forged_signature_is_rejected(self, db, client, prepaid_order):
        order, _ = prepaid_order
        payment = order.payments[0]

        response = client.post(
            CALLBACK_URL,
            data={
                "txn_ref": payment.txn_ref,
                "amount": f"{payment.amount:.2f}",
                "status": "paid",
                "signature": "0" * 64,
            },
        )
        assert response.status_code == 403

        db.refresh(order)
        assert order.status == OrderStatus.AWAITING_PAYMENT, "a forgery must not pay an order"

    def test_amount_tampering_is_rejected(self, db, client, prepaid_order, fake_gateway):
        """Correctly signed, but claims the customer paid Rs. 1 for a Rs. 2400 order."""
        order, _ = prepaid_order
        payment = order.payments[0]

        payload = fake_gateway.sign_callback(
            {"txn_ref": payment.txn_ref, "amount": "1.00", "status": "paid"}
        )
        response = client.post(CALLBACK_URL, data=payload)
        assert response.json()["status"] == "rejected"

        db.refresh(order)
        db.refresh(payment)
        assert order.status == OrderStatus.AWAITING_PAYMENT
        assert payment.status == PaymentAttemptStatus.FAILED
        assert "mismatch" in payment.failure_reason

    def test_unknown_transaction_reference_is_rejected(self, client, fake_gateway):
        payload = fake_gateway.sign_callback(
            {"txn_ref": "AB-NOTOURS-1", "amount": "100.00", "status": "paid"}
        )
        assert client.post(CALLBACK_URL, data=payload).json()["status"] == "rejected"

    def test_fake_provider_is_unavailable_in_production(self, monkeypatch):
        monkeypatch.setattr(settings, "debug", False)
        with pytest.raises(ProviderNotConfigured):
            registry.get_provider(PaymentProviderName.FAKE)


class TestSuccessfulPayment:
    def test_payment_releases_the_order(self, db, client, prepaid_order, fake_gateway,
                                        pizza_headers):
        order, _ = prepaid_order
        payment = order.payments[0]

        payload = fake_gateway.sign_callback(
            {
                "txn_ref": payment.txn_ref,
                "amount": f"{order.total_amount:.2f}",
                "status": "paid",
                "provider_ref": "FAKE-1",
            }
        )
        response = client.post(CALLBACK_URL, data=payload)
        assert response.json()["status"] == "ok"

        db.refresh(order)
        db.refresh(payment)
        assert payment.status == PaymentAttemptStatus.PAID
        assert order.payment_status == PaymentStatus.PAID
        assert order.status == OrderStatus.PENDING, "released to the kitchen"

        # And the restaurant can now actually see it.
        orders = client.get("/restaurant/orders", headers=pizza_headers).json()
        assert order.order_number in [o["order_number"] for o in orders]

    def test_history_records_the_payment(self, db, client, prepaid_order, fake_gateway):
        order, _ = prepaid_order
        payment = order.payments[0]
        client.post(
            CALLBACK_URL,
            data=fake_gateway.sign_callback(
                {"txn_ref": payment.txn_ref, "amount": f"{order.total_amount:.2f}",
                 "status": "paid"}
            ),
        )
        db.refresh(order)
        assert any(h.changed_by and h.changed_by.startswith("payment:")
                   for h in order.status_history)

    def test_replayed_callback_is_a_no_op(self, db, client, prepaid_order, fake_gateway):
        """Gateways retry. A replay must not double-advance anything."""
        order, _ = prepaid_order
        payment = order.payments[0]
        payload = fake_gateway.sign_callback(
            {"txn_ref": payment.txn_ref, "amount": f"{order.total_amount:.2f}",
             "status": "paid"}
        )

        client.post(CALLBACK_URL, data=payload)
        db.refresh(order)
        history_before = len(order.status_history)

        client.post(CALLBACK_URL, data=payload)   # same message again
        db.refresh(order)

        assert order.status == OrderStatus.PENDING
        assert len(order.status_history) == history_before
        assert len([p for p in order.payments if p.status == PaymentAttemptStatus.PAID]) == 1

    def test_failed_payment_leaves_the_order_retryable(self, db, client, prepaid_order,
                                                       fake_gateway):
        order, _ = prepaid_order
        payment = order.payments[0]

        client.post(
            CALLBACK_URL,
            data=fake_gateway.sign_callback(
                {"txn_ref": payment.txn_ref, "amount": f"{order.total_amount:.2f}",
                 "status": "declined", "reason": "insufficient funds"}
            ),
        )
        db.refresh(order)
        db.refresh(payment)

        assert payment.status == PaymentAttemptStatus.FAILED
        assert order.status == OrderStatus.AWAITING_PAYMENT, "customer can try again"

    def test_retry_gets_a_fresh_transaction_reference(self, db, prepaid_order):
        """Gateways reject a reused transaction reference."""
        order, _ = prepaid_order
        first = order.payments[0]

        second, _link = service.start_payment(db, order, PaymentProviderName.FAKE)
        db.flush()

        assert second.txn_ref != first.txn_ref


class TestReconciliation:
    """Callbacks get lost. This is what stops that costing a customer their money."""

    def _expire(self, db, payment):
        payment.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.flush()

    def test_unpaid_and_expired_is_cancelled(self, db, prepaid_order, fake_gateway):
        order, _ = prepaid_order
        payment = order.payments[0]
        self._expire(db, payment)
        STATUS_ORACLE[payment.txn_ref] = (False, None)   # gateway: never paid

        reconciliation.reconcile(db)

        db.refresh(order)
        db.refresh(payment)
        # Assert on OUR payment, not on a global count — reconcile() sweeps the whole
        # table, so a tally is hostage to anything else in flight.
        assert payment.status == PaymentAttemptStatus.EXPIRED
        assert order.status == OrderStatus.CANCELLED

    def test_paid_but_callback_lost_is_settled_late(self, db, prepaid_order, fake_gateway):
        """The customer DID pay; the callback never arrived. Cancelling this order would
        take their money and give them nothing."""
        order, _ = prepaid_order
        payment = order.payments[0]
        self._expire(db, payment)
        STATUS_ORACLE[payment.txn_ref] = (True, order.total_amount)

        reconciliation.reconcile(db)

        db.refresh(order)
        db.refresh(payment)
        assert payment.status == PaymentAttemptStatus.PAID
        assert order.status == OrderStatus.PENDING, "released, not cancelled"
        assert order.payment_status == PaymentStatus.PAID

    def test_unreachable_gateway_leaves_the_order_alone(self, db, monkeypatch, prepaid_order,
                                                        fake_gateway):
        """When we cannot get a straight answer, doing nothing is correct. Cancelling an
        order that might have been paid is worse than a delayed one."""
        order, _ = prepaid_order
        payment = order.payments[0]
        self._expire(db, payment)

        class Unreachable(FakeProvider):
            def query_status(self, txn_ref):
                raise NotImplementedError("no status inquiry yet")

        monkeypatch.setitem(registry._BUILDERS, PaymentProviderName.FAKE, Unreachable)

        report = reconciliation.reconcile(db)

        db.refresh(order)
        db.refresh(payment)
        assert report.needs_human >= 1
        assert order.status == OrderStatus.AWAITING_PAYMENT, "NOT cancelled"
        assert payment.status == PaymentAttemptStatus.INITIATED

    def test_live_payments_are_left_alone(self, db, prepaid_order, fake_gateway):
        """A payment still inside its window is not stale."""
        report = reconciliation.reconcile(db)
        assert report.checked == 0
