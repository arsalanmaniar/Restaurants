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
