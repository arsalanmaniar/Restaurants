"""One place that decides whether a reply is BACKED by something real.

Three separate guards grew here one production bug at a time — discovery padding,
the ungrounded restaurant list, and the unbacked bill. Each was written against
the shape of the failure in front of us, so each is blind outside that shape, and
every new fabrication needed a new guard. This module folds them into a single
auditor with one entry point (`audit`), one shared notion of "what may this reply
assert", and one call site in the agent.

The organising idea, which is what makes it more than a merge:

    A claim that depends on LIVE state must be backed by a tool call in THIS
    turn. A reference to already-established context does not.

Offering a list of restaurants is a live claim (who is open, who exists, who
serves this). Quoting a bill is a live claim (the numbers move with the cart and
the payment method). Mentioning in passing the restaurant the customer is already
ordering from is not.

One correction that shaped the design, worth recording so it is not undone:
grounding restaurant NAMES against conversation context does NOT work. In the
haleem failure (conv 715) the model reused the four restaurants from its own
greeting, so every name it printed WAS in `shown_restaurants` — the invented part
was the claim that they serve haleem, not the names. That is why the list rule
below demands a LISTING TOOL THIS TURN rather than merely a known name.

What this module deliberately does NOT do: judge dish-availability claims. The
difference between "haleem nahi hai" (correct, and our most important honest
answer) and "haleem hai" (fabrication) is one negation in loosely-spelled Roman
Urdu. Getting that wrong suppresses truthful replies, so it stays with the narrow
guards until it can be done properly.
"""

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Conversation,
    MenuItem,
    MessageDirection,
    Order,
    Restaurant,
    RestaurantStatus,
)

# Tools whose results may legitimately put a LIST of restaurants in front of a
# customer. get_menu is not one: it describes a single restaurant.
LISTING_TOOLS = ("list_restaurants", "find_restaurants", "search_restaurants_by_item")

# "1. Karachi Biryani House" / "2) Wok & Roll" — the numbered shape the prompt
# mandates for every restaurant list, in either language.
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.)]\s*(.+)$", re.MULTILINE)

# generate_order_number() produces "AB-" + 6 uppercase hex.
_ORDER_NUMBER_RE = re.compile(r"\bAB-[0-9A-F]{6}\b")

