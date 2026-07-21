"""Contextual upsell — services/upsell.py + the suggest_addons agent tool.

Cases pinned here mirror the four verification points from the Phase 4 plan:

  1. Customer adds a main item → tool suggests a REAL add-on from a different
     category (data-driven, not a hardcoded fries/drinks list).
  2. "At most one upsell per turn" is structurally guaranteed by the tool
     shape (`suggestion_type` is a single tag, and only one payload — promotion
     OR addon OR nothing — is returned).
  3. If an active promotion exists for the cart's restaurant, it beats a
     generic add-on suggestion.
  4. The tool never returns items from another restaurant, nor items already
     in the cart, nor items whose category is already in the cart, nor
     unavailable items — the guarantees that let the surrounding flow stay
     intact.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import CouponDiscountType, MenuItem, Promotion
from app.services import tools
from app.services import upsell as upsell_service

TODAY = date.today()


def _make_promo(
    db, restaurant, *,
    title: str = "Weekend deal",
    discount_type: CouponDiscountType = CouponDiscountType.PERCENTAGE,
    discount_value: Decimal = Decimal("20.00"),
    valid_from: date | None = None,
    valid_to: date | None = None,
    is_active: bool = True,
) -> Promotion:
    """Same-shape helper as test_promotions.py::_make_promo, duplicated here
    because coupling two test modules through a shared helper is worse than
    a few lines of duplication."""
    promo = Promotion(
        restaurant_id=restaurant.id,
        title=title,
        discount_type=discount_type,
        discount_value=discount_value,
        valid_from=valid_from or TODAY,
        valid_to=valid_to or TODAY + timedelta(days=7),
        is_active=is_active,
        applicable_menu_item_ids=[],
    )
    db.add(promo)
    db.flush()
    return promo


# --------------------------------------------------------------------------- #
# Pure service — pick_for_cart
# --------------------------------------------------------------------------- #


class TestPickForCart:
    """The core algorithm without the tool wrapper. Kept separate so bugs in
    the picking logic surface as targeted service failures instead of only
    showing up as vague tool-shape assertion errors."""

    def test_empty_cart_returns_none(self, db, pizza):
        pick = upsell_service.pick_for_cart(db, pizza.id, cart_item_ids=[])
        assert pick.kind == "none"
        assert pick.addon is None
        assert pick.promotion is None

    def test_pizza_in_cart_picks_cheapest_non_pizza_item(self, db, pizza):
        # Pizza Junction menu: Pizza (3 items), Sides (Loaded Fries Rs. 380,
        # Garlic Bread Rs. 300), Drinks (Chocolate Shake Rs. 350). Cheapest
        # non-pizza item is Garlic Bread @ Rs. 300.
        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[chicken_pizza.id],
        )
        assert pick.kind == "addon"
        assert pick.addon.name == "Garlic Bread"
        assert pick.addon.category.name == "Sides"

    def test_never_picks_something_from_a_category_already_in_cart(self, db, pizza):
        """If the cart already has a Sides item (Loaded Fries), the tool must
        move to a different category (Drinks) instead of picking the next-
        cheapest Side."""
        loaded_fries = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name == "Loaded Fries",
            )
        )
        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[loaded_fries.id],
        )
        assert pick.kind == "addon"
        # Not another Side — must be from a different category
        assert pick.addon.category.name != "Sides"
        # Only Drinks left in Pizza Junction — Chocolate Shake Rs. 350
        # (cheaper than any Pizza), OR the cheapest Pizza (Chicken Tikka
        # @ Rs. 1150). The shake wins on price.
        assert pick.addon.name == "Chocolate Shake"

    def test_never_picks_an_item_already_in_the_cart(self, db, pizza):
        loaded_fries = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name == "Loaded Fries",
            )
        )
        garlic_bread = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name == "Garlic Bread",
            )
        )
        # Both Sides in cart — cross-category rule sends us to Drinks or Pizza
        pick = upsell_service.pick_for_cart(
            db, pizza.id,
            cart_item_ids=[loaded_fries.id, garlic_bread.id],
        )
        assert pick.kind == "addon"
        # Never one of the two Sides that are already in the cart
        assert pick.addon.id not in (loaded_fries.id, garlic_bread.id)

    def test_unavailable_items_are_ignored(self, db, pizza):
        """Marking all Sides unavailable must push the pick to Drinks/Pizza —
        never surface something the kitchen can't make."""
        db.execute(
            MenuItem.__table__.update()
            .where(MenuItem.restaurant_id == pizza.id)
            .where(MenuItem.name.in_(("Loaded Fries", "Garlic Bread")))
            .values(is_available=False)
        )
        db.flush()

        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[chicken_pizza.id],
        )
        # Chocolate Shake is the only remaining non-pizza item
        assert pick.addon.name == "Chocolate Shake"

    def test_active_promotion_beats_addon(self, db, pizza):
        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        _make_promo(db, pizza, title="Weekend Pizza Deal")

        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[chicken_pizza.id],
        )
        assert pick.kind == "promotion"
        assert pick.addon is None
        assert pick.promotion.title == "Weekend Pizza Deal"

    def test_expired_promotion_falls_through_to_addon(self, db, pizza):
        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        _make_promo(
            db, pizza, title="Old deal",
            valid_from=TODAY - timedelta(days=10),
            valid_to=TODAY - timedelta(days=1),
        )
        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[chicken_pizza.id],
        )
        assert pick.kind == "addon"
        assert pick.addon.name == "Garlic Bread"

    def test_cross_restaurant_isolation(self, db, pizza, biryani):
        """A promo for biryani must never surface when the cart is at Pizza
        Junction — the algo only considers promos for the restaurant argument."""
        _make_promo(db, biryani, title="Biryani deal")

        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        pick = upsell_service.pick_for_cart(
            db, pizza.id, cart_item_ids=[chicken_pizza.id],
        )
        assert pick.kind == "addon"  # not "promotion" (biryani's promo is invisible)


