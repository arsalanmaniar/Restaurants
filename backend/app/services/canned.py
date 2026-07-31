"""The fixed replies we send without asking the model, in both languages.

These bypass the model entirely — a prefilter redirect, a technical-failure
fallback, a guard replacing a reply that could not be sent. So SYSTEM_PROMPT's
language rules never touch them, and every one was hardcoded in a single
language.

That gap reached a real customer. Conversation 724 was entirely Roman Urdu; the
model malformed its tool call, the salvage guard correctly refused to invent an
answer, and the customer received:

    "Sorry, we're having a technical problem at the moment. Please try again
     shortly, or reply 'help' and a member of our team will get back to you."

It failed in both directions: the fallback and the prefilter replies were
English-only, while the payment replacements were Roman-Urdu-only, so an English
customer hitting those got Urdu. This closes followups-deferred #2 for the
conversational path. notifications.py is deliberately out of scope — it is driven
from the restaurant dashboard, not an inbound message, so resolving the
customer's language there needs different plumbing.

UNKNOWN resolves to ROMAN URDU, not English. That matches the rule SYSTEM_PROMPT
already states for a bare greeting — most of our customers write Roman Urdu — and
it is the specific default that was wrong before: the English fallback was
reaching Roman Urdu speakers.

Every string here is linted by tests/test_tone.py, so the English side cannot
reintroduce the call-centre register the Roman Urdu side just shed.
"""

from app.services.language import ENGLISH, ROMAN_URDU, UNKNOWN

# key -> {language -> text}. Templated entries take **fmt in `text()`.
_TEXTS: dict[str, dict[str, str]] = {
    "fallback": {
        ENGLISH: (
            "Sorry, we're having a technical problem right now. Please try again "
            "in a moment, or reply 'help' for a person."
        ),
        ROMAN_URDU: (
            "Maaf kijiye, abhi technical masla ho raha hai. Thori der baad dobara "
            "koshish karein, ya 'help' likh dein."
        ),
    },
    "offtopic": {
        ENGLISH: "I'm here to help with food orders. What would you like to order?",
        ROMAN_URDU: "Main khane ke order ke liye hun. Aap kya order karna chahenge?",
    },
    "rate_limited": {
        ENGLISH: (
            "Your messages are coming in faster than we can handle. Please wait a "
            "moment before sending another."
        ),
        ROMAN_URDU: (
            "Aapke messages bohot tezi se aa rahe hain. Thora ruk kar dobara "
            "bhejein."
        ),
    },
    # An order exists and is committed to cash on delivery.
    "cod_order": {
        ENGLISH: (
            "Order {order_number} is already placed as cash on delivery, so online "
            "payment can't be added to it. A new order would be needed. Shall I start one?"
        ),
        ROMAN_URDU: (
            "Order {order_number} cash on delivery par place ho chuka hai, is par "
            "online payment nahi lag sakti. Online ke liye naya order banana hoga. "
            "Bana dun?"
        ),
    },
    # No order to point at — says only what is true in every case.
    "no_order": {
        ENGLISH: (
            "No payment link has been sent yet. Paying online needs an order first. "
            "Shall I start one?"
        ),
        ROMAN_URDU: (
            "Abhi tak koi payment link nahi bheja. Online payment ke liye pehle "
            "order place karna hoga. Order shuru karun?"
        ),
    },
    # Built from the trace when a salvaged reply cannot be trusted.
    "order_placed": {
        ENGLISH: "Order {order_number} is placed. {extra}Anything else?",
        ROMAN_URDU: "Order {order_number} place ho chuka hai. {extra}Aur kuch chahiye?",
    },
}


# Every key, so tests can lint the whole set rather than an inventory that drifts
# out of date the moment someone adds a reply.
KEYS = tuple(_TEXTS)


def text(key: str, lang: str, **fmt: object) -> str:
    """The reply for `key` in the customer's language.

    UNKNOWN — which services/language.py returns often, by design — resolves to
    Roman Urdu rather than English. See the module docstring.
    """
    variants = _TEXTS[key]
    return variants.get(lang, variants[ROMAN_URDU]).format(**fmt)


def variants(key: str) -> tuple[str, ...]:
    """Every language's version of `key`.

    Needed wherever code recognises one of these strings by equality rather than
    producing it. prefilter.already_notified_rate_limit compares the last outbound
    against the rate-limit notice to avoid spamming a customer who just got one —
    with one hardcoded string that was a simple ==, but a customer notified in
    Roman Urdu would not match an English constant, the check would report "not
    notified", and they would be told to slow down on every message.
    """
    return tuple(_TEXTS[key].values())
