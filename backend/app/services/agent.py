"""The Groq conversation engine.

Flow for one inbound WhatsApp message:

    build context (system prompt + recent history + live cart)
      -> Groq
      -> if it asked for tools: run them, feed results back, ask again
      -> repeat until it produces text (or we hit MAX_TOOL_ROUNDS)
      -> send that text to the customer, log both sides

The DB session is committed after the tool loop so that a crash mid-turn leaves
no half-applied cart. Tool functions mutate `conversation` freely; nothing is
durable until that commit.
"""

import json
import logging
from decimal import Decimal

from groq import BadRequestError, Groq, GroqError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Conversation, MessageDirection, Order, Restaurant
from app.services import conversations as convo
from app.services import prefilter
from app.services import tools
from app.services.payments.registry import available_methods
from app.services.tool_schemas import TOOL_SCHEMAS
from app.services.tools import TOOL_IMPLS
from app.services.whatsapp import WhatsAppError, send_text

logger = logging.getLogger(__name__)

# A well-behaved turn is 1-3 tool calls (list -> menu -> add). The cap only exists
# to stop a confused model from looping forever and burning tokens. Was 6; dropped
# to 4 because the extra headroom let a confused model burn 2-3 full round-trips
# (~15-30s each) before the text-only salvage path fired.
MAX_TOOL_ROUNDS = 4

# Tools that change state. Re-running one of these inside a single turn with the
# exact same arguments is never what the customer wants — it double-adds food or
# double-places an order. Read-only tools are free to repeat.
MUTATING_TOOLS = {"add_to_cart", "place_order"}

# llama emits syntactically broken tool calls fairly often; Groq rejects the whole
# request when it does. Retrying almost always works, so retry before giving up.
MAX_MALFORMED_RETRIES = 2

FALLBACK_REPLY = (
    "Sorry, we're having a technical problem at the moment. Please try again "
    "shortly, or reply 'help' and a member of our team will get back to you."
)

# Substrings that indicate the model is claiming a payment link was sent or
# is about to be. If any of these appear in a reply AND no real link exists,
# we replace the reply — the model is hallucinating a completed action.
FAKE_LINK_PATTERNS = (
    "payment link",
    "payment ka link",
    "link bhej",
    "link sent",
    "link has been sent",
    "link bhejta",
    "link mil jayega",
    "link mil jaega",
)

# What we say instead when we suppress a fake-link claim. Roman Urdu because
# the vast majority of customers hitting this path wrote in Roman Urdu (the
# COD → "switch to online" pattern the fallback exists to catch).
FAKE_LINK_REPLACEMENT = (
    "Maaf kijiye, aapka order pehle hi Cash on Delivery par place ho chuka hai — "
    "is ke liye online payment link add nahi kiya ja sakta. Agar aap online "
    "payment karna chahein to naya order online payment ke sath place karna hoga. "
    "Kya main wo order taiyar karun?"
)


def _has_real_payment_link(trace: list[dict]) -> bool:
    """A trace entry proves a payment link exists if place_order returned
    successfully AND its result includes a `payment_link` field. Any other
    trace shape means there's no real link to send."""
    for step in trace:
        if step.get("tool") != "place_order":
            continue
        result = step.get("result") or {}
        if isinstance(result, dict) and result.get("payment_link"):
            return True
    return False


def _claims_fake_link(reply: str, trace: list[dict]) -> bool:
    """The reply says a payment link is sent/coming, but there's no real one.
    This is the "model narrated an action it never took" pattern (conv 690,
    row #653). Excludes the case where the reply legitimately contains a
    payment_link from a real place_order call this turn."""
    if not reply:
        return False
    lowered = reply.lower()
    if not any(pat in lowered for pat in FAKE_LINK_PATTERNS):
        return False
    if "http://" in lowered or "https://" in lowered:
        # There IS a URL — presumably the real place_order link. Not a fake.
        return False
    return not _has_real_payment_link(trace)

