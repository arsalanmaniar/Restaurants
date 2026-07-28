"""Which language is the customer writing in, and does our reply match?

The system prompt has always carried a language rule (English → English, Roman
Urdu → Roman Urdu). It is not reliably followed: in production the SAME class of
opener produced different languages — "Hello" was answered in English (conv 713)
while "Hi" and an unambiguous "How are you" were answered in Roman Urdu (convs
715, 722). That is adherence drift, so it needs a deterministic check like every
other model-behaviour rule here.

The hard part is not detection, it is NOT OVER-DETECTING. Pakistani customers
write Roman Urdu studded with English loanwords — "order", "cancel", "confirm",
"ok", "yes", "address", "payment" — and a naive word-count classifier reads those
as English. A first pass over real traffic flagged 7 language "mismatches" of
which only ONE was genuine; the other six were loanwords and a restaurant name
("wok and roll"). Acting on those would have broken four consecutive correct
turns in conv 721, where the customer typed "Yes" inside a Roman Urdu
conversation and was rightly answered in Roman Urdu.

So this module is deliberately biased towards UNKNOWN:

  * loanwords are stripped before anything is counted — they are evidence for
    neither language;
  * a message must have at least MIN_DECISIVE_TOKENS meaningful words left, so
    "Hi", "Yes" and "wok and roll" never decide anything;
  * the customer's language is read from the accumulated inbound history (most
    recent DECISIVE message wins), never from the single latest message — that
    is what stops one "Yes" flipping a Roman Urdu conversation to English;
  * when either side is UNKNOWN the caller does nothing.

A false negative costs us a mismatched reply. A false positive suppresses a
CORRECT reply and burns a regeneration, and can end at the "technical problem"
fallback. The asymmetry is why this errs towards silence.
"""

ENGLISH = "english"
ROMAN_URDU = "roman_urdu"
UNKNOWN = "unknown"

# A message needs this many meaningful (non-loanword) tokens before it is allowed
# to decide anything. "Hi", "Yes", "ok thanks" stay UNKNOWN by construction.
MIN_DECISIVE_TOKENS = 3

# How far one language must lead the other to count as decisive. A single marker
# is not enough: the address "Hoti market saadar Karachi" trips exactly one Urdu
# marker ("hoti") and would otherwise classify a plain address as Roman Urdu —
# which, in an English conversation, would flip the language on a delivery
# address. Two clear markers is the smallest lead that survived real traffic.
MIN_MARGIN = 2

# English function words a Roman Urdu writer essentially never reaches for.
# Deliberately excludes anything that doubles as a loanword below.
_ENGLISH_MARKERS = frozenset("""
    the a an this that these those
    i we they he she it
    am is are was were be been being
    do does did doing
    have has had having
    will would shall should can could may might must
    what where when why how which who whom whose
    my our their his her its your
    and or but if because so than then though although while
    there here very much many few more most
    of in on at by with without about into onto from for
    want need like get give show tell send make take know think
    hello hey good morning evening afternoon night
    sorry excuse
    all some any every each other another
    not don't didn't doesn't isn't aren't won't can't
""".split())

# Roman Urdu markers (Latin script). Common spelling variants included, because
# customers type phonetically and inconsistently.
_URDU_MARKERS = frozenset("""
    hai hain h he hy hei ho hoga hogi hota hoti hote
    aap ap tum tumhara aapka apka aapki apki aapke apke
    mein me main mai mera meri mere mujhe mujhy hum humara
    ka ke ki ko kay kaa se sy per par pe pr
    kya kia kiya kaise kaisay kaisa kon kaun kaunsa konsa kitna kitni kitne
    nahi nai nhi ni na mat
    chahiye chahiyay chaiye chahta chahti mangta mangti
    karo kardo karna karta karti karte kar karein karen kijiye
    batao bata bataye batayein dikhao dikha dedo dena bhejo bhej
    acha achha theek thik sahi bilkul
    abhi phir bhi sab kuch koi kisi
    ye yeh wo woh is us jo
    gaya gayi gaye raha rahi rahe diya dia liya
    salaam assalamualaikum shukriya meherbani
    khana khani khane peena pani
    jaldi dair der waqt
    say sath sy liye lye
""".split())

# English-LOOKING words that Roman Urdu speakers use constantly. These are
# stripped before counting, so they can never be mistaken for English evidence.
# This list is the single most important part of the module — every entry here
# is a false positive that did NOT happen.
_LOANWORDS = frozenset("""
    order orders cancel confirm confirmed confirmation
    ok okay okey yes no yeah yep
    address delivery deliver delivered payment pay paid
    online cash card cod jazzcash easypaisa
    menu restaurant restaurants item items price prices rate
    check checking done finish ready
    please plz thanks thank thanx shukria
    total bill number name time date today tomorrow
    location pin map
    plate piece pcs kg
    biryani pizza karahi haleem nihari kebab kabab tikka
    chicken beef mutton fish egg rice roll wok house junction
    burger fries shake soup noodles
    large medium small
    sir madam bhai
""".split())


def _tokens(text: str | None) -> list[str]:
    """Lowercase alphabetic words, punctuation stripped."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.split():
        word = "".join(ch for ch in raw if ch.isalpha() or ch == "'").lower().strip("'")
        if word:
            out.append(word)
    return out


def classify(text: str | None) -> str:
    """ENGLISH, ROMAN_URDU or UNKNOWN for a single message.

    UNKNOWN is the safe answer and is returned whenever the evidence is thin —
    too few meaningful words, or both languages equally represented.
    """
    words = _tokens(text)
    # Loanwords are evidence for neither side.
    meaningful = [w for w in words if w not in _LOANWORDS]
    if len(meaningful) < MIN_DECISIVE_TOKENS:
        return UNKNOWN

    english = sum(1 for w in meaningful if w in _ENGLISH_MARKERS)
    urdu = sum(1 for w in meaningful if w in _URDU_MARKERS)
    if abs(english - urdu) < MIN_MARGIN:
        return UNKNOWN
    return ENGLISH if english > urdu else ROMAN_URDU


def customer_language(messages: list[str]) -> str:
    """The customer's language, from their inbound messages OLDEST-FIRST.

    The most recent DECISIVE message wins, so a genuine switch is honoured while
    an indecisive "Yes" or "ok" leaves the established language alone. Returns
    UNKNOWN when nothing in the history is decisive — the caller then does
    nothing, which is what keeps short-message conversations untouched.
    """
    for text in reversed(messages):
        verdict = classify(text)
        if verdict != UNKNOWN:
            return verdict
    return UNKNOWN


def reply_mismatches(customer_lang: str, reply: str | None) -> bool:
    """True only when BOTH sides are decisive AND they disagree.

    Doubly conservative on purpose: an undetectable customer language or an
    undetectable reply language is never treated as a violation.
    """
    if customer_lang == UNKNOWN:
        return False
    reply_lang = classify(reply)
    if reply_lang == UNKNOWN:
        return False
    return reply_lang != customer_lang
