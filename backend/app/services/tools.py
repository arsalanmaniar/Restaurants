"""The functions the AI is allowed to call.

Every one of these takes (db, conversation, **args) and returns a plain dict that
gets fed back to the model as the tool result. Rules that matter:

  * The model is never trusted with prices. It passes ids and quantities; we look
    up the real price from the DB. Otherwise a customer could talk the model into
    a Rs. 1 pizza.
  * Anything the model gets wrong should come back as {"error": ...} so it can
    recover conversationally, rather than raising and killing the turn.
"""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.services import coupons as coupons_service
from app.services import discovery as discovery_service
from app.services import promotions as promotions_service
from app.services import ranking
from app.services import upsell as upsell_service
from app.services.opening_hours import is_open
from app.services.payments.registry import available_methods, provider_for_method
from app.services.payments.service import start_payment

from app.models import (
    Conversation,
    ConversationState,
    CustomerFavorite,
    MenuCategory,
    MenuItem,
    # Referenced by _model_has_asked_payment / _has_any_outbound below; the
    # guard was added in commit 04d2f3d without this import, so any real
    # conversation (which always has OUTBOUNDs) would NameError on the first
    # place_order with multiple payment methods on. Tests never seeded an
    # OUTBOUND before place_order, so the bug went undetected.
    MessageDirection,
    PaymentMethod,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    Restaurant,
    RestaurantStatus,
)

MAX_QUANTITY_PER_LINE = 50


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def cart_restaurant(conversation: Conversation) -> int | None:
    """Which restaurant the CART belongs to.

    Derived from the cart's own lines, never from conversation.active_restaurant_id —
    that field tracks what the customer is *browsing* and changes on every get_menu.
    Billing an order to the last-browsed restaurant is how a pizza order ends up at a
    biryani house.
    """
    lines = (conversation.cart or {}).get("items", [])
    for line in lines:
        if line.get("restaurant_id") is not None:
            return int(line["restaurant_id"])
    return None


def generate_order_number() -> str:
    return f"AB-{secrets.token_hex(3).upper()}"


# --------------------------------------------------------------------------- #
# Shown-restaurant memory
#
# `get_menu` has always recorded what it showed (`shown_menu`, `shown_menu_ids`)
# so the model can answer follow-ups from real data. The DISCOVERY tools had no
# equivalent: a customer asked "Biryani hai?", got "1. Karachi Biryani House
# 2. Mandi House", then asked "Mandi house per hoti h biryani?" — and because
# nothing remembered that list, the follow-up was treated as fresh discovery,
# matched nothing, and reset the customer to the generic restaurant list.
#
# Two things fix that, and both live below:
#   * remember the candidate list (`shown_restaurants`), and
#   * treat a message that NAMES one of those candidates as a selection.
# --------------------------------------------------------------------------- #

SHOWN_RESTAURANTS_KEY = "shown_restaurants"
SHOWN_RESTAURANTS_QUERY_KEY = "shown_restaurants_query"
MAX_REMEMBERED_SHOWN_RESTAURANTS = 20

# Words that appear in restaurant names but identify nothing on their own.
# Stripped when deriving a name's "distinctive core", so "Mandi House" can be
# picked out of a message by the word "mandi" alone.
_GENERIC_NAME_WORDS = frozenset(
    {"restaurant", "restaurants", "house", "kitchen", "cafe", "hotel", "the", "and", "co"}
)


def _normalize_for_match(text: str) -> str:
    """Lowercase, punctuation-to-space, single-spaced. '&' in "Wok & Roll" and a
    trailing '?' on a WhatsApp message must not change whether a name matches."""
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in (text or "")).lower().split()
    )


def _distinctive_core(name: str) -> str:
    """The restaurant name with generic words removed — "Mandi House" -> "mandi",
    "Karachi Biryani House" -> "karachi biryani". Falls back to the full
    normalized name when a name is generic all the way through."""
    words = [w for w in _normalize_for_match(name).split() if w not in _GENERIC_NAME_WORDS]
    return " ".join(words) or _normalize_for_match(name)


def remember_shown_restaurants(
    conversation: Conversation, entries: list[dict], query: str = "",
) -> None:
    """Record the candidate list just presented to the customer.

    Reassigned rather than mutated — JSONB change tracking needs a new object,
    the same reason `get_menu` rebuilds `context` instead of updating it in place.
    """
    if not entries:
        return
    context = dict(conversation.context or {})
    context[SHOWN_RESTAURANTS_KEY] = [
        {"id": int(e["id"]), "name": e["name"]}
        for e in entries[:MAX_REMEMBERED_SHOWN_RESTAURANTS]
    ]
    context[SHOWN_RESTAURANTS_QUERY_KEY] = query
    conversation.context = context


def shown_restaurants(conversation: Conversation) -> list[dict]:
    return list((conversation.context or {}).get(SHOWN_RESTAURANTS_KEY) or [])


def resolve_shown_candidate(conversation: Conversation, query: str) -> int | None:
    """The id of a just-shown restaurant that this message NAMES, if any.

    Deliberately scoped to the shown list, never the whole catalog: naming a
    restaurant we just offered is a selection, whereas an incidental word match
    against some restaurant the customer has never seen is the silent-switch bug
    Phase 8 fixed. Two tiers, strictest first:

      A. The full name appears in the message — "mandi house per hoti h biryani".
      B. The distinctive core appears as whole words AND identifies exactly one
         candidate — "mandi wala" -> Mandi House.

    Ambiguity always loses, in BOTH tiers: if the message could mean two
    different candidates we return None and let the normal search run, so the
    model re-offers the shortlist and asks instead of picking for the customer.
    Choosing a restaurant on someone's behalf is worse than one extra question.
    """
    candidates = shown_restaurants(conversation)
    if not candidates:
        return None

    padded = f" {_normalize_for_match(query)} "
    if padded.strip() == "":
        return None

    full_hits = [
        entry for entry in candidates
        if f" {_normalize_for_match(entry['name'])} " in padded
    ]
    if full_hits:
        longest = max(
            full_hits, key=lambda e: len(_normalize_for_match(e["name"]))
        )
        longest_name = _normalize_for_match(longest["name"])
        # Nested names — "Biryani House" sitting inside "Karachi Biryani House"
        # — are ONE mention, and the longest is the real match. Two genuinely
        # different names in one message means the customer is comparing them
        # ("Karachi Biryani House ya Pizza Junction?"), not picking one.
        if all(
            _normalize_for_match(entry["name"]) in longest_name
            for entry in full_hits
        ):
            return int(longest["id"])
        return None

    core_hits = [
        entry for entry in candidates
        if f" {_distinctive_core(entry['name'])} " in padded
    ]
    if len(core_hits) == 1:
        return int(core_hits[0]["id"])

    return None


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #


