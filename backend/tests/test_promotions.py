"""Time-bound restaurant promotions — model, service, agent tool, and
restaurant CRUD endpoints.

Cases pinned here correspond directly to the four verification points from
the Phase 3 plan:

  1. A promotion created for a restaurant with valid_from/valid_to range
  2. The agent tool surfacing it when relevant (active window)
  3. The agent tool NOT surfacing it once valid_to has passed (expiry)
  4. Restaurant-facing endpoint working (list/create promotions)

Plus the tenant-isolation cases that always matter: a restaurant staff member
must never read or mutate another restaurant's promotions.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import CouponDiscountType, Promotion
from app.services import promotions as promotions_service
from app.services import tools

KARACHI = ZoneInfo("Asia/Karachi")
TODAY = date.today()


def _make_promo(
    db, restaurant, *,
    title: str = "Weekend biryani deal",
    discount_type: CouponDiscountType = CouponDiscountType.PERCENTAGE,
    discount_value: Decimal = Decimal("20.00"),
    valid_from: date | None = None,
    valid_to: date | None = None,
    is_active: bool = True,
    max_discount_amount: Decimal | None = None,
    applicable_menu_item_ids: list[int] | None = None,
) -> Promotion:
    promo = Promotion(
        restaurant_id=restaurant.id,
        title=title,
        discount_type=discount_type,
        discount_value=discount_value,
        valid_from=valid_from or TODAY,
        valid_to=valid_to or TODAY + timedelta(days=7),
        is_active=is_active,
        max_discount_amount=max_discount_amount,
        applicable_menu_item_ids=applicable_menu_item_ids or [],
    )
    db.add(promo)
    db.flush()
    return promo


# --------------------------------------------------------------------------- #
# Service: active-window logic
# --------------------------------------------------------------------------- #


class TestIsActiveAt:
    """The date-window logic that decides whether a promo shows up. All three
    gates (is_active flag, valid_from, valid_to) checked independently."""

    def test_running_promo_is_active_today(self, db, biryani):
        promo = _make_promo(db, biryani)
        assert promotions_service.is_active_at(promo)

    def test_manual_off_switch_wins(self, db, biryani):
        promo = _make_promo(db, biryani, is_active=False)
        assert not promotions_service.is_active_at(promo)

    def test_not_yet_started(self, db, biryani):
        promo = _make_promo(
            db, biryani,
            valid_from=TODAY + timedelta(days=5),
            valid_to=TODAY + timedelta(days=10),
        )
        assert not promotions_service.is_active_at(promo)

    def test_already_ended(self, db, biryani):
        promo = _make_promo(
            db, biryani,
            valid_from=TODAY - timedelta(days=10),
            valid_to=TODAY - timedelta(days=1),
        )
        assert not promotions_service.is_active_at(promo)

    def test_exact_boundary_days_are_inclusive(self, db, biryani):
        """valid_from=today AND valid_to=today must still be active. Off-by-one
        here would silently kill a same-day flash promo."""
        promo = _make_promo(db, biryani, valid_from=TODAY, valid_to=TODAY)
        assert promotions_service.is_active_at(promo)

    def test_at_param_lets_us_simulate_other_days(self, db, biryani):
        """The `at` param is the whole reason expiry logic is testable —
        pretend today is next month, check the promo is gone."""
        promo = _make_promo(
            db, biryani,
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=3),
        )
        future = datetime.now(KARACHI) + timedelta(days=30)
        assert not promotions_service.is_active_at(promo, at=future)


# --------------------------------------------------------------------------- #
# Service: list_active_for_restaurant
# --------------------------------------------------------------------------- #


class TestListActiveForRestaurant:
    def test_returns_running_promotions(self, db, biryani):
        p1 = _make_promo(db, biryani, title="Weekend deal")
        p2 = _make_promo(db, biryani, title="Family combo")
        active = promotions_service.list_active_for_restaurant(db, biryani.id)
        assert {p.id for p in active} == {p1.id, p2.id}

    def test_excludes_expired_and_future_and_deactivated(self, db, biryani):
        _make_promo(db, biryani, title="expired",
                    valid_from=TODAY - timedelta(days=10),
                    valid_to=TODAY - timedelta(days=1))
        _make_promo(db, biryani, title="future",
                    valid_from=TODAY + timedelta(days=5),
                    valid_to=TODAY + timedelta(days=10))
        _make_promo(db, biryani, title="deactivated", is_active=False)
        live = _make_promo(db, biryani, title="live")

        active = promotions_service.list_active_for_restaurant(db, biryani.id)
        assert [p.id for p in active] == [live.id]

    def test_does_not_leak_other_restaurants_promos(self, db, biryani, pizza):
        biryani_promo = _make_promo(db, biryani, title="Biryani deal")
        _make_promo(db, pizza, title="Pizza deal")

        for_biryani = promotions_service.list_active_for_restaurant(db, biryani.id)
        assert [p.id for p in for_biryani] == [biryani_promo.id]


# --------------------------------------------------------------------------- #
# Agent tool: list_active_deals
# --------------------------------------------------------------------------- #


class TestListActiveDealsTool:
    """The tool the AI calls to surface deals. Matches the exact spec the
    user asked for in Phase 3's verification checklist (cases 2 and 3)."""

    def test_surfaces_an_active_promotion(self, db, conversation, biryani):
        _make_promo(
            db, biryani,
            title="Weekend Biryani — 20% off",
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("20"),
        )
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["restaurant"]["name"] == biryani.name
        assert len(result["deals"]) == 1
        deal = result["deals"][0]
        assert deal["title"] == "Weekend Biryani — 20% off"
        assert deal["discount"] == "20% off"
        assert "valid_from" in deal and "valid_to" in deal

    def test_hides_expired_promotion(self, db, conversation, biryani):
        _make_promo(
            db, biryani,
            title="Old promo",
            valid_from=TODAY - timedelta(days=10),
            valid_to=TODAY - timedelta(days=1),
        )
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"] == []

    def test_hides_deactivated_promotion(self, db, conversation, biryani):
        _make_promo(db, biryani, is_active=False)
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"] == []

    def test_lookup_by_name(self, db, conversation, biryani):
        _make_promo(db, biryani, title="Biryani feast")
        result = tools.list_active_deals(
            db, conversation, restaurant_name="Karachi Biryani House"
        )
        assert result["restaurant"]["id"] == biryani.id
        assert result["deals"][0]["title"] == "Biryani feast"

    def test_unknown_restaurant_returns_error(self, db, conversation):
        result = tools.list_active_deals(
            db, conversation, restaurant_name="Nowhere Cafe"
        )
        assert "error" in result

    def test_percentage_discount_string_shape(self, db, conversation, biryani):
        _make_promo(
            db, biryani,
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("15"),
            max_discount_amount=Decimal("500"),
        )
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"][0]["discount"] == "15% off (up to Rs. 500)"

    def test_fixed_discount_string_shape(self, db, conversation, biryani):
        _make_promo(
            db, biryani,
            discount_type=CouponDiscountType.FIXED,
            discount_value=Decimal("300"),
        )
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"][0]["discount"] == "Rs. 300 off"

    def test_applies_to_specific_items_flag(self, db, conversation, biryani):
        _make_promo(db, biryani, applicable_menu_item_ids=[])
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"][0]["applies_to_specific_items"] is False

        db.execute(
            Promotion.__table__.update()
            .values(applicable_menu_item_ids=[1, 2])
        )
        db.flush()
        result = tools.list_active_deals(db, conversation, restaurant_id=biryani.id)
        assert result["deals"][0]["applies_to_specific_items"] is True