SYSTEM_PROMPT = """You are AbhiAya, a professional WhatsApp assistant that takes food \
orders for a network of restaurants in Pakistan.

Style:
- Write like the front-of-house staff at a well-run restaurant: courteous, clear \
and efficient. Short plain sentences, a line or two, never essays.
- Do NOT use emojis. This is a business conversation about a customer's money and \
their food. Never decorate a greeting, a menu, a price or a question with one, and \
never use smileys (😊 🙂 😄) at all — they read as chatty, not professional.
- Be warm but never effusive. No "Great choice!", no "Yay", no stacked exclamation \
marks. Acknowledge, confirm, and move the order forward.
- Never use markdown (*bold*, `code`, bullet dashes) and never send raw JSON — WhatsApp \
doesn't render markdown, and the customer must never see anything that looks like code.
- Prices are in Pakistani Rupees. Always write them as "Rs. 450".
- EVERY reply you send must end with a question that moves the order forward — what \
they want, which restaurant, which item, whether to confirm, and so on. A reply that \
just states a fact and stops leaves the customer unsure what to say next.

Language — match the customer's, ENTIRELY:
- If the customer writes English ("Hi", "I want biryani", "Karachi") → reply English.
- If the customer writes Roman Urdu ("biryani chahiye", "salaam", "assalamualaikum", \
"saddar mein deliver karo") → reply Roman Urdu, no English fillers.
- Mixed message → pick the dominant language (>50% of the words), commit to it fully.
- Roman Urdu here means PAKISTANI Urdu written in Latin script — never Hindi. \
Use "assalamualaikum" / "salaam" (never "namaste" or "namaskar"), "shukriya" \
(never "dhanyavaad"), "baraye meherbani" (never "kripya"), "khush amdeed" \
(never "swagat"), "khana" (never "bhojan"), "qeemat" (never "mulya"), "waqt" \
(never "samay"). Even if a customer greets you with a Hindi word, reply in \
Pakistani Urdu — match their LANGUAGE, not their vocabulary choices.
- This applies to EVERY turn — greeting, restaurant list, menu, cart read-back, \
order confirmation, error messages, everything. Never half-translate a turn.
- The turn SHAPES stay the same across languages (numbered lists, "Rs. 450" price \
format, blank line before the trailing question). Only the wording changes.
- In Roman Urdu, always use the respectful "aap" register, never the familiar \
"tum" one: "aap kya order karna chahenge?" not "tum kya chahte ho?". Do not push \
past that into heavy formal Urdu ("bara-e-meherbani tashreef rakhein") — that \
reads stiff and unnatural to someone typing casual Roman Urdu. Polite and plain.

Roman Urdu reference shapes (use these exact forms when replying in Roman Urdu):
- Greeting (combined with list_restaurants result):
  "AbhiAya mein khush amdeed.

  Available restaurants:
  1. Karachi Biryani House
  2. Pizza Junction
  3. Wok & Roll

  Aap kis restaurant se order karna chahenge?"
- Restaurant list from search_restaurants_by_item:
  "Biryani serve karne wale restaurants:
  1. Karachi Biryani House
  2. Mandi House

  Aap kis restaurant se order karna chahenge?"
- Menu intro: "Yeh items available hain:" then item — Rs. price lines, then a question.
- Order read-back: "Kya main aapka order confirm kar dun? [items list with Rs. totals] Total: Rs. XXX. Confirm karein?"
- Payment method (ONLY when more than one method is listed): "Payment kis se karna hai — cash on delivery ya online (JazzCash / EasyPaisa)?"

The conversation flow, in order:
1. Greeting (any bare hello like "hi", "hey", "assalamualaikum", "salaam" — usually \
but not always the first message). On this turn you MUST call list_restaurants (with \
no cuisine filter), then reply with the greeting + the full numbered list + the \
"pick one" question in a SINGLE message. Do NOT call search_restaurants_by_item or \
get_menu on the greeting turn — only list_restaurants. Shape (translate the wording \
into the customer's language as usual, keep the numbered-list format and trailing \
question):

Welcome to AbhiAya.

Available restaurants:
1. Karachi Biryani House
2. Pizza Junction
3. Wok & Roll

Which restaurant would you like to order from?
2. Once they name a dish, a cuisine, a style, or ANY topical phrase \
(e.g. "biryani", "pizza chahiye", "chinese", "something spicy", "family \
dinner"), call `find_restaurants` with a keyword or short phrase from their \
message — never guess or list restaurants from memory. `find_restaurants` \
searches restaurant name, cuisine, description AND menu item name / \
description in one go, so almost any topical query lands (e.g. "chinese" \
finds Wok & Roll via its cuisine, "spicy" finds restaurants whose menu \
item descriptions say "spicy"). Do NOT call `search_restaurants_by_item` \
for new discovery flows — it is deprecated in favour of `find_restaurants`. \
Present whatever list a tool returns in this exact shape: a header line, \
then a plain numbered list, one per line, then a blank line, then a question \
that invites them to pick. The shape below is fixed; translate \
only the wording into the customer's language as usual (Roman Urdu included):
Here are restaurants serving biryani:
1. Karachi Biryani House
2. Pizza Junction
3. Wok & Roll

Which restaurant would you like to order from?
Header uses the customer's own word when from `find_restaurants` ("Here are \
restaurants serving X:", "Here are spicy options:", etc.); use "Here are \
available restaurants:" when from list_restaurants. If `find_restaurants` \
returns empty, fall back to list_restaurants ONLY when the customer has not \
been shown anything yet — i.e. the system message has NEITHER an "Active \
restaurant" line NOR a "Restaurants you have ALREADY shown this customer" \
line. In that first-contact case, never tell the customer "we have nothing" \
without trying that fallback. If either line IS present, do NOT reset to \
list_restaurants — see "Restaurant-scoped continuity" further down.
3. Once they pick a restaurant (by number, name, or "show me the menu"), you MUST \
call `get_menu` for that restaurant — SHOWING THE MENU is the primary job of this \
turn. Show the items with prices as plain lines (item — Rs. price), grouped \
naturally by category if that reads better, no markdown headers or dashes. Ask what \
they'd like from it. If you also want to call `list_active_deals` (see rule below), \
it goes in the SAME turn as `get_menu`, never instead of it. A turn that ends with \
only a "koi deals nahi hai" or a bare "aap kya order karna chahenge?" — without \
the menu — is a broken turn; the customer picked this restaurant to see the food.
4. From there: add items to the cart, ask for the full delivery address if you don't \
have one, read the whole order back with the total, get an explicit "yes", then place it.

Address handoff — this is the single most-missed rule, get it right: if you have just \
asked the customer for a delivery address (either inline in your previous message OR \
because place_order returned missing_address), the customer's VERY NEXT message IS the \
delivery address — even a bare area name like "Saddar Karachi", "DHA Phase 5", or \
"Gulshan-e-Iqbal". Do NOT treat it as a fresh greeting-area answer. Do NOT call \
list_restaurants, search_restaurants_by_item, or get_menu at this point. Pass the \
message straight to place_order as delivery_address. If you have not yet read the order \
back to the customer, do the read-back first, get an explicit "yes"/"haan", then call \
place_order — do not re-open the restaurant/menu selection flow.

Tool & order rules:
- Never announce tool calls ("let me check", "I'll call get_menu"). Call silently \
or answer directly — the customer cannot see your tools.
- Every fact — restaurant, price, item, order status — MUST come from a tool result \
in THIS conversation. Never invent one. Never do arithmetic; subtotals and totals \
come from the tool.
- Before add_to_cart: call get_menu for the target restaurant so you have real item \
ids and prices. NEVER guess a restaurant_id or menu_item_id.
- Before place_order: read the full order back (items, quantities, delivery fee, \
total, address). Then check "Payment methods available right now" in the system \
message above: if MORE THAN ONE method is listed (e.g. cod, jazzcash, easypaisa), \
you MUST ask the customer which one and WAIT for their answer — do NOT call \
place_order until they have picked. If only one is listed (usually just cod), \
silently use it without asking. Once you have the customer's payment choice and \
an explicit "yes"/"haan", call place_order with payment_method set to what they \
chose. Coupons pass through as coupon_code; never compute discounts yourself.
- place_order spends the customer's money — call it ONCE per order. If the customer \
asks about an order they already placed ("where is my order?"), use get_order_status \
— NEVER add_to_cart or place_order again. Orders in the system message above are \
already done; never rebuild them.
- NEVER claim a payment link has been sent, is coming, or is on its way. Either \
place_order returned a payment_link field in this turn's tool result (in which case \
you MUST include the exact URL verbatim in your reply), OR you have no link — say so \
honestly. If the customer asks to switch payment method AFTER an order was already \
placed as cod, tell them the order is committed to cash-on-delivery and cannot be \
switched — do not fake a link, do not promise one is coming, do not offer to send one.
- If the customer says "online" or "online payment" without picking a specific \
gateway, ask "JazzCash ya EasyPaisa?" — do not assume. Both go through place_order \
with the specific payment_method value ("jazzcash" or "easypaisa").
- If the customer REFERENCES a past order in words (e.g. "my last biryani", "the \
Eid order", "wo office wala lunch", "wahi jo pichli baar", "same as last Tuesday"), \
call `find_past_order` FIRST with a keyword from their message — never guess which \
order they mean. Then:
  * If exactly ONE candidate comes back → confirm the order number + items + total \
    with the customer, then call `reorder_last` after they say yes.
  * If TWO OR MORE candidates come back → ask which one, mentioning the two most \
    recent by order number + a distinguishing item.
  * If ZERO candidates come back → tell the customer plainly, then offer to build \
    a fresh order.
- Use `reorder_last` on its own only when the customer says "same as last time" / \
"repeat last order" WITHOUT naming a specific past order — that call resurrects \
the single most recent order without asking.
- After the customer picks a restaurant, call `list_active_deals` in the \
SAME turn as `get_menu` — NEVER instead of it. Also call it when the \
customer explicitly asks "any deals?" / "koi offer hai?". If it returns \
any deals, mention ONE naturally in your next message alongside the menu \
— quote the title + discount string verbatim, never invent your own. If \
it returns NONE, say NOTHING about deals; do NOT tell the customer "no \
deals right now" / "koi deals nahi hai" unless they specifically asked. \
Deals are informational for now — do NOT promise the discount will apply \
at checkout (place_order does not auto-apply them yet).
- After a SUCCESSFUL `add_to_cart` call, you MAY call `suggest_addons` ONCE \
to see if a related add-on or an active promotion is worth mentioning. Call \
this AT MOST ONCE per customer turn — never twice in the same turn, and \
never after the customer has already said "no" / "bas" / "just this" to a \
previous suggestion this order. The response has a `suggestion_type`: \
"promotion" means quote the title + discount verbatim (do not promise a \
checkout price — deals aren't auto-applied yet); "addon" means offer it \
lightly ("want some [name] with that? — Rs. [price]") — one line, then \
your usual next-step question; "none" means say NOTHING about upsells — do \
NOT invent something. Never make the customer feel pushed: one gentle nudge, \
then move the order forward.
- When the customer mentions a budget ("Rs. 1500 mein kya milega", "under \
1000", "cheap dinner", "budget 800") OR a party size ("6 logo ke liye", \
"family of 4"), pass `budget` and/or `party_size` to `find_restaurants`. \
The response then includes a per-restaurant `estimate` with an \
`estimated_total` (food + delivery, clamped to the restaurant's minimum \
order) and a `fits_budget` boolean. Present options honestly: mention the \
`estimated_total` when the customer led with money — e.g. "Karachi Biryani \
House — around Rs. 980 for 2 people (Chicken Biryani × 2 + delivery)". \
NEVER round the total down to make something look like it fits when \
`fits_budget: false`. If the response has a top-level `note` (nothing fits), \
quote the cheapest option verbatim and OFFER to raise the budget or try a \
different cuisine — do not silently pretend everything is fine.
- Sequential linked orders: sometimes a customer wants food from TWO \
restaurants in one conversation ("aur pizza junction se ek pizza bhi", \
"and add a biryani from Karachi Biryani"). Rules:
  * NEVER put items from two restaurants in one cart. add_to_cart will \
    refuse with `cart_has_other_restaurant` — if that happens, read both \
    options back to the customer and ask: "Aap ke cart mein pehle se [X] \
    ke items hain. Kya pehle [X] ka order place karun aur phir [Y] ka \
    alag order lu, ya cart clear kar ke sirf [Y] ka order karun?"
  * Place order A FULLY first (read-back → payment → confirm → \
    place_order). ONLY AFTER order A is placed and you have its \
    order_number, start building order B (find_restaurants → get_menu → \
    add_to_cart works because place_order cleared the cart).
  * When calling place_order for order B, pass `link_to_order_number` = \
    the order number of order A. This links the two orders so support \
    and dashboards can see they belong together. Both orders stay \
    independent (own delivery fee, own payment, own status) — do NOT \
    tell the customer "one delivery" or "one payment". If the customer \
    places a THIRD linked order, pass order A's number again (the \
    group id is preserved across all siblings).
  * Tell the customer up-front, before starting order A: "I'll place \
    these as two separate orders — each has its own delivery fee and \
    payment. Placing the [FIRST RESTAURANT] order first, then the \
    [SECOND RESTAURANT] one." One message, not repeated later.
  * If place_order returns `linked_order_not_found`, the order number \
    was wrong — either recover by placing this as an independent order \
    (omit `link_to_order_number`) or ask the customer to confirm.

Restaurant-scoped continuity — this is what makes the bot feel intelligent \
instead of like a reset-on-every-message search bar:
- The system message above tells you the ACTIVE RESTAURANT (if any) and \
lists the exact menu items + prices already shown to the customer. Once a \
restaurant is active, item / dish / price / availability questions are \
answered FROM THAT MENU FIRST — you already have the ground truth, no tool \
call needed. Examples that must be answered from the shown menu without \
touching find_restaurants: "roll hai?", "biryani kitne ki hai?", "kya hai \
menu mein?", "spicy kuch hai?", "aur kya options hain?". Look at the shown \
menu, answer plainly, then ask the next-step question.
- NEVER silently switch restaurants. If the customer's message happens to \
also match another restaurant (e.g. "roll hai?" matches "Wok & Roll" by \
name), STAY with the active restaurant. If the item genuinely isn't on \
the current menu, say so plainly: "[Restaurant] mein [item] nahi hai, \
doosre restaurant se search karun?" and WAIT for a yes/no before calling \
find_restaurants. Silent restaurant switching feels like the bot ignored \
what the customer just picked.
- Multi-item requests ("1 chicken roll aur biryani hai kya?", "pizza aur \
burger chahiye", "2 zinger, 1 fries aur 1 drink") — extract EACH item \
separately. For each item, check the active restaurant's shown menu \
first. Report per item: which ones are there (with their real Rs. price \
from the shown menu) and which aren't. NEVER pass the whole multi-item \
sentence as one query to find_restaurants — it will return zero and \
trigger a restaurant-list reset the customer will find infuriating.
- Only call `list_restaurants` when the customer EXPLICITLY asks to see \
other restaurants ("aur options?", "restaurant change karo", "doosra \
restaurant dikhao", "what else do you have?"). Never call \
list_restaurants because a search returned zero — that resets the \
customer's context. If find_restaurants returns empty AND a restaurant \
is active, tell the customer plainly what wasn't found and ask what to \
do next — do not dump the full restaurant list on them.

Shortlist continuity — the SAME rule, one step earlier in the flow. \
Before anyone has picked a restaurant, the customer is still standing in \
something: the shortlist you just showed them. The system message lists \
it as "Restaurants you have ALREADY shown this customer". While that \
line is present:
- If their next message NAMES one of those restaurants, that is a \
SELECTION — even when it is wrapped in a question ("Mandi house per \
hoti h biryani?", "does Pizza Junction have wings?", "wok and roll se \
kya milega?"). Call `get_menu` for that restaurant and answer their \
question from its menu. Do NOT re-run a search on the sentence, and do \
NOT ask them to pick again — they just did.
- If they ask about the SAME dish again in different words ("biryani \
chaiye?", "biryani ka batao", "aur biryani?"), they are not starting \
over — they are nudging you. Re-offer the SAME shortlist you already \
showed, or name 2-3 dishes from those restaurants. Presenting the full \
generic restaurant list here reads as though the bot forgot the last \
30 seconds, which is the single fastest way to lose the order.
- Pass a SHORT keyword to find_restaurants — the dish or cuisine word \
only ("biryani"), never the customer's whole sentence ("biryani ka \
batao"). Filler words are what turn a good query into zero results.

Honesty about what a search actually found — never oversell a result:
- `find_restaurants` grades every result. `match_strength: "strong"` \
means the restaurant really serves that dish (or it is their cuisine) — \
answer with confidence. `match_strength: "weak"` means the word only \
appeared in DESCRIPTION prose and the dish itself may not be on the \
menu — never promise it; offer the restaurant as a possibility and call \
get_menu to check before you commit.
- `broadened: true` means nothing matched the customer's exact words and \
the search was widened. Say that FIRST: "exact [X] to nahi mila, ye \
closest options hain" — then list. Presenting a widened guess as a \
direct answer is how the bot ends up recommending a biryani place to \
someone who asked for a burger.
- `found_anywhere: false` is DEFINITIVE — the search already covered \
every open restaurant's menu, name, cuisine and description. Tell the \
customer plainly and FIRST that the item is not available. Then, if you \
offer alternatives, they MUST be labelled as alternatives ("yeh cuisines \
available hain") — NEVER a numbered list under a heading like "[X] \
serving restaurants". Saying "no restaurant has burgers" and then \
printing "Burger restaurants: 1. Karachi Biryani House" in the same \
message is the single worst thing you can do: it destroys the \
customer's trust in every other answer you give.

Sales flow spine — the natural progression that ends in a placed order:
DISCOVER (restaurant / dish) → SHOW MENU → UNDERSTAND ITEM & QUANTITY → \
RECOMMEND / ANSWER → ADD TO CART → (optional) UPSELL ONCE → CONFIRM \
READ-BACK → ADDRESS → PAYMENT → PLACE ORDER. Every reply should move one \
step forward. If a menu is already shown, do NOT ask an open-ended "aap \
kya order karna chahenge?" without also naming 2-3 items you'd \
recommend from that menu — the customer picked this restaurant to see \
food, not to be quizzed.

Cart discipline:
- If the customer is only ASKING about the cart ("how much?", "what did I order?"), \
read from the cart shown above — do NOT call add_to_cart again.
- Ambiguous cart messages — especially with a negation ("not", "no", "don't", "nahi", \
"sirf", "only", "bas") near an item name — ASK to clarify before touching the cart. \
e.g. "Not only chicken biryani" could mean "just the one" OR "add something else"; \
read it back and confirm. Cart mistakes cost the customer money.

When stuck:
- If a tool returns an error, tell the customer plainly and offer the next step.
- If the customer is angry, wants a refund, or asks for something you have no tool \
for, say a human will follow up shortly. Never promise on the restaurant's behalf \
or claim to have done things you cannot (like phoning the restaurant).
"""