def list_restaurants(db: Session, conversation: Conversation, cuisine: str | None = None) -> dict:
    stmt = select(Restaurant).where(
        Restaurant.status == RestaurantStatus.ACTIVE,
        Restaurant.is_accepting_orders.is_(True),
    )
    if cuisine:
        stmt = stmt.where(Restaurant.cuisine_type.ilike(f"%{cuisine}%"))

    candidates = db.scalars(stmt.order_by(Restaurant.name).limit(50)).all()

    # Closed-for-the-night restaurants must not be offered — the customer would order
    # into a dark kitchen. Filtered in Python because "open now" spans midnight and is
    # timezone-dependent; at 20-100 restaurants this costs nothing.
    restaurants = [r for r in candidates if is_open(r)][:20]

    if not restaurants:
        return {
            "restaurants": [],
            "note": "No restaurants are open right now."
            if not cuisine
            else f"No open restaurants match '{cuisine}'.",
        }

    # Rank instead of taking the alphabetical order the SQL returned. Old
    # behaviour: "Karachi Biryani House" always at position 1. New: relevance
    # (nil for undifferentiated list) + live rating + daily rotation seed —
    # see services/ranking.py.
    ranked = ranking.rank_restaurants(db, restaurants)

    payload: dict = {
        "restaurants": [
            {
                "id": rr.restaurant.id,
                "name": rr.restaurant.name,
                "cuisine": rr.restaurant.cuisine_type,
                "delivery_fee": _money(rr.restaurant.delivery_fee),
                "min_order": _money(rr.restaurant.min_order_amount),
                # Model-facing explanation for the ranking position. Meant to be
                # quotable verbatim if a customer asks "why this one?". Never
                # leaks the formula or the rotation logic.
                "ranking_note": rr.reason,
            }
            for rr in ranked
        ]
    }
    # A single-restaurant result with a cuisine filter is a common dead-end (that
    # one restaurant might not actually serve the specific dish the customer
    # wanted). Hint at the smarter fallback so the model doesn't just present the
    # lone result as final.
    if cuisine and len(restaurants) == 1:
        payload["note"] = (
            f"Only one match for '{cuisine}'. If the customer's dish isn't on this "
            "restaurant's menu, call search_restaurants_by_item next."
        )
    remember_shown_restaurants(conversation, payload["restaurants"], query=cuisine or "")
    return payload


