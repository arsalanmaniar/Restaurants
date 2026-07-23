"""Match quality — anchoring, provenance, and the honest zero-match answer.

From a real WhatsApp trace, all four turns on a genuine zero-match query:

    "hi"                          -> full 3-restaurant list          (correct)
    "burger khana h"              -> a shortlist with ONE restaurant (wrong)
    "burger khana h woh h?"       -> full generic list, context lost (wrong)
    "burger mil jaega ye batao?"  -> "Burger milne wale restaurants nahi hai"
                                     AND "1. Karachi Biryani House"  (wrong AND
                                     self-contradicting in one message)

There is no burger anywhere in the catalog, so every restaurant offered was
wrong. Three defects, pinned here:

  1. Unanchored `ILIKE '%term%'` matched mid-word. "mil" (from "mil jaega")
     matched "Fa-MIL-y" in both "Special Family Biryani" and Karachi Biryani
     House's description. Same class: "cola" -> "Cho-COLA-te Shake",
     "lassi" -> "c-LASSI-c". Fixed by anchoring to word starts.

  2. Every match scored relevance 1.0, so a word buried in description prose
     ranked exactly like a real dish match, and the model had no way to tell
     them apart. Fixed by grading the match and reporting the grade.

  3. The zero-match note said "fall back to list_restaurants", so the model
     obediently printed a restaurant list directly under its own "we have no
     burgers" sentence. Fixed by making the zero-match answer definitive and
     forbidding the contradictory header.
"""

from app.services import discovery as discovery_service
from app.services import ranking
from app.services import tools


# --------------------------------------------------------------------------- #
# 1. Anchored matching
# --------------------------------------------------------------------------- #


class TestWordStartAnchoring:

    def test_mid_word_substrings_no_longer_match(self, db):
        """The three confirmed false positives. Each of these matched under
        `ILIKE '%term%'` and must now match nothing."""
        for term, buried_in in (
            ("mil", "Special Fa-mil-y Biryani"),
            ("cola", "Cho-cola-te Shake"),
            ("lassi", "c-lassi-c (Pepperoni description)"),
        ):
            found = discovery_service.find_matching_restaurants(db, term)
            assert found.restaurants == {}, (
                f"{term!r} still matches — it was hiding inside {buried_in}"
            )

    def test_the_exact_trace_query_returns_nothing(self, db, conversation):
        """"burger mil jaega ye batao?" is the message that produced the
        contradictory reply. It must now come back genuinely empty."""
        found = discovery_service.find_matching_restaurants(
            db, "burger mil jaega ye batao?",
        )
        assert found.restaurants == {}

    def test_real_matches_are_all_preserved(self, db, biryani, pizza):
        """Anchoring must not cost a single legitimate match."""
        for term in ("biryani", "chicken", "pizza", "desi", "chinese"):
            found = discovery_service.find_matching_restaurants(db, term)
            assert found.restaurants, f"{term!r} regressed to zero matches"

    def test_word_start_inside_a_multi_word_name_still_matches(self, db):
        """"tikka" sits mid-NAME in "Chicken Tikka Pizza" but at a word start.
        Whole-word anchoring would have been too strict here."""
        found = discovery_service.find_matching_restaurants(db, "tikka")
        assert found.restaurants

    def test_prefix_matching_is_kept_deliberately(self, db, biryani):
        """`\\m` is word-START, not whole-word, so "biry" still finds every
        biryani. This is why we did not use `\\y...\\y`."""
        found = discovery_service.find_matching_restaurants(db, "biry")
        assert biryani.id in found.restaurants

    def test_description_only_match_still_found(self, db, biryani):
        """"spicy" lives only in Beef Biryani's description. Anchoring must
        not kill description search — only mid-word noise."""
        found = discovery_service.find_matching_restaurants(db, "spicy")
        assert biryani.id in found.restaurants

    def test_hyphenated_query_survives(self, db, pizza):
        found = discovery_service.find_matching_restaurants(db, "hand-tossed")
        assert pizza.id in found.restaurants

    def test_regex_metacharacters_do_not_blow_up(self, db, conversation):
        """The phrase pass searches raw customer text. An unescaped "(" used
        to be a regex syntax error, i.e. a 500 on a normal message."""
        for hostile in (
            "pizza (medium)",
            "biryani*",
            "what? [urgent]",
            "a+b",
            "back\\slash",
            "50% off",
            "^caret$",
        ):
            found = discovery_service.find_matching_restaurants(db, hostile)
            assert isinstance(found.restaurants, dict)
            result = tools.find_restaurants(db, conversation, query=hostile)
            assert "restaurants" in result or "error" in result

    def test_deprecated_item_search_is_anchored_too(self, db, conversation):
        """search_restaurants_by_item is deprecated but still callable, and
        it had the identical mid-word bug."""
        result = tools.search_restaurants_by_item(db, conversation, query="cola")
        assert result["restaurants"] == []