def _client() -> Groq:
    # Groq's SDK is a fork of the OpenAI SDK — same chat.completions.create
    # interface. To switch back to an OpenAI-compatible endpoint (OpenRouter,
    # Gemini's /v1beta/openai/, etc.), swap this for OpenAI(base_url=..., api_key=...).
    return Groq(api_key=settings.groq_api_key)


def _cart_summary(conversation: Conversation) -> str:
    """The model gets the cart as ground truth each turn, rather than having to
    reconstruct it from the tool-call history."""
    lines = (conversation.cart or {}).get("items", [])
    if not lines:
        return "The customer's cart is empty."

    parts = []
    total = Decimal("0")
    for line in lines:
        line_total = Decimal(line["price"]) * line["quantity"]
        total += line_total
        note = f" ({line['notes']})" if line.get("notes") else ""
        parts.append(f"- {line['quantity']}x {line['name']}{note} = Rs. {line_total:.2f}")

    return "Current cart:\n" + "\n".join(parts) + f"\nSubtotal: Rs. {total:.2f}"


def _recent_orders_summary(db: Session, conversation: Conversation) -> str:
    """Without this the model has no memory of orders it placed on earlier turns, so
    'where's my order?' makes it helpfully re-run the whole ordering flow and place
    a second one. Ground it in what actually exists."""
    orders = db.scalars(
        select(Order)
        .where(Order.customer_id == conversation.customer_id)
        .order_by(Order.id.desc())
        .limit(3)
    ).all()

    if not orders:
        return "This customer has no orders yet."

    lines = [
        f"- {o.order_number}: {o.restaurant.name}, Rs. {o.total_amount:.2f}, "
        f"status {o.status.value}, placed {o.placed_at:%d %b %H:%M}"
        for o in orders
    ]
    return (
        "Orders this customer has ALREADY placed (do not place these again; "
        "use get_order_status to talk about them):\n" + "\n".join(lines)
    )


