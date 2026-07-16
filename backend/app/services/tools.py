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
from app.services.opening_hours import is_open
from app.services.payments.registry import PROVIDER_FOR_METHOD, available_methods
from app.services.payments.service import start_payment

from app.models import (
    Conversation,
    ConversationState,
    CustomerFavorite,
    MenuCategory,
    MenuItem,
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

    return {
        "restaurants": [
            {
                "id": r.id,
                "name": r.name,
                "cuisine": r.cuisine_type,
                "delivery_fee": _money(r.delivery_fee),
                "min_order": _money(r.min_order_amount),
            }
            for r in restaurants
        ]
    }


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


def place_order(
    db: Session,
    conversation: Conversation,
    delivery_address: str | None = None,
    payment_method: str = "cod",
    notes: str | None = None,
    coupon_code: str | None = None,
) -> dict:
    lines = list((conversation.cart or {}).get("items", []))
    if not lines:
        return {"error": "The cart is empty — nothing to order."}

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

    if not prepaid:
        return result

    payment, link = start_payment(db, order, PROVIDER_FOR_METHOD[method])
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
    "get_menu": get_menu,
    "add_to_cart": add_to_cart,
    "clear_cart": clear_cart,
    "place_order": place_order,
    "get_order_status": get_order_status,
    "add_favorite": add_favorite,
    "remove_favorite": remove_favorite,
    "list_favorites": list_favorites,
    "reorder_last": reorder_last,
}