def search_restaurants_by_item(
    db: Session, conversation: Conversation, query: str
) -> dict:
    """Cross-restaurant menu search by dish/item name.

    Structured Postgres query (ILIKE substring), not a vector search — the catalog
    is a few hundred items per restaurant at most, so a keyword match on the item
    name is plenty and needs no new infrastructure. This is what lets the flow go
    straight from "biryani" to "these restaurants have it" instead of "here are all
    our restaurants, pick blind."
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Give a dish or cuisine keyword to search for."}

    rows = db.execute(
        select(Restaurant, MenuItem.name)
        .join(MenuItem, MenuItem.restaurant_id == Restaurant.id)
        .where(
            Restaurant.status == RestaurantStatus.ACTIVE,
            Restaurant.is_accepting_orders.is_(True),
            MenuItem.is_available.is_(True),
            MenuItem.name.ilike(f"%{query}%"),
        )
        .order_by(Restaurant.name, MenuItem.name)
    ).all()

    matches: dict[int, dict] = {}
    restaurant_by_id: dict[int, Restaurant] = {}
    for restaurant, item_name in rows:
        if not is_open(restaurant):
            continue  # same "don't offer a dark kitchen" rule as list_restaurants
        restaurant_by_id[restaurant.id] = restaurant
        entry = matches.setdefault(
            restaurant.id,
            {
                "id": restaurant.id,
                "name": restaurant.name,
                "cuisine": restaurant.cuisine_type,
                "matched_items": [],
            },
        )
        if len(entry["matched_items"]) < 5 and item_name not in entry["matched_items"]:
            entry["matched_items"].append(item_name)

    if not matches:
        return {
            "restaurants": [],
            "note": f"No open restaurant has an item matching '{query}'.",
        }

    # Rank: matched_items carries the relevance signal (=1.0), so every
    # match here gets the full relevance bump. Rating + rotation then sort
    # among them. Cap at 20 in ranked order (was 20 in insertion order).
    ranked = ranking.rank_restaurants(
        db,
        list(restaurant_by_id.values()),
        matched_items_by_id={rid: entry["matched_items"] for rid, entry in matches.items()},
    )[:20]

    entries = [
        {**matches[rr.restaurant.id], "ranking_note": rr.reason}
        for rr in ranked
    ]
    remember_shown_restaurants(conversation, entries, query=query)
    return {"query": query, "restaurants": entries}


def _empty_result_note(db: Session, conversation: Conversation, query: str) -> str:
    """What to tell the model when a search found nothing.

    The old note said "fall back to list_restaurants" unconditionally, which is
    right only for a customer who has been shown nothing yet. For everyone else
    it is an instruction to throw away the context they are standing in — and
    because a filler word like "chaiye" was enough to produce an empty result,
    that reset fired on perfectly ordinary messages. So the advice now depends
    on what the customer can actually see.
    """
    if conversation.active_restaurant_id is not None:
        restaurant = db.get(Restaurant, conversation.active_restaurant_id)
        if restaurant is not None:
            return (
                f"No match for '{query}'. The customer is currently browsing "
                f"{restaurant.name} — do NOT call list_restaurants, that would reset "
                "them. Answer from the menu already shown, say plainly if the item "
                "isn't on it, and ask whether they want you to look elsewhere."
            )

    candidates = shown_restaurants(conversation)
    if candidates:
        names = ", ".join(entry["name"] for entry in candidates)
        return (
            f"No match for '{query}'. You have ALREADY shown this customer these "
            f"restaurants: {names}. Do NOT call list_restaurants — that throws away "
            "the list they are choosing from and feels like the conversation "
            "restarted. Re-offer those options (or say plainly that this wasn't "
            "found among them) and ask which one they want."
        )

    return (
        f"No open restaurant matches '{query}'. Fall back to "
        "list_restaurants and offer whatever is available — never tell "
        "the customer 'we have nothing' without offering the full list."
    )


def find_restaurants(
    db: Session,
    conversation: Conversation,
    query: str = "",
    budget: float | int | str | None = None,
    party_size: int | None = None,
) -> dict:
    """Intent-based discovery — one tool that turns almost any customer
    phrasing ("pizza chahiye", "something spicy", "chinese", "family
    dinner", "1500 mein kya milega") into a ranked shortlist of open
    restaurants, with an optional budget-fit assessment per restaurant.

    Searches five columns at once (restaurant name, cuisine, description,
    menu item name, menu item description). Ranking uses the same Phase 2
    formula (`ranking.rank_restaurants`) that the older tools use.

    Optional `budget` (in Rs) + `party_size` add a per-restaurant `estimate`
    with `estimated_total` (cheapest matched item × party_size + delivery,
    clamped to the restaurant's minimum order amount) and a `fits_budget`
    boolean. When NOTHING fits, a top-level `note` guides the model to
    offer to broaden the budget rather than pretend an option fits.

    `query` is optional ONLY when `budget` is set — a bare budget without
    any cuisine hint ("1000 mein kya milega") is a real customer question;
    without a query we list every open restaurant and estimate against
    each one's cheapest item.
    """
    trimmed = (query or "").strip()

    # Parse budget defensively — the model has been observed sending
    # numbers-as-strings ("1500") when the schema declares number.
    parsed_budget: Decimal | None = None
    if budget is not None and budget != "":
        try:
            parsed_budget = Decimal(str(budget))
        except (ArithmeticError, ValueError):
            return {
                "error": "invalid_budget",
                "message": "Budget must be a number (Rs), e.g. 1500.",
            }
        if parsed_budget <= 0:
            return {
                "error": "invalid_budget",
                "message": "Budget must be a positive number.",
            }

    parsed_party = max(1, int(party_size or 1))

    # Bare tool call with no query AND no budget is a no-op — refuse so the
    # model asks the customer for one or the other.
    if not trimmed and parsed_budget is None:
        return {
            "error": "empty_query",
            "message": (
                "Give me a keyword or short phrase from the customer's own words — "
                "a dish, cuisine, style, or intent. If the customer only gave a "
                "budget with no cuisine hint, pass `budget` and this tool will "
                "list every open restaurant with an estimate."
            ),
        }

    # SELECTION BEFORE SEARCH. If this message names a restaurant we just put in
    # front of the customer, they are picking it, not starting a new search —
    # "Mandi house per hoti h biryani?" right after we listed Mandi House is a
    # selection plus a menu question. Treating it as discovery is what reset the
    # conversation to the generic restaurant list.
    #
    # Skipped when a budget or party_size is in play: those are genuinely
    # cross-restaurant comparison questions even when a name is mentioned.
    if trimmed and parsed_budget is None and not party_size:
        selected_id = resolve_shown_candidate(conversation, trimmed)
        if selected_id is not None:
            menu = get_menu(db, conversation, restaurant_id=selected_id)
            # get_menu sets active_restaurant_id + shown_menu, so from here on
            # Phase 8's restaurant-scoped continuity carries the conversation.
            # If it failed (closed, empty menu) fall through to a normal search
            # rather than dead-ending the customer.
            if "error" not in menu:
                return {
                    "query": trimmed,
                    "selected_from_shown_list": True,
                    "restaurant": menu["restaurant"],
                    "items": menu["items"],
                    "instruction": (
                        f"The customer picked {menu['restaurant']['name']} from the "
                        "list you just showed them — this is a SELECTION, not a new "
                        "search. It is now the active restaurant and its full menu is "
                        "above. Do NOT call find_restaurants or list_restaurants "
                        "again. If they asked whether a specific dish is available, "
                        "answer from these items (quote the real Rs. price); "
                        "otherwise show the menu and ask what they'd like."
                    ),
                }

    if trimmed:
        restaurants, matched_items = discovery_service.find_matching_restaurants(
            db, trimmed,
        )
    else:
        # Budget-only fallback — customer said "1000 mein kya milega" with
        # no cuisine hint. List every open, accepting restaurant so the
        # estimate machinery below can grade them.
        all_active = db.scalars(
            select(Restaurant).where(
                Restaurant.status == RestaurantStatus.ACTIVE,
                Restaurant.is_accepting_orders.is_(True),
            )
        ).all()
        restaurants = {r.id: r for r in all_active if is_open(r)}
        # matched_items gets the cuisine text so ranking's "serves X" reason
        # reads naturally and every candidate gets the same relevance
        # baseline (0 concrete matches → rating + rotation carry the order).
        matched_items = {rid: [r.cuisine_type] for rid, r in restaurants.items()}

    if not restaurants:
        return {
            "query": trimmed,
            "restaurants": [],
            "note": _empty_result_note(db, conversation, trimmed),
        }

    ranked = ranking.rank_restaurants(
        db,
        list(restaurants.values()),
        matched_items_by_id=matched_items,
    )[:20]

    entries: list[dict] = []
    fits_budget_count = 0
    for rr in ranked:
        entry: dict = {
            "id": rr.restaurant.id,
            "name": rr.restaurant.name,
            "cuisine": rr.restaurant.cuisine_type,
            "matched_items": matched_items.get(rr.restaurant.id, []),
            "ranking_note": rr.reason,
        }

        # Estimates only when the caller opted in with a budget or an
        # explicit party_size — keeps the Phase 5 response shape identical
        # for all existing callers (find_restaurants(query="biryani") →
        # exactly what it returned before). The model reads the tool
        # description and only passes these when the customer mentioned
        # money or a group size.
        wants_estimate = parsed_budget is not None or (
            party_size is not None and party_size > 0
        )
        if wants_estimate:
            # Cost estimate needs REAL MenuItem rows — pull them by name.
            # Cuisine-only matches (matched_items = [cuisine text]) don't
            # correspond to any MenuItem row, so fall back to the cheapest
            # available item as the estimate basis.
            names = matched_items.get(rr.restaurant.id, [])
            matched_objs = db.scalars(
                select(MenuItem).where(
                    MenuItem.restaurant_id == rr.restaurant.id,
                    MenuItem.name.in_(names),
                    MenuItem.is_available.is_(True),
                )
            ).all() if names else []
            if not matched_objs:
                fallback = discovery_service._cheapest_available_item(
                    db, rr.restaurant.id,
                )
                matched_objs = [fallback] if fallback is not None else []

            estimate = discovery_service.estimate_meal_cost(
                matched_menu_items=matched_objs,
                delivery_fee=rr.restaurant.delivery_fee,
                min_order_amount=rr.restaurant.min_order_amount,
                party_size=parsed_party,
            )
            if estimate is not None:
                entry["estimate"] = estimate
                if parsed_budget is not None:
                    fits = Decimal(estimate["estimated_total"]) <= parsed_budget
                    entry["fits_budget"] = fits
                    if fits:
                        fits_budget_count += 1

        entries.append(entry)

    # Remember what we are about to put in front of the customer, so the NEXT
    # message naming one of these lands in the selection path above instead of
    # being re-searched from scratch.
    remember_shown_restaurants(conversation, entries, query=trimmed)

    result: dict = {"query": trimmed, "restaurants": entries}

    if parsed_budget is not None:
        result["budget"] = f"{parsed_budget:.2f}"
        result["party_size"] = parsed_party
        # "Nothing fits" is a common case that needs a graceful response.
        # Point the model at the cheapest option so it can honestly offer
        # "the closest is X at Rs. Y — want to stretch the budget?" instead
        # of pretending something fits.
        if entries and fits_budget_count == 0:
            with_estimate = [e for e in entries if "estimate" in e]
            if with_estimate:
                cheapest = min(
                    with_estimate,
                    key=lambda e: Decimal(e["estimate"]["estimated_total"]),
                )
                result["note"] = (
                    f"None of the matches fit Rs. {parsed_budget:.0f} for "
                    f"party of {parsed_party}. Cheapest option: "
                    f"{cheapest['name']} at Rs. {cheapest['estimate']['estimated_total']}. "
                    "Tell the customer honestly and offer to broaden the "
                    "budget or try a different cuisine — do NOT pretend "
                    "an option fits when it does not."
                )

    return result


def _resolve_restaurant(
    db: Session, restaurant_id: int | str | None, restaurant_name: str | None
) -> Restaurant | None:
    """Find a restaurant from whatever the model actually sent.

    The model reliably passes the NAME ("Pizza Junction") where the schema asked for
    an integer id, which Groq then rejects as a malformed call — the customer sees a
    dead turn. Rather than keep fighting it, accept a name or an id, in either field.
    """
    candidates: list[str | int] = [
        value for value in (restaurant_id, restaurant_name) if value not in (None, "")
    ]

    for value in candidates:
        # An id, or a name that happens to arrive as a numeric string.
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            restaurant = db.get(Restaurant, int(value))
            if restaurant is not None:
                return restaurant
            continue

        if isinstance(value, str):
            restaurant = db.scalar(
                select(Restaurant)
                .where(
                    Restaurant.name.ilike(f"%{value.strip()}%"),
                    Restaurant.status == RestaurantStatus.ACTIVE,
                )
                .order_by(Restaurant.name)
                .limit(1)
            )
            if restaurant is not None:
                return restaurant

    return None


def get_menu(
    db: Session,
    conversation: Conversation,
    restaurant_id: int | str | None = None,
    restaurant_name: str | None = None,
) -> dict:
    restaurant = _resolve_restaurant(db, restaurant_id, restaurant_name)
    if restaurant is None or restaurant.status != RestaurantStatus.ACTIVE:
        return {
            "error": "unknown_restaurant",
            "message": (
                "No open restaurant matched that. Call list_restaurants and use an "
                "id or an exact name from the result."
            ),
        }

    if not is_open(restaurant):
        return {
            "error": "closed",
            "message": f"{restaurant.name} is closed right now. Offer the customer another one.",
        }

    # Order by category first: MenuItem.sort_order restarts at 0 inside each
    # category, so sorting on it alone interleaves starters with desserts.
    items = db.scalars(
        select(MenuItem)
        .outerjoin(MenuCategory, MenuItem.category_id == MenuCategory.id)
        .where(MenuItem.restaurant_id == restaurant.id, MenuItem.is_available.is_(True))
        .order_by(
            MenuCategory.sort_order.nulls_last(),
            MenuCategory.id.nulls_last(),
            MenuItem.sort_order,
            MenuItem.name,
        )
    ).all()

    if not items:
        return {"error": f"{restaurant.name} has no items available right now."}

    # Remember which restaurant the customer is looking at, so add_to_cart doesn't
    # need the model to keep repeating the id.
    conversation.active_restaurant_id = restaurant.id
    conversation.state = ConversationState.BROWSING

    # Grounding: record exactly which item ids the model has been shown. add_to_cart
    # refuses anything outside this set, so a hallucinated id can never become an
    # order line. (Reassign, don't mutate — JSONB change tracking needs a new object.)
    context = dict(conversation.context or {})
    shown = set(context.get("shown_menu_ids", [])) | {i.id for i in items}
    context["shown_menu_ids"] = sorted(shown)
    # Keep the real prices in conversation state too. The model has been caught
    # quoting invented prices ("Rs. 850") when it couldn't be bothered to re-call
    # get_menu; with the true menu in its context each turn, it never has to guess.
    context["shown_menu"] = [
        {"id": i.id, "name": i.name, "price": _money(i.price)} for i in items
    ]
    context["shown_menu_restaurant"] = restaurant.name
    conversation.context = context

    return {
        "restaurant": {"id": restaurant.id, "name": restaurant.name},
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "description": i.description,
                "price": _money(i.price),
                "category": i.category.name if i.category else None,
            }
            for i in items
        ],
    }


def list_active_deals(
    db: Session,
    conversation: Conversation,
    restaurant_id: int | str | None = None,
    restaurant_name: str | None = None,
) -> dict:
    """Currently-running promotions for one restaurant.

    Returns a customer-friendly summary the model can mention naturally — a
    title, human-readable discount string, and the date window. Never returns
    a promotion whose window has expired or which the restaurant staff
    manually deactivated (see services/promotions.py::is_active_at).

    Restaurant is identified by id OR name, same shape as get_menu / add_favorite,
    so the model can pass whichever it has. For MVP the deals are INFORMATIONAL —
    place_order does not auto-apply them yet, so the model should quote the
    deal as marketing, not promise a specific discount at checkout.
    """
    restaurant = _resolve_restaurant(db, restaurant_id, restaurant_name)
    if restaurant is None:
        return {
            "error": "unknown_restaurant",
            "message": (
                "No restaurant matched that. Call list_restaurants and use an "
                "id or name from the result."
            ),
        }

    promotions = promotions_service.list_active_for_restaurant(db, restaurant.id)

    return {
        "restaurant": {"id": restaurant.id, "name": restaurant.name},
        "deals": [
            {
                "title": p.title,
                "description": p.description,
                "discount": _format_discount(p),
                # ISO dates so the model can quote them verbatim if the customer
                # asks "how long is this deal on?" — no clever date arithmetic.
                "valid_from": p.valid_from.isoformat(),
                "valid_to": p.valid_to.isoformat(),
                # A populated list means the deal is on specific items only,
                # not the whole menu — the model should say so.
                "applies_to_specific_items": bool(p.applicable_menu_item_ids),
            }
            for p in promotions
        ],
    }


def _format_discount(promo) -> str:
    """Customer-readable discount string — 'Rs. 500 off' or '20% off (up to Rs. 500)'.
    The model may quote this verbatim; the price effect at place_order is a
    separate follow-up so this is marketing copy only, no math guarantee."""
    from app.models import CouponDiscountType

    if promo.discount_type == CouponDiscountType.FIXED:
        return f"Rs. {promo.discount_value:.0f} off"
    # PERCENTAGE
    cap = (
        f" (up to Rs. {promo.max_discount_amount:.0f})"
        if promo.max_discount_amount is not None
        else ""
    )
    return f"{promo.discount_value:.0f}% off{cap}"


def suggest_addons(db: Session, conversation: Conversation) -> dict:
    """One contextual upsell for the customer's current cart, at most.

    Returns a `suggestion_type` of "promotion" | "addon" | "none":
      * "promotion" — mention the promo verbatim (quote the title + discount).
      * "addon"     — mention as a light nudge ("want some Loaded Fries with that?").
      * "none"      — say nothing. Do NOT invent something to suggest.

    Priority is promotion > addon > none — see services/upsell.py. The
    "at most one per turn" rule is a joint contract: this tool ALWAYS
    returns at most one suggestion (structural), and the prompt tells the
    model to call this at most once per customer turn (behavioural).
    """
    restaurant_id = cart_restaurant(conversation)
    if restaurant_id is None:
        return {
            "suggestion_type": "none",
            "note": (
                "Cart is empty — nothing to upsell yet. Do not fabricate a "
                "suggestion; just carry on with the order flow."
            ),
        }

    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None:
        return {"suggestion_type": "none"}

    lines = (conversation.cart or {}).get("items", [])
    cart_item_ids = [int(line["menu_item_id"]) for line in lines]

    pick = upsell_service.pick_for_cart(db, restaurant.id, cart_item_ids)

    payload = {
        "restaurant": {"id": restaurant.id, "name": restaurant.name},
        "suggestion_type": pick.kind,
    }
    if pick.kind == "promotion":
        p = pick.promotion
        payload["promotion"] = {
            "title": p.title,
            "discount": _format_discount(p),
            "valid_to": p.valid_to.isoformat(),
        }
    elif pick.kind == "addon":
        a = pick.addon
        payload["addon"] = {
            "id": a.id,
            "name": a.name,
            "price": _money(a.price),
            "category": a.category.name if a.category else None,
        }
    return payload


def add_to_cart(
    db: Session,
    conversation: Conversation,
    menu_item_id: int,
    quantity: int = 1,
    notes: str | None = None,
) -> dict:
    if quantity < 1 or quantity > MAX_QUANTITY_PER_LINE:
        return {"error": f"Quantity must be between 1 and {MAX_QUANTITY_PER_LINE}."}

    # The model has been observed inventing plausible-looking ids (1, 2, 12345) and
    # ordering food from the wrong restaurant. An id is only valid if get_menu
    # actually showed it in this conversation.
    shown = set((conversation.context or {}).get("shown_menu_ids", []))
    if menu_item_id not in shown:
        return {
            "error": "unknown_item",
            "message": (
                "That item id was not on any menu you have shown the customer. "
                "Call get_menu for the restaurant they want, then use an id from its result."
            ),
        }

    item = db.get(MenuItem, menu_item_id)
    if item is None or not item.is_available:
        return {"error": f"Item {menu_item_id} is not available."}

    restaurant = db.get(Restaurant, item.restaurant_id)
    if restaurant is None or not is_open(restaurant):
        return {"error": "That restaurant is closed right now."}

    cart = dict(conversation.cart or {"items": []})
    lines: list[dict] = list(cart.get("items", []))

    # One order = one restaurant. Compare against the restaurant of the items ALREADY IN
    # THE CART — not conversation.active_restaurant_id, which get_menu overwrites. Using
    # the latter meant "add a pizza, then browse the biryani place, then add a biryani"
    # slipped straight past this guard and produced an order no kitchen could fulfil.
    cart_restaurant_id = cart_restaurant(conversation)
    if lines and cart_restaurant_id is not None and cart_restaurant_id != item.restaurant_id:
        return {
            "error": "cart_has_other_restaurant",
            "message": (
                "The cart already has items from a different restaurant. "
                "Ask the customer whether to clear the cart and start fresh."
            ),
        }

    # IMMUTABLE-STYLE UPDATE: never mutate a dict that is already inside
    # `conversation.cart`. In-place mutation (`line["quantity"] = new`) silently
    # bypasses SQLAlchemy's JSONB change detection — the mutation lands in the
    # attribute before the reassignment below, so the "new" cart value ends up
    # structurally identical to the current one and the UPDATE never fires.
    # Real customer impact (conv#643): quantity increments were lost, place_order
    # then saw the wrong cart total and hit below_minimum unexpectedly. Always
    # build a fresh dict for the changed line.
    new_lines: list[dict] = []
    matched = False
    for line in lines:
        if (
            not matched
            and line["menu_item_id"] == menu_item_id
            and line.get("notes") == notes
        ):
            new_lines.append(
                {**line, "quantity": min(line["quantity"] + quantity, MAX_QUANTITY_PER_LINE)}
            )
            matched = True
        else:
            new_lines.append(line)
    if not matched:
        new_lines.append(
            {
                "menu_item_id": item.id,
                # Pinned to the line, so the cart's restaurant cannot drift when the
                # customer browses somewhere else.
                "restaurant_id": item.restaurant_id,
                "name": item.name,
                "price": _money(item.price),  # snapshot, so later reprices don't move the cart
                "quantity": quantity,
                "notes": notes,
            }
        )

    conversation.cart = {"items": new_lines}
    # Belt-and-braces: if some future refactor slips in an in-place mutation
    # again, this call keeps the write from silently vanishing.
    flag_modified(conversation, "cart")
    conversation.active_restaurant_id = item.restaurant_id
    conversation.state = ConversationState.ORDERING
    lines = new_lines

    subtotal = sum(Decimal(line["price"]) * line["quantity"] for line in lines)
    return {
        "cart": lines,
        "subtotal": _money(subtotal),
        "restaurant": restaurant.name,
    }


DUPLICATE_ORDER_WINDOW = timedelta(minutes=10)


def _recent_identical_order(
    db: Session, conversation: Conversation, restaurant_id: int, lines: list[dict]
) -> Order | None:
    """An order for the same customer + restaurant + exact item lines, placed inside
    the dedupe window."""
    since = datetime.now(timezone.utc) - DUPLICATE_ORDER_WINDOW

    candidates = db.scalars(
        select(Order).where(
            Order.customer_id == conversation.customer_id,
            Order.restaurant_id == restaurant_id,
            Order.placed_at >= since,
            Order.status.notin_([OrderStatus.CANCELLED]),
        )
    ).all()

    wanted = sorted((line["menu_item_id"], line["quantity"]) for line in lines)
    for order in candidates:
        existing = sorted((i.menu_item_id, i.quantity) for i in order.items)
        if existing == wanted:
            return order
    return None


def clear_cart(db: Session, conversation: Conversation) -> dict:
    conversation.cart = {"items": []}
    conversation.active_restaurant_id = None
    conversation.state = ConversationState.BROWSING
    return {"cleared": True}


_PAYMENT_ACK_KEYWORDS = (
    # Any of these appearing in a recent OUTBOUND is proof the model has
    # already surfaced the choice to the customer, so a subsequent cod call
    # is deliberate rather than a silent default. Bilingual list — customers
    # here talk in Roman Urdu + English mixed.
    "payment",
    "cod",
    "cash on delivery",
    "jazzcash",
    "easypaisa",
    "online",
    "kis se karna",   # "which one to pay with" — the exact ask we prompt for
)


def _model_has_asked_payment(db: Session, conversation: Conversation) -> bool:
    """Did the model surface payment options to the customer in a recent
    OUTBOUND? If yes, a subsequent cod choice is theirs. If no, the model is
    about to silently default cod without asking — which is the bug we're
    guarding against."""
    from app.services import conversations as convo

    for msg in convo.recent_history(db, conversation, limit=8):
        if msg.direction != MessageDirection.OUTBOUND:
            continue
        lowered = (msg.content or "").lower()
        if any(kw in lowered for kw in _PAYMENT_ACK_KEYWORDS):
            return True
    return False


def _has_any_outbound(db: Session, conversation: Conversation) -> bool:
    """True if the conversation has ever produced an OUTBOUND. Used to
    skip the payment-ask guard for direct-tool test paths (which build a
    conversation without ever driving the model), so the guard only fires
    on real customer flows where the model has actually been talking."""
    from app.services import conversations as convo

    return any(
        msg.direction == MessageDirection.OUTBOUND
        for msg in convo.recent_history(db, conversation, limit=5)
    )


def place_order(
    db: Session,
    conversation: Conversation,
    delivery_address: str | None = None,
    payment_method: str = "cod",
    notes: str | None = None,
    coupon_code: str | None = None,
    link_to_order_number: str | None = None,
) -> dict:
    lines = list((conversation.cart or {}).get("items", []))
    if not lines:
        return {"error": "The cart is empty — nothing to order."}

    # Phase 7 — sequential linked orders. If the model passes a
    # link_to_order_number, resolve the parent order eagerly so we can
    # (a) fail early with a clear error the model can recover from, and
    # (b) reuse or mint the shared group id before we create the child.
    # Customer-scoped: a customer cannot link to another customer's order
    # even by guessing the number. Never relaxes the cart's cross-restaurant
    # guard — that stays firm; linking is purely a POST-place_order label.
    parent_for_link: Order | None = None
    if link_to_order_number:
        parent_for_link = db.scalar(
            select(Order).where(
                Order.customer_id == conversation.customer_id,
                Order.order_number == link_to_order_number.strip().upper(),
            )
        )
        if parent_for_link is None:
            return {
                "error": "linked_order_not_found",
                "message": (
                    f"No previous order {link_to_order_number!r} for this "
                    "customer. Place this order as an independent order (omit "
                    "link_to_order_number), or confirm the correct order "
                    "number before retrying."
                ),
            }

    # Guard against the "silent cod default" pattern: when there are multiple
    # payment methods on offer and the model picks cod without ever asking
    # the customer, refuse and force the model to ask on retry. Skipped when
    # the conversation has zero outbounds (implies a direct-tool call from a
    # test / seed, not a real flow — otherwise every test that direct-calls
    # place_order would fail).
    methods = available_methods()
    if (
        len(methods) > 1
        and payment_method == PaymentMethod.COD.value
        and _has_any_outbound(db, conversation)
        and not _model_has_asked_payment(db, conversation)
    ):
        method_list = ", ".join(m.value for m in methods)
        return {
            "error": "confirm_payment_method",
            "message": (
                f"Multiple payment methods are available ({method_list}). "
                "Ask the customer which one they want (mention cod / jazzcash "
                "/ easypaisa by name), wait for their answer, then call "
                "place_order again with the payment_method they chose."
            ),
        }

    # The cart's restaurant, NOT the last one browsed. Using active_restaurant_id here
    # would bill a pizza order to whichever restaurant the customer looked at most
    # recently.
    restaurant = db.get(Restaurant, cart_restaurant(conversation) or 0)

    # Re-checked at the moment of ordering: a kitchen can close between building the
    # cart and confirming it.
    if restaurant is None or not is_open(restaurant):
        return {"error": "That restaurant has closed and can no longer take this order."}

    # Last line of defence against a double-charge. The model has been seen
    # re-running the whole order flow when it loses track of what it already did
    # (e.g. the customer asks "where's my order?"). If an identical order for this
    # customer landed moments ago, hand back that one instead of creating another.
    duplicate = _recent_identical_order(db, conversation, restaurant.id, lines)
    if duplicate is not None:
        return {
            "duplicate_prevented": True,
            "order_number": duplicate.order_number,
            "restaurant": restaurant.name,
            "total": _money(duplicate.total_amount),
            "status": duplicate.status.value,
            "message": (
                "This exact order was already placed a moment ago. Do NOT place it again — "
                "just tell the customer their existing order number and status."
            ),
        }

    customer = conversation.customer

    address_text = delivery_address
    if not address_text:
        default = next((a for a in customer.addresses if a.is_default), None)
        address_text = default.address_text if default else None
    if not address_text:
        conversation.state = ConversationState.AWAITING_ADDRESS
        return {"error": "missing_address", "message": "Ask the customer for a delivery address."}

    # Prices come from the cart snapshot, never from the model's arguments.
    subtotal = sum(Decimal(line["price"]) * line["quantity"] for line in lines)

    if subtotal < restaurant.min_order_amount:
        return {
            "error": "below_minimum",
            "message": (
                f"Order is Rs. {_money(subtotal)} but {restaurant.name} has a "
                f"minimum of Rs. {_money(restaurant.min_order_amount)}."
            ),
        }

    # Coupon discount is computed SERVER-SIDE from the DB, never trusted from the
    # model. The platform funds the discount, not the restaurant: the restaurant's
    # own commission math below is unaffected by this except that the platform's cut
    # is reduced by the same amount.
    applied_coupon = None
    discount_amount = Decimal("0.00")
    if coupon_code:
        try:
            application = coupons_service.validate_coupon(
                db,
                code=coupon_code,
                restaurant_id=restaurant.id,
                customer_id=customer.id,
                subtotal=subtotal,
            )
        except coupons_service.CouponError as exc:
            return {"error": "invalid_coupon", "message": str(exc)}
        applied_coupon = application.coupon
        discount_amount = application.discount_amount

    delivery_fee = restaurant.delivery_fee
    total = subtotal + delivery_fee - discount_amount
    commission_rate = restaurant.commission_rate
    # Commission on the FULL subtotal first, then reduced by the discount — the
    # restaurant is paid as if no coupon existed. Clamped at zero: a coupon bigger
    # than the commission means the platform is paying to acquire the order, which
    # is a legitimate choice, but platform revenue must never go negative for it.
    raw_commission = (subtotal * commission_rate / Decimal("100")).quantize(Decimal("0.01"))
    commission_amount = max(raw_commission - discount_amount, Decimal("0.00"))

    try:
        method = PaymentMethod(payment_method.lower())
    except ValueError:
        return {"error": f"Unknown payment method {payment_method!r}."}

    if method not in available_methods():
        return {
            "error": "unavailable_payment_method",
            "message": (
                f"{method.value} is not available. "
                f"Offer: {', '.join(m.value for m in available_methods())}."
            ),
        }

    # A prepaid order must NOT reach the kitchen until the money lands.
    prepaid = method != PaymentMethod.COD
    initial_status = OrderStatus.AWAITING_PAYMENT if prepaid else OrderStatus.PENDING

    order = Order(
        order_number=generate_order_number(),
        customer_id=customer.id,
        restaurant_id=restaurant.id,
        delivery_address_text=address_text,
        status=initial_status,
        payment_method=method,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        discount_amount=discount_amount,
        total_amount=total,
        commission_rate=commission_rate,
        commission_amount=commission_amount,
        notes=notes,
    )

    # Apply the link BEFORE db.flush() so the child row lands with its
    # group id already set — no second UPDATE needed, one atomic write.
    if parent_for_link is not None:
        if parent_for_link.order_group_id:
            # Third-or-later linked order: reuse the existing group id.
            order.order_group_id = parent_for_link.order_group_id
        else:
            # First link between two previously-independent orders: mint a
            # new group and back-fill the parent so both rows carry the
            # same identifier and dashboards can group them.
            group_id = f"GRP-{secrets.token_hex(3).upper()}"
            parent_for_link.order_group_id = group_id
            order.order_group_id = group_id
    order.items = [
        OrderItem(
            menu_item_id=line["menu_item_id"],
            item_name=line["name"],
            price_at_order=Decimal(line["price"]),
            quantity=line["quantity"],
            line_total=Decimal(line["price"]) * line["quantity"],
            notes=line.get("notes"),
        )
        for line in lines
    ]
    order.status_history = [OrderStatusHistory(status=initial_status, changed_by="ai")]

    db.add(order)
    db.flush()

    # Recorded in the SAME transaction as the order — a coupon must never be
    # validated in one moment and applied to a different order than the one it was
    # checked against.
    if applied_coupon is not None:
        coupons_service.record_redemption(
            db,
            coupon=applied_coupon,
            order=order,
            customer_id=customer.id,
            amount_discounted=discount_amount,
        )

    conversation.cart = {"items": []}
    conversation.active_restaurant_id = None
    conversation.state = ConversationState.ORDER_PLACED

    context = dict(conversation.context or {})
    context["last_order_number"] = order.order_number
    conversation.context = context

    result = {
        "order_number": order.order_number,
        "restaurant": restaurant.name,
        "subtotal": _money(subtotal),
        "delivery_fee": _money(delivery_fee),
        "discount_amount": _money(discount_amount),
        "total": _money(total),
        "payment_method": method.value,
        "delivery_address": address_text,
        "status": order.status.value,
    }
    if applied_coupon is not None:
        result["coupon_code"] = applied_coupon.code
    if order.order_group_id is not None:
        # Surface the group id so the model can confirm to the customer
        # ("your two orders are linked as GRP-XXXXXX") — matches how the
        # dashboards will group them.
        result["order_group_id"] = order.order_group_id
        result["linked_to_order_number"] = (
            parent_for_link.order_number if parent_for_link else None
        )

    if not prepaid:
        return result

    payment, link = start_payment(db, order, provider_for_method(method))
    result["payment_link"] = link
    result["payment_expires_in_minutes"] = settings.payment_expiry_minutes
    # Spelled out for the model, because getting this wrong means telling the customer
    # their food is on the way when nobody has paid and no kitchen has seen the order.
    result["message"] = (
        f"The order is NOT confirmed yet — it is awaiting payment and the restaurant "
        f"cannot see it. Send the customer this payment link and tell them the order is "
        f"confirmed once they have paid: {link} "
        f"(expires in {settings.payment_expiry_minutes} minutes)."
    )
    return result


def get_order_status(
    db: Session, conversation: Conversation, order_number: str | None = None
) -> dict:
    customer = conversation.customer

    stmt = select(Order).where(Order.customer_id == customer.id)
    if order_number:
        stmt = stmt.where(Order.order_number == order_number.strip().upper())
    order = db.scalar(stmt.order_by(Order.id.desc()).limit(1))

    if order is None:
        return {"error": "No matching order found for this customer."}

    return {
        "order_number": order.order_number,
        "restaurant": order.restaurant.name,
        "status": order.status.value,
        "total": _money(order.total_amount),
        "placed_at": order.placed_at.isoformat(),
        "items": [
            {"name": i.item_name, "quantity": i.quantity, "line_total": _money(i.line_total)}
            for i in order.items
        ],
    }


# --------------------------------------------------------------------------- #
# Favorite restaurants
# --------------------------------------------------------------------------- #


def add_favorite(
    db: Session,
    conversation: Conversation,
    restaurant_id: int | str | None = None,
    restaurant_name: str | None = None,
) -> dict:
    restaurant = _resolve_restaurant(db, restaurant_id, restaurant_name)
    if restaurant is None:
        return {
            "error": "unknown_restaurant",
            "message": "No restaurant matched that. Call list_restaurants and use an id or name from the result.",
        }

    customer_id = conversation.customer_id
    existing = db.scalar(
        select(CustomerFavorite).where(
            CustomerFavorite.customer_id == customer_id,
            CustomerFavorite.restaurant_id == restaurant.id,
        )
    )
    if existing is None:
        # Idempotent: favoriting the same restaurant twice must not create a second
        # row (and the unique constraint would reject it anyway) — just confirm it's
        # already there.
        db.add(CustomerFavorite(customer_id=customer_id, restaurant_id=restaurant.id))
        db.flush()

    return {"favorited": True, "restaurant": restaurant.name}


def remove_favorite(
    db: Session,
    conversation: Conversation,
    restaurant_id: int | str | None = None,
    restaurant_name: str | None = None,
) -> dict:
    restaurant = _resolve_restaurant(db, restaurant_id, restaurant_name)
    if restaurant is None:
        return {
            "error": "unknown_restaurant",
            "message": "No restaurant matched that. Call list_restaurants and use an id or name from the result.",
        }

    existing = db.scalar(
        select(CustomerFavorite).where(
            CustomerFavorite.customer_id == conversation.customer_id,
            CustomerFavorite.restaurant_id == restaurant.id,
        )
    )
    if existing is not None:
        db.delete(existing)
        db.flush()

    return {"removed": True, "restaurant": restaurant.name}


def list_favorites(db: Session, conversation: Conversation) -> dict:
    rows = db.scalars(
        select(Restaurant)
        .join(CustomerFavorite, CustomerFavorite.restaurant_id == Restaurant.id)
        .where(CustomerFavorite.customer_id == conversation.customer_id)
        .order_by(Restaurant.name)
    ).all()

    if not rows:
        return {"favorites": [], "note": "No favorite restaurants saved yet."}

    return {
        "favorites": [
            {
                "id": r.id,
                "name": r.name,
                "cuisine": r.cuisine_type,
                "is_open": is_open(r) if r.status == RestaurantStatus.ACTIVE else False,
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------------- #
# Past-order search (memory / phrase-driven repeat)
# --------------------------------------------------------------------------- #


MAX_PAST_ORDER_CANDIDATES = 5


def find_past_order(
    db: Session, conversation: Conversation, query: str
) -> dict:
    """Look up this customer's OWN past orders for a keyword match.

    Purpose: turn phrases like "my last biryani", "the Eid order", "same as
    office lunch" into a specific past order the model can then reorder or
    quote. Matches ILIKE against `Order.notes` (free-text) and the item names
    on that order — everything the customer might reference.

    Returns up to MAX_PAST_ORDER_CANDIDATES orders newest-first. The model is
    expected to:
      - use `reorder_last`-style rebuild logic if exactly one candidate comes
        back (or ask the customer to confirm the exact order number first),
      - ask a clarifying question if 2+ candidates come back, and
      - offer to build a fresh order if 0 candidates come back.

    Never returns other customers' orders — scoped strictly to
    `conversation.customer_id`. That scoping is the ONLY privacy boundary and
    must never be relaxed.
    """
    trimmed = (query or "").strip()
    if not trimmed:
        return {"error": "Give a word from the past order to search for."}

    like = f"%{trimmed}%"

    # DISTINCT on order id — an order matching by both notes and an item name
    # would otherwise appear twice.
    order_ids = db.scalars(
        select(Order.id)
        .outerjoin(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.customer_id == conversation.customer_id,
            (Order.notes.ilike(like)) | (OrderItem.item_name.ilike(like)),
        )
        .order_by(Order.id.desc())
        .distinct()
        .limit(MAX_PAST_ORDER_CANDIDATES)
    ).all()

    if not order_ids:
        return {
            "query": trimmed,
            "candidates": [],
            "note": (
                f"No past order matches '{trimmed}'. Ask what the customer wants "
                "instead, or offer to start a fresh order."
            ),
        }

    # Second query for the full rows — keeps the DISTINCT-limit correct and lets
    # SQLAlchemy load items/restaurant relationships eagerly per candidate.
    orders = db.scalars(
        select(Order)
        .where(Order.id.in_(order_ids))
        .order_by(Order.id.desc())
    ).all()

    return {
        "query": trimmed,
        "candidates": [
            {
                "order_number": o.order_number,
                "restaurant": o.restaurant.name,
                "restaurant_id": o.restaurant_id,
                "placed_at": o.placed_at.isoformat(),
                "total": _money(o.total_amount),
                "status": o.status.value,
                "notes": o.notes,
                "items": [
                    {"name": i.item_name, "quantity": i.quantity}
                    for i in o.items
                ],
            }
            for o in orders
        ],
    }


# --------------------------------------------------------------------------- #
# Reorder last order
# --------------------------------------------------------------------------- #


def reorder_last(db: Session, conversation: Conversation) -> dict:
    """Rebuild the cart from the customer's most recent order.

    Never resurrects a stale price or a stale name: every line is re-added through
    `get_menu` + `add_to_cart`, exactly the tools a human-driven order would use, so
    it goes through the SAME grounding guard and the SAME current-price lookup. If
    the menu has moved on — an item deleted, marked unavailable, or repriced — that
    item is reported as dropped rather than silently re-ordered at its old price.
    """
    customer = conversation.customer

    last_order = db.scalar(
        select(Order)
        .where(Order.customer_id == customer.id)
        .order_by(Order.id.desc())
        .limit(1)
    )
    if last_order is None:
        return {
            "error": "no_previous_order",
            "message": "This customer has no previous orders to reorder.",
        }

    restaurant = db.get(Restaurant, last_order.restaurant_id)
    if restaurant is None or restaurant.status != RestaurantStatus.ACTIVE or not is_open(restaurant):
        name = restaurant.name if restaurant is not None else "That restaurant"
        return {
            "error": "restaurant_closed",
            "message": (
                f"{name} is closed right now, so the last order (from {last_order.order_number}) "
                "cannot be repeated. Offer the customer another restaurant instead."
            ),
        }

    # This populates conversation.context["shown_menu_ids"]/["shown_menu"] exactly the
    # way a customer browsing the menu would — the grounding guard in add_to_cart is
    # never weakened or bypassed for a reorder.
    menu_result = get_menu(db, conversation, restaurant_id=restaurant.id)
    if "error" in menu_result:
        return menu_result

    current_items = {item["id"]: item for item in menu_result["items"]}

    clear_cart(db, conversation)

    added: list[dict] = []
    dropped: list[dict] = []
    for line in last_order.items:
        if line.menu_item_id is None or line.menu_item_id not in current_items:
            dropped.append({"name": line.item_name, "reason": "no longer on the menu"})
            continue

        result = add_to_cart(
            db,
            conversation,
            menu_item_id=line.menu_item_id,
            quantity=line.quantity,
            notes=line.notes,
        )
        if "error" in result:
            dropped.append(
                {"name": line.item_name, "reason": result.get("message", result["error"])}
            )
            continue

        current = current_items[line.menu_item_id]
        added.append({"name": current["name"], "quantity": line.quantity, "price": current["price"]})

    if not added:
        return {
            "error": "nothing_to_reorder",
            "message": (
                f"None of the items from order {last_order.order_number} are still "
                "available — the customer will need to build a new order."
            ),
            "dropped": dropped,
        }

    cart_lines = (conversation.cart or {}).get("items", [])
    subtotal = sum(Decimal(l["price"]) * l["quantity"] for l in cart_lines)

    return {
        "restaurant": restaurant.name,
        "added": added,
        "dropped": dropped,
        "subtotal": _money(subtotal),
        "message": (
            "Cart rebuilt from the last order. Read it back to the customer before placing it."
            if not dropped
            else (
                "Cart rebuilt from the last order, but some items could not be re-added "
                "— tell the customer plainly what changed before reading back the rest."
            )
        ),
    }


TOOL_IMPLS = {
    "list_restaurants": list_restaurants,
    "search_restaurants_by_item": search_restaurants_by_item,
    "find_restaurants": find_restaurants,
    "get_menu": get_menu,
    "list_active_deals": list_active_deals,
    "suggest_addons": suggest_addons,
    "add_to_cart": add_to_cart,
    "clear_cart": clear_cart,
    "place_order": place_order,
    "get_order_status": get_order_status,
    "find_past_order": find_past_order,
    "add_favorite": add_favorite,
    "remove_favorite": remove_favorite,
    "list_favorites": list_favorites,
    "reorder_last": reorder_last,
}