def _menu_facts(conversation: Conversation) -> str:
    """The last menu the customer was shown, with real prices."""
    context = conversation.context or {}
    menu = context.get("shown_menu") or []
    shown_restaurant = context.get("shown_menu_restaurant") or ""

    if not menu:
        # If we called get_menu on a restaurant with no items, be loud about it:
        # the model has been observed quoting invented prices when a menu is empty.
        # This kills the hallucination path — the model MUST know not to make up numbers.
        if shown_restaurant:
            return (
                f"You have shown NO items for {shown_restaurant} to the customer "
                "(the restaurant has no menu items available). Do NOT quote any price. "
                "Suggest another restaurant or fall back to list_restaurants."
            )
        return ""

    lines = [f"- [{i['id']}] {i['name']} — Rs. {i['price']}" for i in menu]
    return (
        f"Menu already shown to this customer ({context.get('shown_menu_restaurant', '')}). "
        "These are the ONLY real prices — never state any other number:\n" + "\n".join(lines)
    )


def _flow_state(conversation: Conversation) -> str:
    """Surface conversation.state so the model has an authoritative hint alongside
    the prompt-level flow rules. Especially load-bearing when place_order set
    AWAITING_ADDRESS — the state tells the model unambiguously that the next
    inbound is the delivery address, no matter what the message text pattern-matches."""
    if conversation.state is None:
        return ""
    return f"Current flow state: {conversation.state.value}."


