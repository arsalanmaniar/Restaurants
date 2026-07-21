"""Intent-based restaurant discovery.

One thing the AI has repeatedly needed: turn a customer's own phrasing —
"pizza chahiye", "something spicy", "chinese", "light dinner", "family
meal" — into a shortlist of relevant restaurants, without the customer
having to know a restaurant's name.

The previous discovery surface was two tools:

  * `list_restaurants(cuisine=None)` — browse everything, optional cuisine
    substring on `Restaurant.cuisine_type`.
  * `search_restaurants_by_item(query)` — substring match on
    `MenuItem.name` only.

`search_restaurants_by_item` missed too much: "chinese" doesn't appear in
any MenuItem row for Wok & Roll (whose items are "Chowmein", "Egg Fried
Rice", …), and "spicy" only shows up inside `MenuItem.description`, never
in the name. So the old tool would return zero for those and the model
was left guessing.

`find_matching_restaurants` here searches five columns at once —
restaurant name, cuisine, description, menu item name, menu item
description — and passes the same matched_items signal that Phase 2's
ranking already knows how to weight (see services/ranking.py). Any
restaurant that matches ANYTHING gets full relevance credit (score += 3.0),
so a "chinese" query beats a random daily rotation for Wok & Roll even
though no MenuItem row literally contains the word "chinese".

For any restaurant that matches only on cuisine/description (no menu-item
hit), matched_items falls back to the restaurant's cuisine text — that
way the ranking's reason string reads "serves Chinese; …", which is
truthful and quotable, rather than exposing an internal "matched via
description" tag.
"""

from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import MenuItem, Restaurant, RestaurantStatus
from app.services.opening_hours import is_open

MAX_MATCHED_ITEMS_PER_RESTAURANT = 5


def find_matching_restaurants(
    db: Session, query: str,
) -> tuple[dict[int, Restaurant], dict[int, list[str]]]:
    """Every open restaurant whose name/cuisine/description or menu item
    name/description matches the query, plus the customer-visible matched
    items per restaurant.

    Returns (`{restaurant_id: Restaurant}`, `{restaurant_id: [matched_item, …]}`).
    Callers hand matched_items straight to `ranking.rank_restaurants` so
    every matched restaurant scores relevance=1.0 (see services/ranking.py).
    """
    trimmed = (query or "").strip()
    if not trimmed:
        return {}, {}

    like = f"%{trimmed}%"

    restaurants_by_id: dict[int, Restaurant] = {}
    matched_items: dict[int, list[str]] = {}

    # Menu item matches first — a concrete dish name is the strongest,
    # most quotable signal, so we prefer real item names in matched_items
    # over a fallback to cuisine text.
    menu_rows = db.execute(
        select(Restaurant, MenuItem.name)
        .join(MenuItem, MenuItem.restaurant_id == Restaurant.id)
        .where(
            Restaurant.status == RestaurantStatus.ACTIVE,
            Restaurant.is_accepting_orders.is_(True),
            MenuItem.is_available.is_(True),
            or_(
                MenuItem.name.ilike(like),
                MenuItem.description.ilike(like),
            ),
        )
        .order_by(Restaurant.id, MenuItem.name)
    ).all()
    for r, item_name in menu_rows:
        restaurants_by_id.setdefault(r.id, r)
        items = matched_items.setdefault(r.id, [])
        if item_name not in items and len(items) < MAX_MATCHED_ITEMS_PER_RESTAURANT:
            items.append(item_name)

    # Restaurant-level fields — cuisine, name, description. Any restaurant
    # that already got picked up via menu keeps its menu-item matched_items
    # unchanged; new arrivals get their cuisine text as the match signal.
    restaurant_hits = db.scalars(
        select(Restaurant).where(
            Restaurant.status == RestaurantStatus.ACTIVE,
            Restaurant.is_accepting_orders.is_(True),
            or_(
                Restaurant.name.ilike(like),
                Restaurant.cuisine_type.ilike(like),
                Restaurant.description.ilike(like),
            ),
        )
    ).all()
    for r in restaurant_hits:
        restaurants_by_id.setdefault(r.id, r)
        if not matched_items.get(r.id):
            # Cuisine text is always truthful and reads well in the ranking
            # reason ("serves Chinese; rated 4.5/5"). Never leak internal
            # tags like "matched via description".
            matched_items[r.id] = [r.cuisine_type]

    # Never offer a dark kitchen — same "restaurant is closed right now"
    # rule as list_restaurants / search_restaurants_by_item.
    open_only = {
        rid: r for rid, r in restaurants_by_id.items() if is_open(r)
    }
    return open_only, {rid: matched_items[rid] for rid in open_only}


def _cheapest_available_item(db: Session, restaurant_id: int) -> MenuItem | None:
    """Cheapest available menu item for one restaurant. Fallback when a
    query matched at the restaurant level (cuisine / description) but no
    concrete menu item — we still need SOMETHING to base a budget estimate
    on, so we use the price floor as a lower bound."""
    return db.scalar(
        select(MenuItem)
        .where(
            MenuItem.restaurant_id == restaurant_id,
            MenuItem.is_available.is_(True),
        )
        .order_by(MenuItem.price, MenuItem.id)
        .limit(1)
    )


def estimate_meal_cost(
    *,
    matched_menu_items: list[MenuItem],
    delivery_fee: Decimal,
    min_order_amount: Decimal,
    party_size: int = 1,
) -> dict | None:
    """Representative meal-cost estimate for one restaurant.

    Formula: cheapest matched item × party_size + delivery, clamped to the
    restaurant's minimum order amount (a real order has to clear that).
    Returns None when there's nothing to base an estimate on — the caller
    then omits the estimate rather than fabricating a number.

    Deliberately a LOWER bound of a realistic order, not an average — the
    customer's own message ("Rs. 1500 mein kya milega?") wants a "can I
    fit?" answer, not a "will I definitely fit?" one. Model surfaces this
    honestly; the actual place_order total may go higher if the customer
    adds more.
    """
    if not matched_menu_items:
        return None

    party_size = max(1, int(party_size or 1))
    primary = min(matched_menu_items, key=lambda i: i.price)

    food = primary.price * party_size
    # A real order has to clear the restaurant's minimum, so the estimate
    # can't sit below it — otherwise the model would tell the customer
    # "fits in Rs. 300" and place_order would then error with below_minimum.
    if food < min_order_amount:
        food = min_order_amount
    total = food + delivery_fee

    return {
        "primary_item": {
            "name": primary.name,
            "price": f"{primary.price:.2f}",
        },
        "party_size": party_size,
        "food_estimate": f"{food:.2f}",
        "delivery_fee": f"{delivery_fee:.2f}",
        "estimated_total": f"{total:.2f}",
    }
