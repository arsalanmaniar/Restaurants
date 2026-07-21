"""Time-bound restaurant promotions.

Two things the whole rest of the app relies on this module to guarantee:

  * "active" is a live check — always re-computed from `now Asia/Karachi` vs
    the promotion's `valid_from` / `valid_to`, never a cached flag. A promo
    "auto-expires" the moment we cross midnight PKT past its `valid_to`; the
    ORM `is_active` flag is only the manual override on top of the date check.
  * A promotion is ALWAYS restaurant-scoped. There is no platform-wide
    "sitewide sale" here — that would be a coupon.

For MVP the promotion is INFORMATIONAL: the AI tool `list_active_deals`
surfaces it in conversation. Auto-applying at place_order (which touches
coupon math + commission) is a deliberate follow-up so this phase can ship
without destabilising the working payment flow.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Promotion
from app.services.opening_hours import PAKISTAN_TZ


class PromotionError(Exception):
    """Raised for validation failures the caller can surface to a human. Same
    contract as CouponError — safe to relay verbatim."""


def _today(at: datetime | None = None) -> date:
    """The promotion's date window is interpreted in Karachi calendar days —
    "valid through 20 July" means until the END of 20 July local time, not
    UTC midnight (which is 5am PKT — the middle of the dinner rush)."""
    return (at or datetime.now(PAKISTAN_TZ)).astimezone(PAKISTAN_TZ).date()


def is_active_at(promotion: Promotion, at: datetime | None = None) -> bool:
    """Whether the promotion is running right now (or at the supplied moment).

    Three gates in order:
      1. `is_active` (manual off-switch — beats the date window either way)
      2. today >= valid_from  (not started yet -> hidden)
      3. today <= valid_to    (already ended  -> hidden)
    """
    if not promotion.is_active:
        return False
    today = _today(at)
    if today < promotion.valid_from:
        return False
    if today > promotion.valid_to:
        return False
    return True


def list_active_for_restaurant(
    db: Session,
    restaurant_id: int,
    *,
    at: datetime | None = None,
) -> list[Promotion]:
    """Every promotion currently running for one restaurant, newest-first.

    The date filter is applied in Python because Postgres does not know about
    Asia/Karachi (`_today` handles that), and there are typically 0-3 promos
    per restaurant so a Python filter costs nothing. If per-restaurant promo
    counts ever explode this can become a `WHERE valid_to >= ...` query with
    a small clock-skew tolerance.
    """
    candidates = db.scalars(
        select(Promotion)
        .where(Promotion.restaurant_id == restaurant_id)
        .order_by(Promotion.id.desc())
    ).all()
    return [p for p in candidates if is_active_at(p, at)]


def validate_new_promotion(
    *,
    title: str,
    discount_value,
    valid_from: date,
    valid_to: date,
    min_order_amount=None,
    max_discount_amount=None,
) -> None:
    """Cheap sanity checks that would otherwise show up as an IntegrityError
    downstream (harder to give the restaurant a useful error message for)."""
    if not title.strip():
        raise PromotionError("Title cannot be empty.")
    if valid_from > valid_to:
        raise PromotionError("valid_from cannot be after valid_to.")
    if discount_value is None or discount_value <= 0:
        raise PromotionError("Discount value must be greater than zero.")
    if min_order_amount is not None and min_order_amount < 0:
        raise PromotionError("Minimum order amount cannot be negative.")
    if max_discount_amount is not None and max_discount_amount < 0:
        raise PromotionError("Maximum discount amount cannot be negative.")