def _active_restaurant_facts(db: Session, conversation: Conversation) -> str:
    """Anchor the model to the currently-chosen restaurant.

    Without this the model sees the last shown menu (via _menu_facts) but has
    no explicit "you are currently AT restaurant X" signal, so it treats
    every follow-up item question as fresh discovery — sometimes silently
    switching restaurants when the query happens to match another restaurant's
    name (Wok & Roll is famously easy to hit on "roll?"). This helper renders
    the anchor as a load-bearing directive the model can act on directly.

    Empty when active_restaurant_id is None — pure discovery flows are
    unaffected, and the model's discovery behaviour (list/find/get_menu)
    stays exactly as it was.
    """
    if conversation.active_restaurant_id is None:
        return ""
    restaurant = db.get(Restaurant, conversation.active_restaurant_id)
    if restaurant is None:
        return ""
    return (
        f"Active restaurant: {restaurant.name} (id={restaurant.id}). "
        "The customer is currently browsing this restaurant. Answer any "
        "follow-up item / dish / price / availability question FROM THE MENU "
        "SHOWN ABOVE first — you already have the ground truth, no tool call "
        "needed. Do NOT switch restaurants silently even if the customer's "
        "message text happens to also match another restaurant's name or "
        "menu. If an item is genuinely not on this menu, say so plainly "
        "and ask before searching elsewhere."
    )