# --------------------------------------------------------------------------- #
# Service: validation
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_empty_title_rejected(self):
        with pytest.raises(promotions_service.PromotionError):
            promotions_service.validate_new_promotion(
                title="   ",
                discount_value=Decimal("10"),
                valid_from=TODAY,
                valid_to=TODAY + timedelta(days=1),
            )

    def test_valid_from_after_valid_to_rejected(self):
        with pytest.raises(promotions_service.PromotionError):
            promotions_service.validate_new_promotion(
                title="ok",
                discount_value=Decimal("10"),
                valid_from=TODAY + timedelta(days=5),
                valid_to=TODAY,
            )

    def test_zero_discount_rejected(self):
        with pytest.raises(promotions_service.PromotionError):
            promotions_service.validate_new_promotion(
                title="ok",
                discount_value=Decimal("0"),
                valid_from=TODAY,
                valid_to=TODAY + timedelta(days=1),
            )


# --------------------------------------------------------------------------- #
# Restaurant CRUD endpoints
# --------------------------------------------------------------------------- #


class TestRestaurantPromotionsAPI:
    """Verifies case 4 from the Phase 3 checklist: the endpoint round-trip."""

    def test_create_and_list(self, db, client, biryani_headers, biryani):
        payload = {
            "title": "Family biryani weekend",
            "description": "Get 15% off orders above Rs. 1500 this weekend.",
            "discount_type": "percentage",
            "discount_value": 15,
            "min_order_amount": 1500,
            "valid_from": TODAY.isoformat(),
            "valid_to": (TODAY + timedelta(days=3)).isoformat(),
        }
        response = client.post(
            "/restaurant/promotions", headers=biryani_headers, json=payload,
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["title"] == payload["title"]
        assert created["restaurant_id"] == biryani.id

        list_response = client.get("/restaurant/promotions", headers=biryani_headers)
        assert list_response.status_code == 200
        assert any(p["id"] == created["id"] for p in list_response.json())

    def test_patch_updates_fields(self, db, client, biryani_headers, biryani):
        promo = _make_promo(db, biryani, title="original", is_active=True)
        db.commit()

        response = client.patch(
            f"/restaurant/promotions/{promo.id}",
            headers=biryani_headers,
            json={"title": "renamed", "is_active": False},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "renamed"
        assert response.json()["is_active"] is False

    def test_patch_range_swap_rejected(self, db, client, biryani_headers, biryani):
        """Patching valid_from past the existing valid_to must be rejected
        server-side (CheckConstraint would catch it too, but a friendly 422
        beats a Postgres error)."""
        promo = _make_promo(db, biryani)
        db.commit()

        response = client.patch(
            f"/restaurant/promotions/{promo.id}",
            headers=biryani_headers,
            json={"valid_from": (TODAY + timedelta(days=999)).isoformat()},
        )
        assert response.status_code == 422

    def test_delete(self, db, client, biryani_headers, biryani):
        promo = _make_promo(db, biryani)
        db.commit()

        response = client.delete(
            f"/restaurant/promotions/{promo.id}", headers=biryani_headers,
        )
        assert response.status_code == 204
        assert db.get(Promotion, promo.id) is None

    def test_cannot_see_another_restaurants_promos(
        self, db, client, biryani_headers, pizza,
    ):
        """Tenant boundary — biryani staff must not see or edit Pizza Junction's
        promotions, even by guessing the id."""
        pizza_promo = _make_promo(db, pizza, title="pizza-only")
        db.commit()

        listed = client.get("/restaurant/promotions", headers=biryani_headers).json()
        assert all(p["id"] != pizza_promo.id for p in listed)

        assert client.patch(
            f"/restaurant/promotions/{pizza_promo.id}",
            headers=biryani_headers,
            json={"title": "hijacked"},
        ).status_code == 404

        assert client.delete(
            f"/restaurant/promotions/{pizza_promo.id}", headers=biryani_headers,
        ).status_code == 404

    def test_invalid_percentage_over_100_rejected(self, db, client, biryani_headers):
        response = client.post(
            "/restaurant/promotions",
            headers=biryani_headers,
            json={
                "title": "impossible",
                "discount_type": "percentage",
                "discount_value": 150,
                "valid_from": TODAY.isoformat(),
                "valid_to": (TODAY + timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 422
