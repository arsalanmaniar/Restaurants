"""Roman Urdu tone — the rules we can actually enforce.

Tone in MODEL output cannot be pinned deterministically, and per the earlier
decision it is not guard-enforced either: a tone false positive would suppress a
correct reply, and that trade is far worse than an awkward sentence reaching the
customer (same asymmetry services/language.py documents).

What IS pinnable is the text WE wrote — the canned replies, and the reference
shapes inside SYSTEM_PROMPT. Those had drifted furthest from the rules the prompt
itself lays down. A prompt whose own examples break its rules teaches the model to
break them, so the shapes are linted against the same checks as the canned strings.

Every banned phrase below appeared in real production output. Message ids are from
the live messages_log; they are what the customer meant by "abhi bhi proper Roman
Urdu wali tone nahi hai".
"""

import re

from app.services import agent


# Call-centre English translated whole, plus the grammar slips. Each entry is
# (pattern, why, where it was seen).
BANNED = [
    ("kya aapko koi aur madad chahiye",
     "'Do you need any further assistance?' — call-centre English. Say 'Aur kuch chahiye?'",
     "[983] [986] [987] [989] [991]"),
    ("aap kya karna chahenge",
     "abstract in a way Urdu is not. Name the thing: 'Aur kya lenge?'",
     "[851] [1009] [1013] [1017] [1019] [1021] [1023]"),
    ("main aapke liye",
     "'for you' is English politeness scaffolding",
     "[1017] [1019] [1021]"),
    ("bheja ja raha hai",
     "English passive. Say 'Payment link ye hai:'",
     "[945]"),
    ("bheja jayega",
     "English passive. Say 'aa jayega' / 'main bata dunga'",
     "[983] [989]"),
    ("aapka cart mein",
     "wrong case — 'Aapke cart mein'",
     "[1035]"),
    ("poori bill",
     "'bill' is masculine — 'poora bill'",
     "[898]"),
    ("ki tarf se",
     "misspelling of 'taraf'",
     "[983]"),
    ("awaiting payment",
     "raw internal status value in a customer reply",
     "[989]"),
    ("here are restaurants serving",
     "English header left inside a Roman Urdu reply",
     "[995]"),
]

MAX_WORDS = 45
MAX_QUESTIONS = 1


def _lint(text: str) -> list[str]:
    """Every tone rule this string breaks."""
    problems = []
    lowered = text.lower()

    for phrase, why, seen in BANNED:
        if phrase in lowered:
            problems.append(f"banned phrase {phrase!r} ({why}; seen in {seen})")

    if len(text.split()) > MAX_WORDS:
        problems.append(f"too long: {len(text.split())} words > {MAX_WORDS}")

    if text.count("?") > MAX_QUESTIONS:
        problems.append(f"{text.count('?')} questions; ask ONE")

    return problems


def _canned_replies() -> dict[str, str]:
    """The fixed strings we send customers directly, with no model in the loop."""
    return {
        "COD_ORDER_REPLACEMENT": agent.COD_ORDER_REPLACEMENT.format(order_number="AB-1234"),
        "NO_ORDER_REPLACEMENT": agent.NO_ORDER_REPLACEMENT,
    }


class TestCannedRepliesFollowTheToneRules:
    """These bypass the model entirely, so the prompt cannot fix them — they are
    only ever as good as the string in the source."""

    def test_every_canned_reply_passes(self):
        failures = {
            name: problems
            for name, text in _canned_replies().items()
            if (problems := _lint(text))
        }
        assert not failures, f"canned replies break their own tone rules: {failures}"

    def test_the_lint_actually_bites(self):
        """Guard the guard: the pre-fix COD_ORDER_REPLACEMENT must fail. Without
        this, a lint that silently matched nothing would look like a passing suite."""
        before_the_fix = (
            "Maaf kijiye, aapka order AB-1234 pehle hi Cash on Delivery par place ho "
            "chuka hai — is ke liye online payment link add nahi kiya ja sakta. Agar aap "
            "online payment karna chahein to naya order online payment ke sath place "
            "karna hoga. Kya main wo order taiyar karun?"
        )
        assert _lint(before_the_fix), "the lint must reject the string this fix replaced"

    def test_a_known_bad_production_reply_is_rejected(self):
        """[1017], verbatim — the reply the customer complained about."""
        problems = _lint(
            "Sandwich Karachi Biryani House mein nahi hai. Aap kya karna chahenge, "
            "koi aur item order karna chahenge ya main aapke liye doosre restaurant "
            "se search karun?"
        )
        assert any("aap kya karna chahenge" in p for p in problems)
        assert any("main aapke liye" in p for p in problems)

    def test_a_good_reply_passes_cleanly(self):
        """The lint must not simply reject everything."""
        assert _lint("Sandwich humare kisi bhi restaurant mein nahi hai. Aur kuch dikhaun?") == []
        assert _lint("Chicken Biryani add kar diya — Rs. 450.\n\nAur kuch chahiye?") == []


class TestPromptExamplesFollowTheirOwnRules:
    """The reference shapes teach by example. If a shape breaks the rules stated
    twenty lines above it, the example is what the model will copy."""

    def _roman_urdu_shape_lines(self) -> list[str]:
        """The quoted example lines from the Roman Urdu shapes section."""
        section = agent.SYSTEM_PROMPT.split("Roman Urdu reference shapes")[1]
        section = section.split("The conversation flow, in order:")[0]
        # Quoted example text only — not the surrounding instructions.
        return [
            line.strip()
            for line in re.findall(r'"([^"]{20,})"', section, re.DOTALL)
        ]

    def test_the_section_was_actually_found(self):
        """If the prompt is restructured and this stops matching, the tests below
        would pass vacuously."""
        shapes = self._roman_urdu_shape_lines()
        assert len(shapes) >= 5, f"expected the reference shapes, found {len(shapes)}"

    def test_no_shape_uses_a_banned_phrase(self):
        failures = {}
        for shape in self._roman_urdu_shape_lines():
            lowered = shape.lower()
            hits = [phrase for phrase, _why, _seen in BANNED if phrase in lowered]
            if hits:
                failures[shape[:60]] = hits
        assert not failures, f"prompt examples use phrasing the prompt bans: {failures}"

    def test_no_shape_stacks_questions(self):
        failures = {
            shape[:60]: shape.count("?")
            for shape in self._roman_urdu_shape_lines()
            if shape.count("?") > MAX_QUESTIONS
        }
        assert not failures, f"prompt examples stack questions: {failures}"


class TestTheRulesAreStatedInThePrompt:
    """Cheap anchors so the guidance cannot be dropped without a test noticing.
    Deliberately few — asserting on prose is brittle, and these pin only the rules
    the corpus showed were missing."""

    def test_sentence_construction_rule_is_present(self):
        assert "Do NOT compose the reply" in agent.SYSTEM_PROMPT
        assert "ACTIVE voice" in agent.SYSTEM_PROMPT

    def test_question_cap_is_present(self):
        assert "ONE question" in agent.SYSTEM_PROMPT

    def test_grammar_corrections_are_present(self):
        assert "Aapke cart mein" in agent.SYSTEM_PROMPT
        assert "poora bill" in agent.SYSTEM_PROMPT

    def test_no_english_leak_rule_is_present(self):
        assert "raw status value" in agent.SYSTEM_PROMPT