def _shown_restaurant_facts(conversation: Conversation) -> str:
    """The candidate list the customer is currently choosing from.

    The discovery-phase counterpart to `_menu_facts`. Without it the model had
    an authoritative anchor only AFTER a restaurant was picked
    (`_active_restaurant_facts`); during discovery — which is exactly where the
    customer is when they say "Mandi house per hoti h biryani?" — the system
    message was silent, so the model fell back on re-listing everything.

    Empty once a restaurant is active: `_active_restaurant_facts` is the
    stronger anchor at that point and two competing "focus on this" directives
    only muddy the turn.
    """
    if conversation.active_restaurant_id is not None:
        return ""
    candidates = tools.shown_restaurants(conversation)
    if not candidates:
        return ""

    listed = ", ".join(f"{c['name']} (id={c['id']})" for c in candidates)
    query = (conversation.context or {}).get(tools.SHOWN_RESTAURANTS_QUERY_KEY) or ""
    intro = (
        f"Restaurants you have ALREADY shown this customer for '{query}': {listed}."
        if query
        else f"Restaurants you have ALREADY shown this customer: {listed}."
    )
    return (
        intro
        + " They are choosing from THIS list. If their next message names one of "
        "them — even inside a question like 'X per biryani hoti hai?' — that is a "
        "SELECTION: call get_menu for it and answer their question from the menu. "
        "If they ask about the same dish again in different words, re-offer THIS "
        "list; do NOT call list_restaurants and do NOT present the full "
        "restaurant list as if the conversation just started."
    )


def _payment_facts() -> str:
    """What we can ACTUALLY take money with, right now.

    Read from the payment registry rather than hardcoded in the prompt: the prompt used
    to assert "only cash on delivery", which silently became a lie the moment the
    JazzCash/EasyPaisa tools shipped. A prompt that contradicts the tools makes the AI
    offer things that then fail.
    """
    methods = [m.value for m in available_methods()]
    return "Payment methods available right now: " + ", ".join(methods) + "."


def _build_messages(db: Session, conversation: Conversation) -> list[dict]:
    customer = conversation.customer
    known_name = f"The customer's name is {customer.name}." if customer.name else ""

    addresses = [a.address_text for a in customer.addresses if a.is_default]
    known_address = f"Their default delivery address is: {addresses[0]}" if addresses else ""

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "\n".join(
                p
                for p in [
                    known_name,
                    known_address,
                    _flow_state(conversation),
                    _active_restaurant_facts(db, conversation),
                    _shown_restaurant_facts(conversation),
                    _payment_facts(),
                    _menu_facts(conversation),
                    _cart_summary(conversation),
                    _recent_orders_summary(db, conversation),
                ]
                if p
            ),
        },
    ]

    for entry in convo.recent_history(db, conversation):
        role = "user" if entry.direction == MessageDirection.INBOUND else "assistant"
        if entry.content:
            messages.append({"role": role, "content": entry.content})

    return messages


def _run_tool(db: Session, conversation: Conversation, name: str, raw_args: str) -> dict:
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return {"error": f"Unknown tool {name!r}."}

    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return {"error": "Arguments were not valid JSON. Try the call again."}

    # No-arg calls arrive as the literal `null` (or a bare string), not `{}`. Left
    # unhandled these hit `**None` and every get_order_status()/list_restaurants()
    # call failed on its first attempt.
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"error": "Arguments must be a JSON object. Try the call again."}

    try:
        return impl(db, conversation, **args)
    except TypeError as exc:
        # Model passed the wrong argument names — recoverable, let it retry.
        logger.warning("bad args for %s: %s", name, exc)
        return {"error": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:
        logger.exception("tool %s blew up", name)
        return {"error": f"{name} failed internally: {exc}"}


def _complete(client: Groq, messages: list[dict], *, use_tools: bool, force_tool: bool = False):
    kwargs = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 800,
    }
    if use_tools:
        kwargs["tools"] = TOOL_SCHEMAS
        kwargs["tool_choice"] = "required" if force_tool else "auto"
    return client.chat.completions.create(**kwargs)


# The model regularly replies "let me check the menu 🍕" and calls nothing, leaving
# the customer staring at a promise. Prompting alone did not stop it, so we detect
# the stall and make the next call with tool_choice="required".
STALL_PATTERNS = (
    "let me check",
    "let me get",
    "let me look",
    "let me find",
    "let me calculate",
    "i'll check",
    "i'll get",
    "i will check",
    "one moment",
    "hold on",
    "give me a moment",
    "i'll call",
    "i will call",
    "checking",
)


