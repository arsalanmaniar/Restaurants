"""Recording an order rating.

Lives in a service rather than a route because the WhatsApp flow will call it too
(the AI will ask "how was your food?" after delivery), and both callers must obey
the same rules:

  * only the customer who placed the order may rate it
  * only a DELIVERED order may be rated — rating food you haven't received is
    meaningless, and rating a cancelled order is a way to punish a restaurant for
    something it didn't do
  * one rating per order
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderRating, OrderStatus


class RatingError(Exception):
    """Raised with a customer-safe message."""


def record_rating(
    db: Session,
    *,
    order: Order,
    customer_id: int,
    rating: int,
    comment: str | None = None,
    source: str = "whatsapp",
) -> OrderRating:
    if order.customer_id != customer_id:
        raise RatingError("That order belongs to someone else.")

    if order.status != OrderStatus.DELIVERED:
        raise RatingError(
            f"Order {order.order_number} hasn't been delivered yet, so it can't be rated."
        )

    if not 1 <= rating <= 5:
        raise RatingError("Rating must be between 1 and 5.")

    existing = db.scalar(select(OrderRating).where(OrderRating.order_id == order.id))
    if existing is not None:
        raise RatingError(f"Order {order.order_number} has already been rated.")

    entry = OrderRating(
        order_id=order.id,
        restaurant_id=order.restaurant_id,
        customer_id=customer_id,
        rating=rating,
        comment=comment,
        source=source,
    )
    db.add(entry)
    db.flush()
    return entry
