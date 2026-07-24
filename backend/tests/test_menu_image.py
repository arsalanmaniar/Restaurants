"""Phase G (#9) — send a picture of the menu over WhatsApp.

Wassender's send-message endpoint takes a public imageUrl (verified against
their API docs). We store a URL only — no file hosting — at two levels:
Restaurant.menu_image_url (whole menu) and MenuCategory.menu_image_url (per
category). send_menu_image delivers whichever exists, preferring the whole-menu
image, and falls back to "no image" so the model never claims a picture was sent
when there isn't one.
"""

import pytest
from sqlalchemy import select

from app.models import Conversation, MenuCategory, MessageDirection, MessageLog
from app.services import tools


@pytest.fixture
def sent_images(monkeypatch):
    """Capture (to, url, caption) instead of hitting Wassender."""
    captured = []
    monkeypatch.setattr(
        tools, "send_image",
        lambda to, image_url, caption=None: captured.append((to, image_url, caption)),
    )
    return captured


# --------------------------------------------------------------------------- #
# send_image client
# --------------------------------------------------------------------------- #


class TestSendImageClient:

    def test_builds_the_imageurl_payload(self, monkeypatch):
        from app.services import whatsapp

        calls = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"success": True}

        def fake_post(url, json, headers, timeout):
            calls["url"] = url
            calls["json"] = json
            return _Resp()

        monkeypatch.setattr(whatsapp.settings, "wassender_api_key", "test-key")
        monkeypatch.setattr(whatsapp.httpx, "post", fake_post)

        whatsapp.send_image("923001234567", "https://x/menu.jpg", caption="Menu")

        assert calls["json"]["imageUrl"] == "https://x/menu.jpg"
        assert calls["json"]["text"] == "Menu"
        assert calls["json"]["to"] == "+923001234567"

    def test_caption_is_optional(self, monkeypatch):
        from app.services import whatsapp

        captured = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {}

        monkeypatch.setattr(whatsapp.settings, "wassender_api_key", "test-key")
        monkeypatch.setattr(
            whatsapp.httpx, "post",
            lambda url, json, headers, timeout: captured.update(json=json) or _Resp(),
        )

        whatsapp.send_image("923001234567", "https://x/menu.jpg")
        assert "text" not in captured["json"]

    def test_unconfigured_is_a_noop(self, monkeypatch):
        from app.services import whatsapp

        monkeypatch.setattr(whatsapp.settings, "wassender_api_key", "")
        result = whatsapp.send_image("923001234567", "https://x/menu.jpg")
        assert result["sent"] is False


# --------------------------------------------------------------------------- #
# send_menu_image tool
# --------------------------------------------------------------------------- #


