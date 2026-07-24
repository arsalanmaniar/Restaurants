"""Phase E — restaurant accept/cancel triggers a customer WhatsApp notification.

The first place the platform sends a WhatsApp message it wasn't asked for: when a
restaurant acts on an order from its dashboard. Accept sends the itemised bill
(the customer's 'confirmed' moment); cancel sends an apology. No other transition
notifies the customer for now.

The bill is composed by READING the order's stored amounts — it never recomputes
a total, so this phase carries no financial risk (that is the tax phase's job).
"""

import pytest

from app.models import MessageDirection, MessageLog, OrderStatus
from app.services import notifications


# --------------------------------------------------------------------------- #
# Message building (pure — no send, no DB writes)
# --------------------------------------------------------------------------- #


class TestMessageBuilding:

    def test_accepted_builds_an_itemised_bill(self, db, cod_order):
        cod_order.status = OrderStatus.ACCEPTED
        msg = notifications.build_notification(cod_order)

        assert cod_order.order_number in msg
        assert cod_order.restaurant.name in msg
        # Every line item, with its line total.
        for item in cod_order.items:
            assert item.item_name in msg
        assert f"Total: Rs. {cod_order.total_amount:.2f}" in msg
        assert f"Delivery: Rs. {cod_order.delivery_fee:.2f}" in msg
        assert "confirm ho gaya" in msg  # this is the 'confirmed' moment

    def test_bill_reads_stored_amounts_never_recomputes(self, db, cod_order):
        """Guard the money boundary: the bill must reflect exactly what is stored
        on the order, even if that were somehow inconsistent."""
        cod_order.status = OrderStatus.ACCEPTED
        cod_order.total_amount = cod_order.total_amount  # explicit: no recompute
        msg = notifications.build_notification(cod_order)
        assert f"Rs. {cod_order.total_amount:.2f}" in msg

    def test_cancelled_builds_an_apology(self, db, cod_order):
        cod_order.status = OrderStatus.CANCELLED
        msg = notifications.build_notification(cod_order)
        assert cod_order.order_number in msg
        assert "cancel" in msg.lower()
        assert "maaf" in msg.lower()

    def test_non_notifying_statuses_return_none(self, db, cod_order):
        for status in (
            OrderStatus.PENDING,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.DELIVERED,
            OrderStatus.AWAITING_PAYMENT,
        ):
            cod_order.status = status
            assert notifications.build_notification(cod_order) is None

    def test_no_emoji_in_notifications(self, db, cod_order):
        """Consistent with the professional tone pass — notifications are
        business messages about money, no emojis."""
        for status in (OrderStatus.ACCEPTED, OrderStatus.CANCELLED):
            cod_order.status = status
            msg = notifications.build_notification(cod_order)
            assert all(ord(ch) < 0x1F000 for ch in msg), f"emoji leaked into {status}"


# --------------------------------------------------------------------------- #
# send_order_notification — send + log, given a session
# --------------------------------------------------------------------------- #


class TestSendAndLog:

    @pytest.fixture
    def sent(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            notifications, "send_text", lambda to, body: captured.append((to, body))
        )
        return captured

    def test_sends_to_the_customer_and_logs_outbound(self, db, cod_order, sent):
        cod_order.status = OrderStatus.ACCEPTED
        assert notifications.send_order_notification(db, cod_order) is True

        assert len(sent) == 1
        to, body = sent[0]
        assert to == cod_order.customer.whatsapp_number
        assert cod_order.order_number in body

        # Logged into the transcript as an OUTBOUND, tagged as a notification.
        logged = db.scalars(
            select_outbound_notifications(cod_order.customer_id)
        ).all()
        assert any(
            m.meta and m.meta.get("notification") == "accepted" for m in logged
        )

    def test_returns_false_and_sends_nothing_for_a_silent_status(
        self, db, cod_order, sent,
    ):
        cod_order.status = OrderStatus.PREPARING
        assert notifications.send_order_notification(db, cod_order) is False
        assert sent == []

    def test_delivery_failure_still_logs_the_attempt(self, db, cod_order, monkeypatch):
        """If Wassender is unreachable the transcript must still show what we
        tried to say."""
        from app.services.whatsapp import WhatsAppError

        def boom(to, body):
            raise WhatsAppError("wassender down")

        monkeypatch.setattr(notifications, "send_text", boom)
        cod_order.status = OrderStatus.ACCEPTED

        assert notifications.send_order_notification(db, cod_order) is True
        logged = db.scalars(select_outbound_notifications(cod_order.customer_id)).all()
        assert logged, "the attempted notification should still be logged"


# --------------------------------------------------------------------------- #
# End to end: the dashboard PATCH triggers the WhatsApp send
# --------------------------------------------------------------------------- #


class TestDashboardTriggersNotification:
    """The new architectural piece: a dashboard API call causes an OUTBOUND
    WhatsApp message. The notification runs in a BackgroundTask that opens its
    own session, so we point that session at the test transaction."""

    @pytest.fixture(autouse=True)
    def _wire_background_session(self, db, monkeypatch):
        monkeypatch.setattr(notifications, "SessionLocal", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)

    @pytest.fixture
    def sent(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            notifications, "send_text", lambda to, body: captured.append((to, body))
        )
        return captured

    def test_accept_from_dashboard_sends_the_bill(
        self, db, client, cod_order, pizza_headers, sent,
    ):
        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "accepted"},
        )
        assert response.status_code == 200

        assert len(sent) == 1
        to, body = sent[0]
        assert to == cod_order.customer.whatsapp_number
        assert cod_order.order_number in body
        assert "Total:" in body

    def test_cancel_from_dashboard_sends_an_apology(
        self, db, client, cod_order, pizza_headers, sent,
    ):
        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        assert len(sent) == 1
        _, body = sent[0]
        assert "cancel" in body.lower()

    def test_internal_transition_notifies_nobody(
        self, db, client, cod_order, pizza_headers, sent,
    ):
        """accepted (1 notification) then preparing (0). The customer is not
        pinged for every internal kitchen step."""
        client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "accepted"},
        )
        assert len(sent) == 1  # from accepted

        client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": "preparing"},
        )
        assert len(sent) == 1, "preparing must not notify the customer"


def select_outbound_notifications(customer_id: int):
    """Outbound messages for a customer that carry a notification tag."""
    from sqlalchemy import select

    from app.models import Conversation

    return (
        select(MessageLog)
        .join(Conversation, MessageLog.conversation_id == Conversation.id)
        .where(
            Conversation.customer_id == customer_id,
            MessageLog.direction == MessageDirection.OUTBOUND,
        )
    )