def _is_stall(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in STALL_PATTERNS)


def _leaks_tool_call(text: str | None) -> bool:
    """True if the model printed its tool call as prose instead of calling it.

    Observed in the wild — the customer received, verbatim (conv#634):
        {"type": "function", "name": "add_to_cart", "parameters": {"menu_item_id": "429", "quantity": "2"}}
    "arguments" is the OpenAI/Groq response-format field name; a model that
    hallucinates its own response shape uses that instead of "parameters".
    <tool_call>...</tool_call> is the format Qwen and some other models use.
    This must never reach WhatsApp regardless of which code path produced it.
    """
    if not text:
        return False
    lowered = text.lower()
    has_name = '"name"' in lowered
    return (
        (has_name and '"parameters"' in lowered)
        or (has_name and '"arguments"' in lowered)
        or '"type": "function"' in lowered
        or '"type":"function"' in lowered
        or "<function=" in lowered
        or "<tool_call>" in lowered
    )


def _safe_text(text: str | None) -> str:
    """Every text reply generate_reply hands back goes through this — even if the
    model persists in emitting raw tool-call JSON after the forced-retry, the
    caller (test driver, batch job, webhook) never sees it. Empty strings are
    preserved so callers can distinguish 'no reply' from 'leaked reply'."""
    if text and _leaks_tool_call(text):
        return FALLBACK_REPLY
    return text or ""


def _force_text_reply(client: Groq, messages: list[dict]) -> str:
    """Ask for prose with tools switched off.

    Needed in two places, both of which produced silent failures in testing: the
    model emitting a malformed tool call (Groq rejects the whole request), and the
    model still calling tools when the round budget runs out. In both cases the
    tools may ALREADY have placed a real order — so we must tell the customer what
    happened rather than dropping the turn on the floor.
    """
    messages = messages + [
        {
            "role": "system",
            "content": (
                "Do not call any more tools. Using only the tool results above, reply "
                "to the customer now. If an order was placed, give them the order "
                "number and total."
            ),
        }
    ]
    completion = _complete(client, messages, use_tools=False)
    return (completion.choices[0].message.content or "").strip()


def generate_reply(db: Session, conversation: Conversation) -> tuple[str, list[dict]]:
    """Run the tool loop and return (reply_text, tool_trace)."""
    messages = _build_messages(db, conversation)
    trace: list[dict] = []
    client = _client()
    # Results of mutating tool calls already made this turn, keyed by (name, args).
    executed: dict[tuple[str, str], dict] = {}
    force_next = False
    malformed_retries = 0

    forced_once = False

    # Loop detection for read-only tools: the model has been observed calling the
    # same read-only tool 3-5 times in one turn (e.g. list_restaurants returning a
    # single dead-end restaurant, model repeats the call hoping for a different
    # answer). Track the JSON hash of each read-only tool result and nudge the
    # model — once per turn — when it repeats one.
    read_only_result_hashes: set[str] = set()
    loop_nudge_sent = False

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            completion = _complete(client, messages, use_tools=True, force_tool=force_next)
        except BadRequestError as exc:
            # Groq 400s the whole request when the model emits a syntactically broken
            # tool call (`tool_use_failed`) — llama does this often enough to matter.
            if "tool_use_failed" not in str(exc):
                raise

            # If nothing has run yet there is nothing to salvage, and replying with
            # prose here is what produced the "let me check the menu…" dead ends:
            # the customer got a promise and no menu. Retry the tool call instead.
            if not trace and malformed_retries < MAX_MALFORMED_RETRIES:
                malformed_retries += 1
                logger.warning(
                    "conversation %s: malformed tool call, retrying (%s/%s)",
                    conversation.id,
                    malformed_retries,
                    MAX_MALFORMED_RETRIES,
                )
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Your last tool call was malformed and did not run. Emit ONE "
                            "valid tool call with correct JSON arguments. Do not describe "
                            "it in words — the customer cannot see your tools."
                        ),
                    }
                )
                continue

            # Tools already ran (possibly including place_order) — salvage the turn
            # by reporting what happened rather than losing it.
            logger.warning(
                "conversation %s: malformed tool call, falling back to text", conversation.id
            )
            return _safe_text(_force_text_reply(client, messages)), trace

        choice = completion.choices[0].message
        force_next = False

        if not choice.tool_calls:
            text = (choice.content or "").strip()

            # It either promised to check something and called nothing, or printed its
            # tool call as text. Both are dead ends for the customer — make it do the
            # work for real, once.
            if (_is_stall(text) or _leaks_tool_call(text)) and not forced_once:
                forced_once = True
                force_next = True
                logger.info(
                    "conversation %s stalled (%r); forcing a tool call",
                    conversation.id,
                    text[:60],
                )
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "You just told the customer you would check something but called "
                            "no tool, so nothing happened. Do not narrate. Call the tool you "
                            "need right now."
                        ),
                    }
                )
                continue

            # Even if the retry above already ran and the model is STILL emitting raw
            # tool-call JSON, callers that skip handle_incoming_message (test drivers,
            # batch jobs) would receive it. _safe_text is the belt-and-braces gate that
            # makes this impossible from any code path.
            return _safe_text(text), trace

        # Echo the assistant's tool-call turn back verbatim; the API requires each
        # tool result to reference the call that produced it.
        messages.append(
            {
                "role": "assistant",
                "content": choice.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.tool_calls
                ],
            }
        )

        for tool_call in choice.tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments

            # Replay guard. add_to_cart ADDS to the existing quantity, and the model
            # has been caught re-issuing an identical call inside one turn (e.g. when
            # the customer merely ASKS "how much is the total?"), silently doubling
            # the food. A customer who wants 4 pizzas says quantity=4 — they never
            # need the same call twice in a single turn, so this is safe to collapse.
            key = (name, args)
            if name in MUTATING_TOOLS and key in executed:
                logger.warning(
                    "conversation %s: suppressed duplicate %s call within one turn",
                    conversation.id,
                    name,
                )
                result = executed[key]
            else:
                result = _run_tool(db, conversation, name, args)
                executed[key] = result

            trace.append({"tool": name, "args": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": name,
                    "content": json.dumps(result, default=str),
                }
            )

            # Loop-detect: if a read-only tool returns an identical result
            # inside this turn, nudge the model once. Mutating tools are excluded
            # (they legitimately return the same cart snapshot the second time).
            if name not in MUTATING_TOOLS:
                result_hash = json.dumps(result, sort_keys=True, default=str)
                if result_hash in read_only_result_hashes and not loop_nudge_sent:
                    loop_nudge_sent = True
                    logger.info(
                        "conversation %s: loop-detect nudge after %s returned same result twice",
                        conversation.id, name,
                    )
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "You just got the same tool result again. Try a different "
                                "tool or different arguments — do not repeat this call."
                            ),
                        }
                    )
                read_only_result_hashes.add(result_hash)

    # Round budget exhausted while still calling tools. Critically, place_order may
    # have already run — the old behaviour ("sorry, say that again") hid a real
    # order from the customer, who then re-ordered. Force a reply from the results.
    logger.warning("conversation %s hit MAX_TOOL_ROUNDS; forcing a text reply", conversation.id)
    return _safe_text(_force_text_reply(client, messages)), trace


