"""The UltraMsg webhook.

This endpoint is exposed to the public internet and every message it accepts costs money
(a Groq call) and writes to the database. The guards here are what stop it being abused,
and what stop the bot talking to itself.
"""

import pytest

from app.core.config import settings

WEBHOOK = "/webhooks/ultramsg"

INBOUND = {
    "event_type": "message_received",
    "data": {
        "id": "true_923001234567@c.us_ABC",
        "from": "923001234567@c.us",
        "body": "hello",
        "type": "chat",
        "fromMe": False,
    },
}


@pytest.fixture(autouse=True)
def webhook_session(db, monkeypatch):
    """The webhook processes messages in a background task that opens its OWN session via
    SessionLocal. Left alone it would escape the test's transaction and write real rows,
    so point it at the test session (and stop it closing that session out from under us).
    """
    monkeypatch.setattr("app.api.webhooks.SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """Never call Groq from a webhook test."""
    calls = []
    monkeypatch.setattr(
        "app.api.webhooks.handle_incoming_message",
        lambda db, conversation, body: calls.append(body),
    )
    return calls


@pytest.fixture(autouse=True)
def _hermetic_webhook_secret(monkeypatch):
    """Baseline: clear the webhook secret so posts without ?secret= are accepted.
    Tests that specifically exercise the secret gate re-set it via monkeypatch."""
    monkeypatch.setattr(settings, "ultramsg_webhook_secret", "")


class TestSecret:
    def test_wrong_secret_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ultramsg_webhook_secret", "s3cret")
        response = client.post(f"{WEBHOOK}?secret=wrong", json=INBOUND)
        assert response.status_code == 403

    def test_correct_secret_is_accepted(self, client, monkeypatch):
        monkeypatch.setattr(settings, "ultramsg_webhook_secret", "s3cret")
        response = client.post(f"{WEBHOOK}?secret=s3cret", json=INBOUND)
        assert response.status_code == 200

    def test_missing_secret_in_production_refuses_to_serve(self, client, monkeypatch):
        """An unset secret used to mean 'let everyone in' — a public endpoint that spends
        money on Groq calls and writes to our database."""
        monkeypatch.setattr(settings, "ultramsg_webhook_secret", "")
        monkeypatch.setattr(settings, "debug", False)

        response = client.post(WEBHOOK, json=INBOUND)
        assert response.status_code == 503


class TestMessageFiltering:
    def test_own_messages_are_ignored(self, client, no_ai):
        """UltraMsg echoes our OWN outbound messages back. Replying to them would make
        the bot talk to itself forever."""
        payload = {**INBOUND, "data": {**INBOUND["data"], "fromMe": True}}
        response = client.post(WEBHOOK, json=payload)

        assert response.json()["reason"] == "own message"
        assert no_ai == []

    def test_non_message_events_are_ignored(self, client, no_ai):
        response = client.post(WEBHOOK, json={"event_type": "message_ack", "data": {}})
        assert response.json()["status"] == "ignored"
        assert no_ai == []

    def test_non_text_messages_get_a_polite_reply(self, client, no_ai, monkeypatch):
        sent = []
        monkeypatch.setattr("app.api.webhooks.send_text", lambda to, body: sent.append(body))

        payload = {**INBOUND, "data": {**INBOUND["data"], "type": "image"}}
        response = client.post(WEBHOOK, json=payload)

        assert "unsupported type" in response.json()["reason"]
        assert no_ai == []

    def test_empty_body_is_ignored(self, client, no_ai):
        payload = {**INBOUND, "data": {**INBOUND["data"], "body": "   "}}
        assert client.post(WEBHOOK, json=payload).json()["status"] == "ignored"
        assert no_ai == []

    def test_malformed_json_does_not_trigger_a_retry_storm(self, client):
        """A 4xx would make UltraMsg retry a payload that will never parse."""
        response = client.post(WEBHOOK, content=b"not json{{{")
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"


class TestMessageProcessing:
    def test_a_normal_message_reaches_the_ai(self, client, no_ai):
        response = client.post(WEBHOOK, json=INBOUND)
        assert response.json()["status"] == "accepted"
        assert no_ai == ["hello"]

    def test_oversized_message_is_truncated(self, client, no_ai):
        """Every character is fed to Groq — an oversized paste is a direct hit to the
        token bill."""
        from app.api.webhooks import MAX_INBOUND_CHARS

        payload = {**INBOUND, "data": {**INBOUND["data"], "body": "x" * 50_000}}
        client.post(WEBHOOK, json=payload)

        assert len(no_ai[0]) == MAX_INBOUND_CHARS

    def test_duplicate_delivery_is_processed_once(self, client, no_ai):
        """UltraMsg retries. Without de-duplication the AI runs twice and can place the
        same order twice."""
        client.post(WEBHOOK, json=INBOUND)
        client.post(WEBHOOK, json=INBOUND)   # same provider message id

        assert no_ai == ["hello"], "the retry must not be processed again"

    def test_blocked_customer_is_dropped_silently(self, db, client, no_ai):
        from app.services import conversations as convo

        customer = convo.get_or_create_customer(db, "923001234567")
        customer.is_blocked = True
        db.flush()

        client.post(WEBHOOK, json=INBOUND)
        assert no_ai == []
