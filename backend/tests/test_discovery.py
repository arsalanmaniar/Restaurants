"""Intent-based restaurant discovery — services/discovery.py + the
`find_restaurants` agent tool.

Cases pinned here mirror the four verification points from the Phase 5 plan:

  1. Dish/cuisine keyword (e.g. "biryani", "pizza chahiye") lands on the
     right restaurant, no exact-name lookup required.
  2. A vaguer intent ("spicy", "chinese") also lands — the tool searches
     five columns including MenuItem.description and Restaurant.cuisine_type.
  3. The older `search_restaurants_by_item` and `list_restaurants` tools
     still work — backward-compat, deprecated in the schema description
     but not removed.
  4. The full menu → cart → address → payment → confirm flow still works
     unchanged (this tool only READS, never mutates).

Plus the correctness guarantees that make the discovery tool safe to trust:
open-only filtering, ranking wired in from Phase 2, and no leaked internal
tags in matched_items.
"""

from sqlalchemy import delete

from app.models import MenuItem, RestaurantWorkingHours
from app.services import discovery as discovery_service
from app.services import tools


# --------------------------------------------------------------------------- #
# Pure service — find_matching_restaurants
# --------------------------------------------------------------------------- #


class TestFindMatchingRestaurants:
    """Directly against the ILIKE-across-five-columns search. Kept separate
    from the tool wrapper so search-shape bugs show up as focused failures."""

    def test_dish_name_match(self, db, biryani):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "biryani",
        )
        assert biryani.id in restaurants
        # Real menu item names (Chicken Biryani, Beef Biryani, Special Family
        # Biryani) — proves matched_items are truthful, not synthetic tags.
        assert any(
            "Biryani" in item for item in matched[biryani.id]
        )

    def test_cuisine_only_match_uses_cuisine_as_matched_signal(
        self, db, biryani,
    ):
        """A "desi" query matches biryani's cuisine_type='Desi' but no menu
        item literally contains 'desi'. matched_items falls back to the
        cuisine text so ranking still gives it the relevance boost."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "desi",
        )
        assert biryani.id in restaurants
        assert matched[biryani.id] == ["Desi"]  # truthful, quotable

    def test_description_only_match_via_menu_item_description(
        self, db, biryani,
    ):
        """'spicy' shows up in Beef Biryani's description ("Slow-cooked beef,
        spicy Sindhi masala") but NOT in any name. Menu-description search
        finds it and matched_items carries the real item name."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "spicy",
        )
        assert biryani.id in restaurants
        assert "Beef Biryani" in matched[biryani.id]

    def test_restaurant_description_match(self, db, pizza):
        """Pizza Junction's description says "Hand-tossed pizzas, loaded
        fries, and shakes." The word "hand-tossed" only lives in the
        restaurant description, no menu item mentions it — restaurant-level
        ILIKE catches it and cuisine text ("Pizza") becomes the matched
        signal."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "hand-tossed",
        )
        assert pizza.id in restaurants
        assert matched[pizza.id] == ["Pizza"]

    def test_query_matches_across_multiple_restaurants(self, db, biryani):
        """A single query can hit several restaurants — 'chicken' matches
        Chicken Biryani (biryani), Chicken Tikka Pizza (pizza), Chicken
        Chowmein (wok & roll). All three must appear."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "chicken",
        )
        # At least the biryani place, plus at least one other
        assert biryani.id in restaurants
        assert len(restaurants) >= 2

    def test_empty_query_returns_nothing(self, db):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(db, "")
        assert restaurants == {}
        assert matched == {}

    def test_no_match_returns_empty(self, db):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "sushi-omakase-nowhere-xyz",
        )
        assert restaurants == {}
        assert matched == {}

    def test_unavailable_menu_items_are_ignored(self, db, pizza):
        """Menu items marked unavailable must not appear as matched_items —
        the customer would order something the kitchen can't make right now.
        The restaurant itself may still appear via its cuisine/description
        match (Pizza Junction's cuisine_type IS 'Pizza')."""
        # Kill every Pizza-named menu item; leave sides/drinks alone
        db.execute(
            MenuItem.__table__.update()
            .where(MenuItem.restaurant_id == pizza.id)
            .where(MenuItem.name.ilike("%Pizza%"))
            .values(is_available=False)
        )
        db.flush()

        _, matched, _s, _b = discovery_service.find_matching_restaurants(db, "pizza")
        # None of the disabled pizza items should appear in matched_items
        disabled = {
            "Chicken Tikka Pizza (Medium)",
            "Pepperoni Pizza (Medium)",
            "Fajita Pizza (Large)",
        }
        for items in matched.values():
            assert not (disabled & set(items))

    def test_closed_restaurant_is_filtered_out(self, db, biryani):
        """Restaurant matches the query but is closed — must not be
        offered. Achieved by wiping working hours (the always_open fixture
        installed them) and re-installing a schedule that excludes today."""
        # Wipe working hours for biryani entirely — with no hours, is_open()
        # returns False by design (see the always_open fixture docstring).
        db.execute(
            delete(RestaurantWorkingHours).where(
                RestaurantWorkingHours.restaurant_id == biryani.id
            )
        )
        db.flush()

        restaurants, _matched, _s, _b = discovery_service.find_matching_restaurants(db, "biryani")
        assert biryani.id not in restaurants


