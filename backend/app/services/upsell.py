"""One contextual upsell for a cart, at most.

The AI calls this after a successful `add_to_cart` to decide whether to
suggest *one* more thing to the customer. The suggestion is either:

  * an active promotion for the cart's restaurant (from Phase 3 data), OR
  * the cheapest available menu item from a category the cart doesn't yet
    cover — the data-driven definition of an "add-on", so nothing here has
    to know what "fries" or "drinks" mean, and the same code works for
    every restaurant the platform onboards, OR
  * nothing at all.

Priority: promotion beats add-on. If both exist the customer only hears
about the promotion — it's higher-value marketing and repeating both in
one turn feels pushy. The "at most one per turn" guarantee comes from
this shape (Suggestion.kind is a single tag), plus a prompt clause that
tells the model to call `suggest_addons` at most once per customer turn.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MenuItem, Promotion
from app.services import promotions as promotions_service


@dataclass
class Suggestion:
    """The tool's structured decision. Exactly one of `promotion` / `addon`
    is set when `kind` is not "none"; both are None when `kind == "none"`."""

    kind: str  # "promotion" | "addon" | "none"
    promotion: Promotion | None = None
    addon: MenuItem | None = None


def pick_for_cart(
    db: Session,
    restaurant_id: int,
    cart_item_ids: list[int],
) -> Suggestion:
    """The single upsell decision for one cart, or a "none" verdict.

    Never returns multiple options — the caller mentions whatever this
    picks, once, or says nothing if the kind is "none".
    """
    # 1. Any active promotion wins outright. Newest-first ordering already
    # applied by list_active_for_restaurant, so [0] is the freshest promo.
    active_promos = promotions_service.list_active_for_restaurant(db, restaurant_id)
    if active_promos:
        return Suggestion(kind="promotion", promotion=active_promos[0])

    # 2. Cheapest available item from a category the cart isn't already
    # covering. The "different category" heuristic is what makes this feel
    # like a real add-on and not "want another pizza with your pizza?".
    if not cart_item_ids:
        return Suggestion(kind="none")

    cart_categories = set(
        db.scalars(
            select(MenuItem.category_id).where(MenuItem.id.in_(cart_item_ids))
        ).all()
    )
    # NULL category items don't constrain the search — they just mean the
    # cart has an uncategorised item, which we ignore for add-on purposes.
    cart_categories.discard(None)

    stmt = select(MenuItem).where(
        MenuItem.restaurant_id == restaurant_id,
        MenuItem.is_available.is_(True),
        # Uncategorised items are never good add-ons — we can't reason about
        # complementarity if we don't know what family they're in.
        MenuItem.category_id.is_not(None),
        # Never suggest something already in the cart, even in an edge case
        # where the cart items were somehow uncategorised.
        MenuItem.id.notin_(cart_item_ids),
    )
    if cart_categories:
        stmt = stmt.where(MenuItem.category_id.notin_(cart_categories))

    # Cheapest first — a Rs. 300 fries with a Rs. 1750 pizza reads as a
    # nudge, not a re-negotiation. Ties broken by id for deterministic
    # output (tests, and the same customer twice in a row).
    candidate = db.scalar(stmt.order_by(MenuItem.price, MenuItem.id).limit(1))

    if candidate is None:
        return Suggestion(kind="none")

    return Suggestion(kind="addon", addon=candidate)