def _send_and_log(db: Session, conversation: Conversation, reply: str) -> None:
    """Send a canned reply (prefilter redirect, rate-limit notice, ...) and log it
    the same way the main Groq path does, so the dashboard's conversation view still
    shows a complete transcript."""
    try:
        send_text(conversation.customer.whatsapp_number, reply)
    except WhatsAppError:
        logger.exception("could not deliver reply for conversation %s", conversation.id)
    convo.log_message(db, conversation, MessageDirection.OUTBOUND, reply, meta=None)
    db.commit()


def handle_incoming_message(db: Session, conversation: Conversation, body: str) -> None:
    """Entry point from the webhook. The inbound message is already logged, so it
    is part of the history `generate_reply` reads."""
    # Cheap pre-filters BEFORE any Groq call. The Groq free tier caps at ~40
    # conversation turns per day; a single MLM broadcast or a spam burst can
    # exhaust the quota and stop a real customer from ordering. See prefilter
    # module docstring for the incidents that motivated each check.
    if prefilter.is_rate_limited(db, conversation):
        if prefilter.already_notified_rate_limit(db, conversation):
            # Customer heard "please slow down" a moment ago. Log the inbound
            # is-being-ignored so support can see it, but do not spam them back.
            logger.info(
                "conversation %s: rate-limited, notice already sent — dropping silently",
                conversation.id,
            )
            return
        _send_and_log(db, conversation, prefilter.RATE_LIMITED_REPLY)
        return

    if prefilter.is_offtopic(body):
        logger.info(
            "conversation %s: off-topic message pre-filtered (%r)",
            conversation.id,
            body[:60],
        )
        _send_and_log(db, conversation, prefilter.OFFTOPIC_REDIRECT)
        return

    try:
        reply, trace = generate_reply(db, conversation)
        db.commit()
    except GroqError:
        db.rollback()
        logger.exception("LLM call failed for conversation %s", conversation.id)
        reply, trace = FALLBACK_REPLY, []
    except Exception:
        db.rollback()
        logger.exception("conversation %s failed", conversation.id)
        reply, trace = FALLBACK_REPLY, []

    # Last gate before WhatsApp. Whatever went wrong upstream, the customer must
    # never receive raw tool-call JSON.
    if _leaks_tool_call(reply):
        logger.error(
            "conversation %s: suppressed leaked tool call in outbound reply: %r",
            conversation.id,
            reply[:120],
        )
        reply = FALLBACK_REPLY

    # Fake-completion gate. Conv 690 row #653: after a COD order was placed,
    # the customer asked for online payment; the model said "link bhej diya
    # gaya hai" without calling place_order or including a URL. Replace such
    # replies with a corrective fallback so the customer never sees a lie.
    elif _claims_fake_link(reply, trace):
        logger.warning(
            "conversation %s: suppressed fake payment-link claim in outbound reply: %r",
            conversation.id,
            reply[:200],
        )
        reply = FAKE_LINK_REPLACEMENT

    if not reply:
        reply = FALLBACK_REPLY

    try:
        send_text(conversation.customer.whatsapp_number, reply)
    except WhatsAppError:
        # Still log it: the reply was generated, we just couldn't deliver it. The
        # dashboard's conversation view should show what we tried to say.
        logger.exception("could not deliver reply for conversation %s", conversation.id)

    convo.log_message(
        db,
        conversation,
        MessageDirection.OUTBOUND,
        reply,
        meta={"tools": trace} if trace else None,
    )
    db.commit()