# --------------------------------------------------------------------------- #
# Agent tool — suggest_addons wrapper
# --------------------------------------------------------------------------- #


class TestSuggestAddonsTool:
    """The JSON shape the model sees. Covers the four Phase 4 verification
    cases end-to-end."""

    def test_case1_cart_with_main_gets_real_addon(
        self, db, cart_with_pizza,
    ):
        """CASE 1 — customer adds a main → suggestion is a real add-on from a
        different category, all data-driven."""
        result = tools.suggest_addons(db, cart_with_pizza)
        assert result["suggestion_type"] == "addon"
        assert result["addon"]["name"] == "Garlic Bread"
        # Verifies real menu data (not hardcoded): price + category came from
        # what seed.py loaded, and the id is a real menu item id.
        assert result["addon"]["price"] == "300.00"
        assert result["addon"]["category"] == "Sides"
        assert "id" in result["addon"]

    def test_case2_at_most_one_field_returned(
        self, db, cart_with_pizza,
    ):
        """CASE 2 — the tool ALWAYS returns at most one thing to mention. The
        `suggestion_type` tag is single-valued, and only the matching payload
        field is populated. This is the structural half of the 'at most one
        upsell per turn' guarantee."""
        result = tools.suggest_addons(db, cart_with_pizza)
        # A single tag
        assert result["suggestion_type"] in ("promotion", "addon", "none")
        # And at most one of these keys present
        has_promo = "promotion" in result
        has_addon = "addon" in result
        assert (has_promo, has_addon) in {(True, False), (False, True), (False, False)}

    def test_case3_active_promo_surfaces_instead_of_addon(
        self, db, cart_with_pizza, pizza,
    ):
        """CASE 3 — an active promo for the cart's restaurant beats the addon
        suggestion. The response's suggestion_type is 'promotion' and the
        addon key is absent."""
        _make_promo(
            db, pizza,
            title="Weekend Pizza Bonanza — 25% off",
            discount_type=CouponDiscountType.PERCENTAGE,
            discount_value=Decimal("25"),
        )
        result = tools.suggest_addons(db, cart_with_pizza)
        assert result["suggestion_type"] == "promotion"
        assert result["promotion"]["title"] == "Weekend Pizza Bonanza — 25% off"
        assert result["promotion"]["discount"] == "25% off"
        assert "addon" not in result  # promo replaces addon, never both

    def test_case4_no_regression_when_cart_is_empty(self, db, conversation):
        """CASE 4 — the tool is a NO-OP on an empty cart. add_to_cart /
        place_order behaviour is not affected because this tool never mutates
        state; it only reads."""
        result = tools.suggest_addons(db, conversation)
        assert result["suggestion_type"] == "none"
        assert "addon" not in result
        assert "promotion" not in result

    def test_no_regression_add_to_cart_still_works_after_suggest(
        self, db, cart_with_pizza, pizza,
    ):
        """Calling suggest_addons must not disturb the cart state — a
        subsequent add_to_cart on the same conversation still succeeds and
        the cart reflects both items."""
        loaded_fries = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name == "Loaded Fries",
            )
        )
        # 1. suggest_addons (reads, does not mutate)
        first = tools.suggest_addons(db, cart_with_pizza)
        assert first["suggestion_type"] == "addon"

        # 2. Add the suggested item — the same code path a real turn would take
        add_result = tools.add_to_cart(
            db, cart_with_pizza, menu_item_id=loaded_fries.id, quantity=1,
        )
        assert "error" not in add_result

        cart_items = cart_with_pizza.cart["items"]
        names = [line["name"] for line in cart_items]
        assert "Chicken Tikka Pizza (Medium)" in names
        assert "Loaded Fries" in names

    def test_deactivated_promo_does_not_surface(
        self, db, cart_with_pizza, pizza,
    ):
        _make_promo(db, pizza, title="Turned off", is_active=False)
        result = tools.suggest_addons(db, cart_with_pizza)
        assert result["suggestion_type"] == "addon"  # falls through to addon

    def test_promotion_response_includes_valid_to(
        self, db, cart_with_pizza, pizza,
    ):
        """Model needs valid_to to be able to answer 'how long is this on?' —
        it's part of the promotion payload contract."""
        _make_promo(
            db, pizza, title="Two-day flash",
            valid_from=TODAY, valid_to=TODAY + timedelta(days=1),
        )
        result = tools.suggest_addons(db, cart_with_pizza)
        assert result["suggestion_type"] == "promotion"
        assert result["promotion"]["valid_to"] == (TODAY + timedelta(days=1)).isoformat()

    def test_restaurant_with_only_one_category_returns_none(
        self, db, conversation, pizza,
    ):
        """If every non-pizza category is emptied, the tool has no cross-
        category item to suggest and returns 'none' instead of a same-
        category or unrelated pick."""
        # Kill everything except one specific Pizza item
        chicken_pizza = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        db.execute(
            MenuItem.__table__.update()
            .where(MenuItem.restaurant_id == pizza.id)
            .where(MenuItem.id != chicken_pizza.id)
            .values(is_available=False)
        )
        db.flush()

        # Ground the cart through get_menu (the grounding guard needs it)
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=chicken_pizza.id, quantity=1)
        db.flush()

        result = tools.suggest_addons(db, conversation)
        assert result["suggestion_type"] == "none"

    def test_place_order_after_seen_outbound_does_not_nameerror(
        self, db, cart_with_pizza, pizza,
    ):
        """Regression guard for the latent NameError in _has_any_outbound /
        _model_has_asked_payment (both reference MessageDirection which was
        not imported before Phase 4). Every real conversation has OUTBOUNDs;
        every place_order call with multiple payment methods on would have
        NameError-ed on the first run. Stumbled onto during the Phase 4
        manual trace; fixed by adding the missing import."""
        from app.models import MessageDirection
        from app.services import conversations as convo_svc

        # Give the customer a default address so place_order doesn't miss it.
        from app.models import CustomerAddress
        db.add(CustomerAddress(
            customer_id=cart_with_pizza.customer_id,
            address_text="House 1, DHA Lahore",
            is_default=True,
        ))
        # Seed the payment-method question that unlocks the anti-silent-COD
        # guard — this is what real customer flows always look like.
        convo_svc.log_message(
            db, cart_with_pizza, MessageDirection.OUTBOUND,
            "Payment kis se karna hai — cod, jazzcash, ya easypaisa?",
        )
        db.flush()

        result = tools.place_order(db, cart_with_pizza, payment_method="cod")
        # Whatever the outcome (order placed / silent-cod refused), it must
        # NOT be a NameError propagated back from _has_any_outbound.
        assert "NameError" not in str(result)
        assert "MessageDirection" not in str(result)

    def test_addon_id_is_a_real_menu_item_id(
        self, db, cart_with_pizza,
    ):
        """Grounding: the id returned in the suggestion is a real MenuItem id
        the model could then pass to add_to_cart. Guards against ever
        returning a synthetic or offset id by accident."""
        result = tools.suggest_addons(db, cart_with_pizza)
        assert result["suggestion_type"] == "addon"
        item = db.get(MenuItem, result["addon"]["id"])
        assert item is not None
        assert item.name == result["addon"]["name"]
        assert item.is_available is True