class TestSendMenuImageTool:

    def test_sends_the_whole_menu_image_when_present(
        self, db, conversation, pizza, sent_images,
    ):
        pizza.menu_image_url = "https://cdn.example/pizza-menu.jpg"
        db.flush()

        result = tools.send_menu_image(db, conversation, restaurant_id=pizza.id)

        assert result["sent"] == 1
        to, url, caption = sent_images[0]
        assert to == conversation.customer.whatsapp_number
        assert url == "https://cdn.example/pizza-menu.jpg"

    def test_whole_menu_image_beats_category_images(
        self, db, conversation, pizza, sent_images,
    ):
        pizza.menu_image_url = "https://cdn.example/whole.jpg"
        for cat in pizza.categories:
            cat.menu_image_url = "https://cdn.example/cat.jpg"
        db.flush()

        tools.send_menu_image(db, conversation, restaurant_id=pizza.id)
        # Exactly one image — the whole-menu one, not one per category.
        assert len(sent_images) == 1
        assert sent_images[0][1] == "https://cdn.example/whole.jpg"

    def test_falls_back_to_category_images(self, db, conversation, pizza, sent_images):
        pizza.menu_image_url = None
        cats = sorted(pizza.categories, key=lambda c: (c.sort_order, c.id))
        for i, cat in enumerate(cats):
            cat.menu_image_url = f"https://cdn.example/cat-{i}.jpg"
        db.flush()

        result = tools.send_menu_image(db, conversation, restaurant_id=pizza.id)
        assert result["sent"] == len(cats)
        assert len(sent_images) == len(cats)

    def test_no_image_available_returns_no_image(self, db, conversation, pizza, sent_images):
        pizza.menu_image_url = None
        for cat in pizza.categories:
            cat.menu_image_url = None
        db.flush()

        result = tools.send_menu_image(db, conversation, restaurant_id=pizza.id)
        assert result.get("no_image") is True
        assert sent_images == []

    def test_unknown_restaurant_errors(self, db, conversation, sent_images):
        result = tools.send_menu_image(db, conversation, restaurant_name="Nowhere XYZ")
        assert result.get("error") == "unknown_restaurant"
        assert sent_images == []

    def test_sent_image_is_logged_to_the_transcript(
        self, db, conversation, pizza, sent_images,
    ):
        pizza.menu_image_url = "https://cdn.example/pizza-menu.jpg"
        db.flush()
        tools.send_menu_image(db, conversation, restaurant_id=pizza.id)

        logged = db.scalars(
            select(MessageLog)
            .join(Conversation, MessageLog.conversation_id == Conversation.id)
            .where(
                Conversation.customer_id == conversation.customer_id,
                MessageLog.direction == MessageDirection.OUTBOUND,
            )
        ).all()
        assert any(
            m.meta and m.meta.get("menu_image_url") == "https://cdn.example/pizza-menu.jpg"
            for m in logged
        )

    def test_delivery_failure_reports_send_failed(self, db, conversation, pizza, monkeypatch):
        from app.services.whatsapp import WhatsAppError

        pizza.menu_image_url = "https://cdn.example/pizza-menu.jpg"
        db.flush()

        def boom(to, image_url, caption=None):
            raise WhatsAppError("wassender down")

        monkeypatch.setattr(tools, "send_image", boom)
        result = tools.send_menu_image(db, conversation, restaurant_id=pizza.id)
        assert result.get("error") == "send_failed"


# --------------------------------------------------------------------------- #
# get_menu availability flag
# --------------------------------------------------------------------------- #


class TestGetMenuFlag:

    def test_flag_false_when_no_images(self, db, conversation, pizza):
        pizza.menu_image_url = None
        for cat in pizza.categories:
            cat.menu_image_url = None
        db.flush()
        result = tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert result["menu_image_available"] is False

    def test_flag_true_with_whole_menu_image(self, db, conversation, pizza):
        pizza.menu_image_url = "https://cdn.example/menu.jpg"
        db.flush()
        result = tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert result["menu_image_available"] is True

    def test_flag_true_with_a_category_image(self, db, conversation, pizza):
        pizza.menu_image_url = None
        pizza.categories[0].menu_image_url = "https://cdn.example/cat.jpg"
        db.flush()
        result = tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert result["menu_image_available"] is True


# --------------------------------------------------------------------------- #
# Dashboard: set the images via the API
# --------------------------------------------------------------------------- #


class TestDashboardSetsImages:

    def test_restaurant_sets_its_whole_menu_image(self, db, client, pizza, pizza_headers):
        response = client.patch(
            "/restaurant/me",
            headers=pizza_headers,
            json={"menu_image_url": "https://cdn.example/pizza-menu.jpg"},
        )
        assert response.status_code == 200
        assert response.json()["menu_image_url"] == "https://cdn.example/pizza-menu.jpg"

    def test_category_image_can_be_patched_onto_an_existing_category(
        self, db, client, pizza, pizza_headers,
    ):
        category = db.scalar(
            select(MenuCategory).where(MenuCategory.restaurant_id == pizza.id)
        )
        response = client.patch(
            f"/restaurant/categories/{category.id}",
            headers=pizza_headers,
            json={"menu_image_url": "https://cdn.example/cat.jpg"},
        )
        assert response.status_code == 200
        assert response.json()["menu_image_url"] == "https://cdn.example/cat.jpg"

    def test_cannot_patch_another_restaurants_category(
        self, db, client, biryani, pizza_headers,
    ):
        other = db.scalar(
            select(MenuCategory).where(MenuCategory.restaurant_id == biryani.id)
        )
        response = client.patch(
            f"/restaurant/categories/{other.id}",
            headers=pizza_headers,
            json={"menu_image_url": "https://cdn.example/x.jpg"},
        )
        assert response.status_code == 404
