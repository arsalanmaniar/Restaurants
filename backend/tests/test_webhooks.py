"""The Wassender webhook.

This endpoint is exposed to the public internet and every message it accepts costs money
(a Groq call) and writes to the database. The guards here are what stop it being abused,
and what stop the bot talking to itself.
"""

import copy

import pytest

from app.core.config import settings

WEBHOOK = "/webhooks/wassender"

# Real Wassender shape (verified against a live delivery): message text and id
# live under `data.messages.*`, and sender/fromMe under `data.messages.key.*`.
INBOUND = {
    "event": "messages.received",
    "data": {
        "messages": {
            "id": "3AFE6F540BFF485F65C9",
            "messageBody": "hello",
            "key": {
                "id": "3AFE6F540BFF485F65C9",
                "cleanedSenderPn": "923001234567",
                "fromMe": False,
            },
        },
    },
}


def _with(**overrides) -> dict:
    """Deep-copy INBOUND and apply `data.<key>=value` or `data.messages.key.<key>=value`
    style overrides via dotted-path keys, so tests stay readable."""
    payload = copy.deepcopy(INBOUND)
    for dotted, value in overrides.items():
        node = payload
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return payload


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
    monkeypatch.setattr(settings, "wassender_webhook_secret", "")


class TestSecret:
    def test_wrong_secret_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "wassender_webhook_secret", "s3cret")
        response = client.post(f"{WEBHOOK}?secret=wrong", json=INBOUND)
        assert response.status_code == 403

    def test_correct_secret_is_accepted(self, client, monkeypatch):
        monkeypatch.setattr(settings, "wassender_webhook_secret", "s3cret")
        response = client.post(f"{WEBHOOK}?secret=s3cret", json=INBOUND)
        assert response.status_code == 200

    def test_missing_secret_in_production_refuses_to_serve(self, client, monkeypatch):
        """An unset secret used to mean 'let everyone in' — a public endpoint that spends
        money on Groq calls and writes to our database."""
        monkeypatch.setattr(settings, "wassender_webhook_secret", "")
        monkeypatch.setattr(settings, "debug", False)

        response = client.post(WEBHOOK, json=INBOUND)
        assert response.status_code == 503


class TestMessageFiltering:
    def test_own_messages_are_ignored(self, client, no_ai):
        """Wassender echoes our OWN outbound messages back. Replying to them would make
        the bot talk to itself forever."""
        payload = _with(**{"data.messages.key.fromMe": True})
        response = client.post(WEBHOOK, json=payload)

        assert response.json()["reason"] == "own message"
        assert no_ai == []

    def test_non_message_events_are_ignored(self, client, no_ai):
        response = client.post(WEBHOOK, json={"event": "messages.ack"})
        assert response.json()["status"] == "ignored"
        assert no_ai == []

    def test_non_text_messages_are_ignored_silently(self, client, no_ai, monkeypatch):
        """Wassender's real payload shape doesn't expose a message-type field we've
        confirmed, so we no longer send a "text only" reply. Media messages simply
        arrive without a messageBody and drop through the empty-body guard."""
        sent = []
        monkeypatch.setattr("app.api.webhooks.send_text", lambda to, body: sent.append(body))

        payload = _with()  # inherits INBOUND
        payload["data"]["messages"].pop("messageBody", None)  # media-shaped: no body

        response = client.post(WEBHOOK, json=payload)

        assert response.json()["status"] == "ignored"
        assert no_ai == []
        assert sent == [], "no outbound reply should be sent for media messages"

    def test_empty_body_is_ignored(self, client, no_ai):
        payload = _with(**{"data.messages.messageBody": "   "})
        assert client.post(WEBHOOK, json=payload).json()["status"] == "ignored"
        assert no_ai == []

    def test_malformed_json_does_not_trigger_a_retry_storm(self, client):
        """A 4xx would make Wassender retry a payload that will never parse."""
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

        payload = _with(**{"data.messages.messageBody": "x" * 50_000})
        client.post(WEBHOOK, json=payload)

        assert len(no_ai[0]) == MAX_INBOUND_CHARS

    def test_duplicate_delivery_is_processed_once(self, client, no_ai):
        """Wassender retries. Without de-duplication the AI runs twice and can place the
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


class TestLocationPin:
    """Phase D — a shared WhatsApp location pin (Baileys `locationMessage` shape).
    FIELD PATH is inferred, not verified against a real Wassender location payload;
    see api/webhooks.py::_extract_location."""

    def _location_payload(self, lat: float, lng: float) -> dict:
        payload = _with()
        payload["data"]["messages"].pop("messageBody", None)  # a pin has no text body
        payload["data"]["messages"]["message"] = {
            "locationMessage": {"degreesLatitude": lat, "degreesLongitude": lng}
        }
        return payload

    def test_extract_location_parses_the_baileys_shape(self):
        from app.api.webhooks import _extract_location

        payload = self._location_payload(24.8607, 67.0011)
        assert _extract_location(payload) == (24.8607, 67.0011)

    def test_extract_location_none_for_a_text_message(self):
        from app.api.webhooks import _extract_location

        assert _extract_location(INBOUND) is None

    def test_pin_becomes_text_the_ai_can_read(self, client, no_ai):
        client.post(WEBHOOK, json=self._location_payload(24.8607, 67.0011))
        assert len(no_ai) == 1
        assert "map pin" in no_ai[0].lower()
        assert "24.8607,67.0011" in no_ai[0]

    def test_pin_coordinates_are_stashed_in_conversation_context(self, db, client, no_ai):
        from app.services import conversations as convo

        client.post(WEBHOOK, json=self._location_payload(24.8607, 67.0011))

        customer = convo.get_or_create_customer(db, "923001234567")
        conversation = convo.get_or_create_conversation(db, customer)
        assert conversation.context["delivery_location"] == {"lat": 24.8607, "lng": 67.0011}

    def test_media_without_a_location_is_still_ignored(self, client, no_ai):
        """Regression guard: the new location branch must not resurrect other
        media types. A message with a non-location `message` object and no body
        still drops, exactly as before."""
        payload = _with()
        payload["data"]["messages"].pop("messageBody", None)
        payload["data"]["messages"]["message"] = {"imageMessage": {"url": "x"}}

        assert client.post(WEBHOOK, json=payload).json()["status"] == "ignored"
        assert no_ai == []