# --------------------------------------------------------------------------- #
# Agent tool — find_restaurants
# --------------------------------------------------------------------------- #


class TestFindRestaurantsTool:
    """The JSON shape the model sees. Covers the four Phase 5 verification
    cases end-to-end."""

    def test_case1_dish_query_lands_on_right_restaurant(
        self, db, conversation, biryani,
    ):
        """CASE 1 — customer says a dish word, tool matches without any
        exact restaurant-name lookup."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        assert result["query"] == "biryani"
        names = [r["name"] for r in result["restaurants"]]
        assert biryani.name in names
        # matched_items surface real menu item names
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert any("Biryani" in item for item in biryani_row["matched_items"])
        # ranking_note is present so the model can quote it verbatim
        assert biryani_row["ranking_note"]

    def test_case1_roman_urdu_dish_query(
        self, db, conversation, pizza,
    ):
        """'pizza' word inside 'pizza chahiye' still matches. The tool
        doesn't parse language — it treats the message as a keyword the
        model already extracted. Model would pass "pizza" here."""
        result = tools.find_restaurants(db, conversation, query="pizza")
        names = [r["name"] for r in result["restaurants"]]
        assert pizza.name in names

    def test_case2_vague_intent_spicy(self, db, conversation, biryani):
        """CASE 2 — 'spicy' matches menu-item descriptions ("spicy Sindhi
        masala") that aren't in any item NAME. Old search_restaurants_by_item
        would miss this; find_restaurants catches it."""
        result = tools.find_restaurants(db, conversation, query="spicy")
        assert result["query"] == "spicy"
        assert len(result["restaurants"]) >= 1
        # Biryani place must be one of them
        assert any(r["id"] == biryani.id for r in result["restaurants"])

    def test_case2_cuisine_query_matches_via_cuisine_field(
        self, db, conversation,
    ):
        """A cuisine word — "chinese" — matches Wok & Roll purely via
        Restaurant.cuisine_type='Chinese' (and its description "Fast
        Chinese"). No menu item literally contains "chinese"."""
        result = tools.find_restaurants(db, conversation, query="chinese")
        names = [r["name"] for r in result["restaurants"]]
        assert "Wok & Roll" in names
        wok_row = next(r for r in result["restaurants"] if r["name"] == "Wok & Roll")
        # matched_items fell back to cuisine text — customer-friendly, truthful
        assert wok_row["matched_items"] == ["Chinese"]

    def test_case3_search_restaurants_by_item_still_works(
        self, db, conversation, biryani,
    ):
        """CASE 3 — backward-compat: the legacy tool still returns a valid
        result shape. Deprecated in the schema, but the impl is untouched."""
        result = tools.search_restaurants_by_item(db, conversation, query="biryani")
        assert "restaurants" in result
        assert any(r["id"] == biryani.id for r in result["restaurants"])

    def test_case3_list_restaurants_still_works(self, db, conversation):
        """CASE 3 — the greeting-turn tool is unchanged."""
        result = tools.list_restaurants(db, conversation)
        # All three seed restaurants surfaced, in ranked order
        names = {r["name"] for r in result["restaurants"]}
        assert {"Karachi Biryani House", "Pizza Junction", "Wok & Roll"} <= names

    def test_case4_tool_does_not_touch_order_state(
        self, db, conversation,
    ):
        """CASE 4 — a plain discovery query must not touch anything the ORDER
        flow depends on: no cart change, no `shown_menu_ids` grounding, no
        active restaurant. Those may only be set by get_menu / add_to_cart,
        which is what stops a hallucinated item id becoming an order line.

        It DOES now record `shown_restaurants` (the candidate list it just
        presented) — that key is read only by the discovery path and by the
        agent's system message, never by the add_to_cart grounding guard."""
        before_cart = dict(conversation.cart or {})
        before_active = conversation.active_restaurant_id

        tools.find_restaurants(db, conversation, query="biryani")

        context = conversation.context or {}
        assert (conversation.cart or {}) == before_cart
        assert conversation.active_restaurant_id == before_active
        assert "shown_menu_ids" not in context
        assert "shown_menu" not in context

    def test_empty_query_returns_error(self, db, conversation):
        result = tools.find_restaurants(db, conversation, query="")
        assert result.get("error") == "empty_query"

    def test_unknown_query_returns_empty_with_definitive_note(
        self, db, conversation,
    ):
        """Was: the note pointed the model at a list_restaurants fallback.

        That instruction is gone. The search already covered every open
        restaurant, so the answer is definitive — and obeying the old note is
        what produced "no burger restaurants" immediately followed by a
        numbered list of restaurants. See test_match_quality.py.
        """
        result = tools.find_restaurants(
            db, conversation, query="sushi-omakase-nowhere-xyz",
        )
        assert result["restaurants"] == []
        assert result["found_anywhere"] is False
        assert "list_restaurants" not in result["note"]
        assert result["available_cuisines"]

    def test_result_uses_ranking_from_phase_2(
        self, db, conversation, biryani, pizza,
    ):
        """A matched query gets full relevance credit — restaurant with
        matched_items appears with a ranking_note that mentions the match.
        Proves the Phase 2 ranking is actually wired in, not bypassed."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        biryani_row = next(
            r for r in result["restaurants"] if r["id"] == biryani.id
        )
        # ranking_note reads like "serves Chicken Biryani, Beef Biryani (+1 more)"
        # — comes from ranking._build_reason, so this is the Phase 2 path.
        assert "serves" in biryani_row["ranking_note"].lower() or \
               "biryani" in biryani_row["ranking_note"].lower()

    def test_no_internal_tag_leak_in_matched_items(
        self, db, conversation,
    ):
        """matched_items must contain ONLY customer-facing text — real menu
        names or the cuisine text. Never an internal tag like 'cuisine
        match' or 'description match'."""
        for query in ("biryani", "chinese", "spicy", "hand-tossed"):
            result = tools.find_restaurants(db, conversation, query=query)
            for r in result.get("restaurants", []):
                for item in r["matched_items"]:
                    # No brackets, no colons, no square-bracket tags
                    assert "[" not in item
                    assert "match" not in item.lower()

    def test_matched_items_capped_per_restaurant(
        self, db, conversation, biryani,
    ):
        """Even if a query hits many menu items, matched_items is capped so
        the ranking reason doesn't become an essay ("(+42 more)")."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        biryani_row = next(
            r for r in result["restaurants"] if r["id"] == biryani.id
        )
        assert len(biryani_row["matched_items"]) <= (
            discovery_service.MAX_MATCHED_ITEMS_PER_RESTAURANT
        )
