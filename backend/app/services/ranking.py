"""Restaurant ranking used by list_restaurants / search_restaurants_by_item.

Replaces the previous alphabetical order (which pinned "Karachi Biryani House"
at the top forever) with a formula that combines three signals:

  score = 3 × relevance  +  2 × normalised_rating  +  rotation_seed

- relevance ∈ {0.0, 1.0}: 1 if the customer's search actually matched something
  concrete on this restaurant (e.g. an item name from search_restaurants_by_item),
  0 otherwise. Undifferentiated `list_restaurants(cuisine=None)` gives 0 to
  every candidate — rating + rotation carry the day.

- normalised_rating ∈ [0, 1]: (avg_rating − 1) ÷ 4, so a 5-star restaurant
  scores 1 and a 1-star scores 0. Restaurants with no ratings get 0.5 — the
  neutral prior, so a brand-new restaurant isn't punished until it has evidence
  either way.

- rotation_seed ∈ [0, 0.1): a small deterministic hash of (restaurant_id, day
  of year in Asia/Karachi). Its role is to break ties so the same restaurant
  isn't always first — the ordering is stable within a day (so a customer who
  reloads the chat sees the same order) but rotates day to day. Restaurants
  never randomly move around within one conversation.

The 3 : 2 : 0.1 ratio is deliberate: a relevance match (3.0) beats a perfect
rating (2.0) beats every rotation tiebreak (< 0.1). Nothing beats a real match.
"""
from __future__ import annotations

from datetime import date, datetime
from hashlib import blake2b
from typing import Iterable, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import OrderRating, Restaurant
from app.services.opening_hours import PAKISTAN_TZ


NEUTRAL_RATING_PRIOR = 0.5  # unrated restaurants sit at the middle of the range
ROTATION_MAX = 0.1          # small enough to never beat a real rating gap


class RankedRestaurant(NamedTuple):
    restaurant: Restaurant
    score: float
    reason: str


def _today_karachi(at: datetime | None = None) -> date:
    """Ranking rotates by CALENDAR day in Karachi, not UTC — otherwise the
    daily rotation would flip at 5am for local users, which feels random."""
    moment = (at or datetime.now(PAKISTAN_TZ)).astimezone(PAKISTAN_TZ)
    return moment.date()


def _rotation_seed(restaurant_id: int, day: date) -> float:
    """Deterministic per-(restaurant, day) small float in [0, ROTATION_MAX).
    blake2b (not built-in hash()) because Python's hash randomises across
    processes — that would make ranking non-reproducible across web workers."""
    key = f"{restaurant_id}:{day.toordinal()}".encode()
    digest = blake2b(key, digest_size=4).digest()
    fraction = int.from_bytes(digest, "big") / 0xFFFFFFFF
    return fraction * ROTATION_MAX


def _avg_ratings(db: Session, restaurant_ids: list[int]) -> dict[int, tuple[float, int]]:
    """{restaurant_id: (avg_rating, count)} for the given restaurants. Absent
    keys ⇒ zero ratings ⇒ the neutral prior applies. One SQL round-trip regardless
    of the candidate count, so ranking a batch is cheap."""
    if not restaurant_ids:
        return {}
    rows = db.execute(
        select(
            OrderRating.restaurant_id,
            func.avg(OrderRating.rating).label("avg"),
            func.count(OrderRating.id).label("count"),
        )
        .where(OrderRating.restaurant_id.in_(restaurant_ids))
        .group_by(OrderRating.restaurant_id)
    ).all()
    return {row.restaurant_id: (float(row.avg), int(row.count)) for row in rows}


def _normalise_rating(avg: float | None) -> float:
    if avg is None:
        return NEUTRAL_RATING_PRIOR
    # 1-5 → 0-1; clamp defensively against future scale changes
    return max(0.0, min(1.0, (avg - 1.0) / 4.0))


def _build_reason(
    matched_items: list[str] | None,
    avg_rating: float | None,
    rating_count: int,
) -> str:
    """Customer-friendly explanation of why this restaurant ranked where it did.
    The model may quote this verbatim if a customer asks 'why this one?'. Never
    exposes the formula, weights, or the rotation logic — just the human-visible
    signals ('serves biryani', '4.5 stars from 12 orders', 'featured today')."""
    parts: list[str] = []
    if matched_items:
        head = ", ".join(matched_items[:2])
        more = "" if len(matched_items) <= 2 else f" (+{len(matched_items) - 2} more)"
        parts.append(f"serves {head}{more}")
    if rating_count > 0 and avg_rating is not None:
        parts.append(f"rated {avg_rating:.1f}/5 from {rating_count} orders")
    if not parts:
        parts.append("featured today")
    return "; ".join(parts)


def rank_restaurants(
    db: Session,
    restaurants: Iterable[Restaurant],
    *,
    matched_items_by_id: dict[int, list[str]] | None = None,
    relevance_by_id: dict[int, float] | None = None,
    at: datetime | None = None,
) -> list[RankedRestaurant]:
    """Return restaurants in ranked order with a per-restaurant score and reason.

    `matched_items_by_id` is optional — pass it from search_restaurants_by_item
    so those matches carry the full relevance weight. list_restaurants passes
    nothing and every candidate scores 0 on relevance (leaving rating + rotation
    to decide the order). Ranking is stable within a day: same inputs → same
    output, no per-request randomness.

    `relevance_by_id` grades that match instead of treating every hit as
    equal. find_restaurants passes it so a restaurant that merely had the word
    somewhere in its description does not rank level with one that actually
    serves the dish (see discovery.RELEVANCE_BY_STRENGTH). Omit it and the
    old all-or-nothing behaviour applies unchanged, which is what keeps
    list_restaurants and search_restaurants_by_item as they were.
    """
    candidates = list(restaurants)
    if not candidates:
        return []

    matched_items_by_id = matched_items_by_id or {}
    relevance_by_id = relevance_by_id or {}
    ratings = _avg_ratings(db, [r.id for r in candidates])
    day = _today_karachi(at)

    ranked: list[RankedRestaurant] = []
    for r in candidates:
        matched = matched_items_by_id.get(r.id, [])
        relevance = relevance_by_id.get(r.id, 1.0 if matched else 0.0)
        avg, count = ratings.get(r.id, (None, 0))
        rating_normalised = _normalise_rating(avg)
        rotation = _rotation_seed(r.id, day)

        score = 3.0 * relevance + 2.0 * rating_normalised + rotation
        reason = _build_reason(matched, avg, count)
        ranked.append(RankedRestaurant(restaurant=r, score=score, reason=reason))

    # Sort descending. Ties within a day: rotation_seed already broke them.
    # If two restaurants somehow collide on rotation too, name is the final
    # tiebreak so the order is fully deterministic per (candidate set, day).
    ranked.sort(key=lambda rr: (-rr.score, rr.restaurant.name))
    return ranked