# --------------------------------------------------------------------------- #
# 2. Match provenance and graded relevance
# --------------------------------------------------------------------------- #


class TestMatchStrength:

    def test_dish_name_hit_is_strong(self, db, biryani):
        found = discovery_service.find_matching_restaurants(db, "biryani")
        assert found.strengths[biryani.id] == discovery_service.MATCH_STRONG

    def test_cuisine_hit_is_strong(self, db, biryani):
        found = discovery_service.find_matching_restaurants(db, "desi")
        assert found.strengths[biryani.id] == discovery_service.MATCH_STRONG

    def test_description_only_hit_is_weak(self, db, biryani, pizza):
        """"spicy" is real but circumstantial — it proves a description
        mentions spice, not that a dish called "spicy" exists."""
        found = discovery_service.find_matching_restaurants(db, "spicy")
        assert found.strengths[biryani.id] == discovery_service.MATCH_WEAK

        found = discovery_service.find_matching_restaurants(db, "hand-tossed")
        assert found.strengths[pizza.id] == discovery_service.MATCH_WEAK

    def test_strong_signal_wins_over_weak_for_same_restaurant(self, db, biryani):
        """"chicken" hits Chicken Biryani by NAME and other items only by
        description. One real name match makes the whole restaurant strong."""
        found = discovery_service.find_matching_restaurants(db, "chicken")
        assert found.strengths[biryani.id] == discovery_service.MATCH_STRONG

    def test_tool_surfaces_strength_and_provenance(self, db, conversation, biryani):
        result = tools.find_restaurants(db, conversation, query="biryani")
        row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert row["match_strength"] == discovery_service.MATCH_STRONG
        assert "menu item name" in row["matched_on"]

    def test_weak_match_is_flagged_to_the_model(self, db, conversation, biryani):
        result = tools.find_restaurants(db, conversation, query="spicy")
        row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert row["match_strength"] == discovery_service.MATCH_WEAK
        assert "may NOT be on the menu" in row["matched_on"]
        # All-weak results carry an explicit don't-promise-this warning.
        assert result["weak_matches_only"] is True
        assert "Do NOT claim they do" in result["weak_match_note"]

    def test_strong_result_carries_no_weak_warning(self, db, conversation):
        result = tools.find_restaurants(db, conversation, query="biryani")
        assert "weak_matches_only" not in result

    def test_weak_match_ranks_below_a_strong_one(self, db, biryani, pizza):
        """The scoring consequence: 3×0.4 = 1.2 for weak vs 3×1.0 = 3.0 for
        strong, so a real dish match can never be outranked by prose."""
        ranked = ranking.rank_restaurants(
            db,
            [biryani, pizza],
            matched_items_by_id={biryani.id: ["Chicken Biryani"], pizza.id: ["Pizza"]},
            relevance_by_id={
                biryani.id: discovery_service.RELEVANCE_BY_STRENGTH["weak"],
                pizza.id: discovery_service.RELEVANCE_BY_STRENGTH["strong"],
            },
        )
        assert ranked[0].restaurant.id == pizza.id

    def test_ranking_without_relevance_is_unchanged(self, db, biryani, pizza):
        """list_restaurants and search_restaurants_by_item pass no relevance
        map and must behave exactly as before."""
        ranked = ranking.rank_restaurants(
            db, [biryani, pizza], matched_items_by_id={biryani.id: ["Chicken Biryani"]},
        )
        assert ranked[0].restaurant.id == biryani.id  # matched beats unmatched