# Any "Rs. 1,422.50" style figure the reply states.
_MONEY_RE = re.compile(r"rs\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)

# A bill component line: a label followed by an Rs figure. The leading \b stops
# "total" matching inside "Subtotal", so a Subtotal line contributes only the
# `subtotal` label.
_BILL_LINE_RE = re.compile(
    r"\b(subtotal|tax|delivery|discount|total|kul)\b[^0-9\n]{0,20}rs\.?\s*[0-9]",
    re.IGNORECASE,
)

# Bilingual hedges — their presence means the model is flagging uncertainty
# rather than asserting, which the weak-match note explicitly asks for.
HEDGE_MARKERS = (
    "shayad", "ho sakta", "sure nahi", "confirm", "check kar", "menu dekh",
    "pata kar", "possib", "might", "not sure", "let me check", "verify",
)

_CENT = Decimal("0.01")


class Violation:
    """What was wrong, and what to tell the model about it."""

    def __init__(self, kind: str, nudge: str, force_tool: bool = False):
        self.kind = kind
        self.nudge = nudge
        self.force_tool = force_tool

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Violation {self.kind}>"


# --------------------------------------------------------------------------- #
# Shared extraction
# --------------------------------------------------------------------------- #


def active_restaurant_names(db: Session) -> list[str]:
    return list(
        db.scalars(
            select(Restaurant.name).where(Restaurant.status == RestaurantStatus.ACTIVE)
        ).all()
    )


def _tool_results(trace: list[dict], name: str) -> list[dict]:
    return [
        step["result"]
        for step in trace
        if step.get("tool") == name and isinstance(step.get("result"), dict)
    ]


def _names_from_listing_tools(trace: list[dict]) -> set[str]:
    """Restaurant names a LISTING tool returned this turn."""
    names: set[str] = set()
    for tool in LISTING_TOOLS:
        for result in _tool_results(trace, tool):
            for entry in result.get("restaurants") or []:
                if isinstance(entry, dict) and entry.get("name"):
                    names.add(entry["name"])
            # find_restaurants' selection path returns a single `restaurant`.
            restaurant = result.get("restaurant")
            if isinstance(restaurant, dict) and restaurant.get("name"):
                names.add(restaurant["name"])
    return names


def _names_in_play(db: Session, conversation: Conversation, trace: list[dict]) -> set[str]:
    """Every restaurant the reply may mention without offering it as a choice:
    anything a tool returned this turn, plus the restaurant already active."""
    names = _names_from_listing_tools(trace)
    for tool in ("get_menu", "preview_bill", "place_order", "get_order_status"):
        for result in _tool_results(trace, tool):
            restaurant = result.get("restaurant")
            if isinstance(restaurant, dict) and restaurant.get("name"):
                names.add(restaurant["name"])
            elif isinstance(restaurant, str) and restaurant:
                names.add(restaurant)
    if conversation.active_restaurant_id is not None:
        active = db.get(Restaurant, conversation.active_restaurant_id)
        if active is not None:
            names.add(active.name)
    return names


def _money_figures(text: str | None) -> list[Decimal]:
    out: list[Decimal] = []
    for raw in _MONEY_RE.findall(text or ""):
        try:
            out.append(Decimal(raw.replace(",", "")))
        except (InvalidOperation, ValueError):
            continue
    return out


def quoted_total(text: str | None) -> Decimal | None:
    """The figure the reply presents as the order Total, or None."""
    match = re.search(
        r"\b(?:total|kul)\b[^0-9]{0,20}rs\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        text or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def looks_like_a_bill(reply: str | None) -> bool:
    """Two or more DISTINCT bill component lines. Two, not one, because a lone
    "total Rs. 580" is how a budget estimate and an order-status reply legitimately
    talk — neither is a bill."""
    labels = {m.group(1).lower() for m in _BILL_LINE_RE.finditer(reply or "")}
    return len(labels) >= 2


def _preview_totals(trace: list[dict]) -> list[Decimal]:
    totals: list[Decimal] = []
    for result in _tool_results(trace, "preview_bill"):
        if result.get("total") is None:
            continue
        try:
            totals.append(Decimal(str(result["total"])))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return totals


# --------------------------------------------------------------------------- #
# Rule 1 — a restaurant list must come from a listing tool THIS TURN
# --------------------------------------------------------------------------- #


def _restaurant_list_violation(
    db: Session, reply: str, trace: list[dict]
) -> Violation | None:
    """Subsumes the old `_ungrounded_restaurant_list` AND `_discovery_padding`'s
    list case, and is stronger than either.

    The old detector COUNTED recognised names and fired at two or more, so a list
    mixing one real restaurant with one invented one ("1. Karachi Biryani House
    2. Lahore Tikka House") counted only one and escaped — the more fabricated the
    list, the less likely it was caught. Here, one recognised entry is enough to
    identify the reply as a restaurant list, and then EVERY entry must be grounded.
    """
    # Past-order enumeration legitimately names restaurants with no listing tool.
    if _ORDER_NUMBER_RE.search(reply) or _tool_results(trace, "get_order_status"):
        return None

    entries = [m.group(1).strip() for m in _NUMBERED_LINE_RE.finditer(reply)]
    # Two or more entries is what makes it an OFFER OF CHOICES. A single numbered
    # line naming a restaurant is a confirmation ("1. Pizza Junction — Chicken
    # Tikka Pizza"), not a shortlist, and blocking it was a regression the ported
    # tests caught.
    if len(entries) < 2:
        return None

    known = active_restaurant_names(db)
    # A numbered list is a RESTAURANT list if any entry names a real restaurant.
    # (A numbered menu — "1. Chicken Biryani — Rs. 450" — names none, so it is
    # left alone.)
    if not any(name.lower() in e.lower() for e in entries for name in known):
        return None

    grounded = _names_from_listing_tools(trace)
    ungrounded = [
        entry
        for entry in entries
        if not any(name.lower() in entry.lower() for name in grounded)
    ]
    if not ungrounded:
        return None

    if not grounded:
        return Violation(
            "unlisted_offer",
            "You listed restaurants without calling any listing tool this turn — "
            "that list came from memory, not from our catalogue, so it may be "
            "entirely wrong. Call find_restaurants with the dish or cuisine the "
            "customer asked about (or list_restaurants if they asked for "
            "everything), then answer ONLY from what it returns. If it finds "
            "nothing, say plainly that the item is not available and do not name "
            "any restaurant.",
            force_tool=True,
        )
    return Violation(
        "unlisted_offer",
        "Your list includes restaurants the search did not return: "
        f"{', '.join(repr(e) for e in ungrounded)}. List ONLY the restaurants the "
        "search actually returned — never add others from earlier in the chat or "
        "from memory. Rewrite the reply accordingly.",
    )


# --------------------------------------------------------------------------- #
# Rule 2 — search fidelity (the old `_discovery_padding`, for prose too)
# --------------------------------------------------------------------------- #


def _latest_find_result(trace: list[dict]) -> dict | None:
    results = _tool_results(trace, "find_restaurants")
    return results[-1] if results else None


def _search_fidelity_violation(
    db: Session, conversation: Conversation, reply: str, trace: list[dict]
) -> Violation | None:
    """A search ran and returned real options; the reply names a restaurant that
    was NOT among them. Covers prose as well as lists, which is why it stays
    alongside rule 1.

    Allowed names are masked out of the reply BEFORE scanning, so an allowed
    "Pizza Junction 2" cannot leave the absent "Pizza Junction" matching inside it.
    """
    result = _latest_find_result(trace)
    if not result or not result.get("restaurants"):
        return None

    masked = reply.lower()
    for name in _names_in_play(db, conversation, trace):
        masked = masked.replace(name.lower(), " ")
    strays = [name for name in active_restaurant_names(db) if name.lower() in masked]
    if not strays:
        return None
    return Violation(
        "search_padding",
        f"Your reply names {', '.join(repr(s) for s in strays)}, which the search "
        "did not return. Name ONLY the restaurants the search actually returned. "
        "If you want to say what a different restaurant does or does not serve, "
        "call get_menu for it first; never assert that without checking.",
    )


# --------------------------------------------------------------------------- #
# Rule 3 — a bill must come from preview_bill THIS TURN
# --------------------------------------------------------------------------- #


def _bill_violation(reply: str, trace: list[dict]) -> Violation | None:
    """Subsumes `_readback_bill_is_unbacked`. Two ways in, because a bill can be
    fabricated without ever quoting a parseable Total line:
      * the reply quotes a Total  → it must MATCH a preview this turn;
      * the reply is bill-SHAPED  → some preview must have run at all.
    """
    quoted = quoted_total(reply)
    bill_shaped = looks_like_a_bill(reply)
    if quoted is None and not bill_shaped:
        return None

    totals = _preview_totals(trace)
    unbacked = (
        not totals
        or (quoted is not None and not any(abs(quoted - t) <= _CENT for t in totals))
    )
    if not unbacked:
        return None
    return Violation(
        "unbacked_bill",
        "The bill you just showed was NOT produced by preview_bill — you cannot "
        "compute a bill yourself, and you must never show Subtotal/Tax/Delivery/"
        "Total lines that did not come from it. Call preview_bill now with the "
        "customer's payment method, delivery address and contact name, then read "
        "back its EXACT figures. If it tells you a detail is still missing, ask "
        "the customer for that instead and show no bill at all this turn. Never "
        "invent a tax rate or delivery fee, and never show Rs. 0 for either.",
        force_tool=True,
    )


# --------------------------------------------------------------------------- #
# Rule 4 (Tier 2) — every Rs figure must have a real source
# --------------------------------------------------------------------------- #


def _legitimate_amounts(
    db: Session, conversation: Conversation, trace: list[dict]
) -> set[Decimal]:
    """Every money figure the reply is entitled to state.

    Enumerable on purpose: menu prices, the restaurant's own fees, cart line
    totals, whatever a bill/placement tool returned this turn, the customer's own
    past order totals, any estimate this turn's search produced, and any number
    the CUSTOMER themselves typed (echoing their budget back is not a fabrication).
    """
    allowed: set[Decimal] = set()

    def add(value) -> None:
        try:
            allowed.add(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            pass

    for price, in db.execute(select(MenuItem.price)).all():
        add(price)
    for fee, minimum in db.execute(
        select(Restaurant.delivery_fee, Restaurant.min_order_amount)
    ).all():
        add(fee)
        add(minimum)

    for line in (conversation.cart or {}).get("items", []):
        try:
            unit = Decimal(str(line["price"]))
            add(unit * int(line["quantity"]))
        except (InvalidOperation, ValueError, TypeError, KeyError):
            continue

    for tool in ("preview_bill", "place_order"):
        for result in _tool_results(trace, tool):
            for key in (
                "subtotal", "tax_amount", "delivery_fee", "discount_amount", "total",
            ):
                if result.get(key) is not None:
                    add(result[key])
            for item in result.get("items") or []:
                if isinstance(item, dict) and item.get("line_total") is not None:
                    add(item["line_total"])

    for result in _tool_results(trace, "get_order_status"):
        if result.get("total") is not None:
            add(result["total"])
    for result in _tool_results(trace, "find_restaurants"):
        for entry in result.get("restaurants") or []:
            estimate = entry.get("estimate") if isinstance(entry, dict) else None
            if isinstance(estimate, dict):
                for key in ("estimated_total", "food_estimate", "delivery_fee"):
                    if estimate.get(key) is not None:
                        add(estimate[key])
                primary = estimate.get("primary_item")
                if isinstance(primary, dict) and primary.get("price") is not None:
                    add(primary["price"])

    for total, in db.execute(
        select(Order.total_amount).where(Order.customer_id == conversation.customer_id)
    ).all():
        add(total)

    return allowed


def _price_violation(
    db: Session, conversation: Conversation, reply: str, trace: list[dict],
    customer_figures: set[Decimal],
) -> Violation | None:
    figures = _money_figures(reply)
    if not figures:
        return None
    allowed = _legitimate_amounts(db, conversation, trace) | customer_figures
    invented = [
        f for f in figures
        if not any(abs(f - a) <= _CENT for a in allowed)
    ]
    if not invented:
        return None
    return Violation(
        "invented_amount",
        "Your reply states "
        f"{', '.join('Rs. ' + str(f) for f in invented)}, which no menu price, "
        "cart line, bill or past order of this customer's contains. Never do "
        "arithmetic or estimate a figure yourself — every number you state must "
        "come from a tool result. Re-state only the figures the tools gave you, "
        "or call preview_bill for the bill.",
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def audit(
    db: Session,
    conversation: Conversation,
    reply: str | None,
    trace: list[dict],
    *,
    customer_figures: set[Decimal] | None = None,
    check_prices: bool = True,
) -> Violation | None:
    """The single grounding check. Returns the first violation, or None.

    Ordered most-specific first, so the nudge the model receives names the actual
    problem rather than a generic one.
    """
    if not reply:
        return None

    violation = _restaurant_list_violation(db, reply, trace)
    if violation is not None:
        return violation

    violation = _search_fidelity_violation(db, conversation, reply, trace)
    if violation is not None:
        return violation

    violation = _bill_violation(reply, trace)
    if violation is not None:
        return violation

    if check_prices:
        violation = _price_violation(
            db, conversation, reply, trace, customer_figures or set()
        )
        if violation is not None:
            return violation

    return None


def figures_the_customer_wrote(db: Session, conversation: Conversation) -> set[Decimal]:
    """Numbers from the customer's own recent messages. Echoing back "Rs. 1500
    mein kya milega" is not a fabrication."""
    from app.services import conversations as convo

    figures: set[Decimal] = set()
    for msg in convo.recent_history(db, conversation, limit=12):
        if msg.direction != MessageDirection.INBOUND:
            continue
        for raw in re.findall(r"\d[\d,]*(?:\.\d{1,2})?", msg.content or ""):
            try:
                figures.add(Decimal(raw.replace(",", "")))
            except (InvalidOperation, ValueError):
                continue
    return figures