class TestBroadenedSearch:

    def test_exact_phrase_match_is_not_broadened(self, db):
        found = discovery_service.find_matching_restaurants(db, "chicken biryani")
        assert found.broadened is False

    def test_token_fallback_is_reported_as_broadened(self, db, biryani):
        """"Biryani ka batao" only matches after the phrase is split, so the
        customer must be told the search was widened."""
        found = discovery_service.find_matching_restaurants(db, "Biryani ka batao")
        assert biryani.id in found.restaurants
        assert found.broadened is True

    def test_zero_match_is_not_broadened(self, db):
        """Nothing was found, so nothing was widened into — the flag would
        only confuse the model."""
        found = discovery_service.find_matching_restaurants(db, "burger khana h")
        assert found.broadened is False

    def test_tool_tells_the_model_to_admit_widening(self, db, conversation):
        result = tools.find_restaurants(db, conversation, query="Biryani ka batao")
        assert result["broadened"] is True
        assert "exactly" in result["broadened_note"]


# --------------------------------------------------------------------------- #
# 3. The honest zero-match answer
# --------------------------------------------------------------------------- #


class TestNothingAnywhere:

    def test_burger_is_a_genuine_zero_match(self, db, conversation):
        """Confirms the premise of the whole trace: there is no burger."""
        result = tools.find_restaurants(db, conversation, query="burger")
        assert result["restaurants"] == []
        assert result["found_anywhere"] is False

    def test_note_forbids_the_contradictory_header(self, db, conversation):
        """The exact bug: "no burger restaurants" followed by a numbered list
        of restaurants under a burger heading."""
        result = tools.find_restaurants(db, conversation, query="burger")
        note = result["note"]
        assert "not available" in note
        # Asserted on meaning rather than exact prose — the note is guidance
        # copy and gets reworded; the CONTRACT is that it forbids the list and
        # never sends the model back to list_restaurants.
        assert "numbered restaurant list" in note
        assert "contradicts" in note
        assert "list_restaurants" not in note

    def test_constructive_alternative_is_offered(self, db, conversation):
        """Forbidding the list is not enough — the model needs something
        truthful to pivot to, or it just dead-ends the customer."""
        result = tools.find_restaurants(db, conversation, query="burger")
        cuisines = result["available_cuisines"]
        assert {"Desi", "Pizza", "Chinese"} <= set(cuisines)

    def test_no_restaurant_names_in_the_zero_match_payload(self, db, conversation):
        """Cuisines, not restaurant names. Naming restaurants right after
        "we don't have it" is what read as a contradiction."""
        result = tools.find_restaurants(db, conversation, query="burger")
        blob = str(result)
        for name in ("Karachi Biryani House", "Pizza Junction", "Wok & Roll"):
            assert name not in blob

    def test_repeated_zero_match_stays_consistent(self, db, conversation):
        """Turns 2-4 of the trace: asking three times in different words must
        give the same definitive answer each time, never a reset."""
        for msg in ("burger khana h", "burger khana h woh h?", "burger mil jaega ye batao?"):
            result = tools.find_restaurants(db, conversation, query=msg)
            assert result["restaurants"] == [], f"{msg!r} matched something"
            assert result["found_anywhere"] is False
            assert "list_restaurants" not in result["note"]

    def test_active_restaurant_case_is_unchanged(self, db, conversation, pizza):
        """A zero-match while browsing a restaurant keeps the Phase 8
        behaviour — stay put, don't reset."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        result = tools.find_restaurants(db, conversation, query="burger")
        assert pizza.name in result["note"]
        assert "do NOT call list_restaurants" in result["note"]
