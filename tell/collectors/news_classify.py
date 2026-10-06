"""Title-regex classification for news items (SPEC section 3.7 and section 8).

Ordered rules, first match wins: every ``leadership`` rule is tried before any
``funding`` rule, then ``launch``, then ``expansion``. Matching is case-insensitive
on the headline only; article text is never fetched or stored. A title no rule
matches is unclassified, and the collector does not store it.

Each rule has a stable name (``<category>.<rule>``) that the collector writes to
``payload.matched_regex`` so a wrong classification can be traced to the rule that
made it. The category maps to the event type ``news_<category>`` and to the
section 8 signal of the same name (``news_funding`` is what the section 13.1
backtest counts).

Leadership headlines usually carry a person's name, which tell never stores (the
project's PII rule). :func:`redact_leadership` turns a leadership headline into the
form the collector stores: the person-name span replaced with ``[person]``, or, when
no span can be delimited safely, only the role phrase (``"appoints new chief revenue
officer"``). ADR 0016 records this provisional policy.
"""

from __future__ import annotations

import re
from typing import Final, Literal

__all__ = [
    "CATEGORIES",
    "LEADERSHIP_VERBS",
    "PERSON",
    "RULES",
    "Category",
    "classify",
    "event_type",
    "person_spans",
    "redact_leadership",
]

Category = Literal["leadership", "funding", "launch", "expansion"]
CATEGORIES: Final[tuple[Category, ...]] = ("leadership", "funding", "launch", "expansion")
"""Category order is rule priority: the first category with a matching rule wins."""

_RULE_SOURCES: Final[tuple[tuple[Category, str, str], ...]] = (
    ("leadership", "appoints", r"\bappoints\b"),
    (
        "leadership",
        "names_as",
        r"\bnames\b.*\bas (?:(?:its|their|the|a|new) )*(?:chief|vp|vice president|head of|president)\b",
    ),
    ("leadership", "joins_as", r"\bjoins\b.*\bas\b"),
    ("leadership", "hires_as", r"\bhires\b.*\bas\b"),
    ("leadership", "promotes_to", r"\bpromotes\b.*\bto\b"),
    ("funding", "raises", r"\braises\b"),
    ("funding", "series", r"\bseries [a-e]\b"),
    ("funding", "seed", r"\bseed (?:round|funding)\b"),
    ("funding", "funding_round", r"\bfunding round\b"),
    ("funding", "closes_round", r"\bcloses\b.*\bround\b"),
    ("funding", "secures_dollars", r"\bsecures \$"),
    ("funding", "secures_funding", r"\bsecures (?:[\w-]+ ){0,2}funding\b"),
    (
        "funding",
        "gets_in_funding",
        r"\bgets \$\d+(?:[.,]\d+)?\s?(?:k|m|mn|million|b|bn|billion)? in (?:[\w-]+ )?funding\b",
    ),
    (
        "funding",
        "dollar_amount",
        r"\$\d+(?:\.\d+)?\s?(?:m|million|b|billion) (?:round|funding|investment)\b",
    ),
    ("funding", "announces", r"\bannounces\b.*\b(?:funding|round|raise)\b"),
    ("funding", "funding", r"\bfunding\b"),
    ("launch", "launches", r"\blaunches\b"),
    ("launch", "introduces", r"\bintroduc(?:es|ing)\b"),
    ("launch", "unveils", r"\bunveils\b"),
    ("launch", "debuts", r"\bdebuts\b"),
    ("launch", "releases", r"\breleases\b"),
    ("launch", "rolls_out", r"\brolls out\b"),
    ("expansion", "opens_office", r"\bopens\b.*\boffice\b"),
    ("expansion", "expands_to", r"\bexpands (?:to|into)\b"),
    ("expansion", "new_office", r"\bnew (?:office|headquarters)\b"),
)

RULES: Final[tuple[tuple[Category, str, re.Pattern[str]], ...]] = tuple(
    (category, f"{category}.{name}", re.compile(pattern, re.IGNORECASE))
    for category, name, pattern in _RULE_SOURCES
)
"""``(category, rule name, compiled pattern)`` in priority order."""

_WHITESPACE = re.compile(r"\s+")


def classify(title: str) -> tuple[Category | None, str | None]:
    """Return ``(category, rule name)`` for the first rule matching ``title``.

    ``(None, None)`` when no rule matches. Whitespace is collapsed first so a
    headline split across lines in the feed still matches.
    """
    text = _WHITESPACE.sub(" ", title).strip()
    for category, name, pattern in RULES:
        if pattern.search(text):
            return category, name
    return None, None


def event_type(category: Category) -> str:
    """The ``events.type`` for a category: ``news_funding``, ``news_leadership``, ..."""
    return f"news_{category}"


# --- leadership redaction (ADR 0016, provisional) ----------------------------------------

PERSON: Final = "[person]"
"""What a person-name span is replaced with in a stored leadership headline."""

LEADERSHIP_VERBS: Final = frozenset(
    {
        "appoints", "names", "hires", "promotes", "taps", "welcomes",
        "succeeds", "succeeding", "replaces", "replacing",
    }
)  # fmt: skip
"""Verbs whose object is a person: the capitalised run right after one is a name.

``succeeds`` / ``replaces`` widen the orchestrator's list (``appoints|names|hires|
promotes|taps|welcomes``) so an outgoing executive named after them is redacted too.
The span after ``names`` is only taken in a leadership headline, which ``classify``
has already decided.
"""

_JOIN_VERBS: Final = frozenset({"joins"})
"""Verbs whose subject is a person: the capitalised run right before one is a name."""

_ROLE_WORDS: Final = frozenset(
    {
        "chief", "ceo", "cfo", "cto", "coo", "cmo", "cro", "cpo", "cio", "ciso", "cso",
        "president", "vp", "svp", "evp", "vice", "head", "general", "director", "chair",
        "chairman", "chairwoman", "chairperson", "board", "senior", "executive",
        "managing", "founder", "cofounder", "co-founder", "former", "ex", "veteran",
    }
)  # fmt: skip
"""Capitalised role words: never part of a name run, and a valid end for one."""

_BOUNDARY_WORDS: Final = frozenset(
    {
        "as", "to", "its", "their", "the", "a", "an", "new", "interim", "for", "from",
        "at", "after", "amid", "in", "on", "with",
    }
)  # fmt: skip
"""Words that may follow a name run (``appoints Jane Doe as ...``)."""

_STOP_WORDS: Final = _ROLE_WORDS | _BOUNDARY_WORDS
_AND_WORDS: Final = frozenset({"and", "&"})
_SEPARATORS: Final = frozenset({"-", "\u2013", "\u2014", "|"})
_MAX_RUN: Final = 4
_EDGE_PUNCT: Final = "\"'\u201c\u201d\u2018\u2019()[]{},.;:!?"

_FUNCTIONS: Final = (
    r"(?:business development|customer success|investor relations|global sales|sales|"
    r"marketing|engineering|product|operations|finance|people|talent|partnerships|growth|"
    r"revenue|technology|design|data|security|legal|strategy|communications|research|ai|"
    r"hr|it|policy)"
)
_ROLE: Final = (
    r"(?:chief(?:\s+[a-z&-]+){1,3}?\s+officer|chief\s+executive"
    r"|(?:senior\s+|executive\s+)?(?:vp|svp|evp|vice\s+president)(?:\s+of\s+" + _FUNCTIONS + r")?"
    r"|head\s+of\s+" + _FUNCTIONS + r"|general\s+counsel|general\s+manager"
    r"|(?:managing\s+)?director|chair(?:man|woman|person)?|board\s+member|co-?founder"
    r"|president|ciso|c[efmotrpis]o)"
)
_ROLE_PHRASE: Final = re.compile(
    r"(?:\b(?P<conn>as|to)\s+)?"
    r"(?P<quals>(?:(?:its|their|the|a|an|new|interim|first|next|permanent)\s+)*)"
    r"\b(?P<role>" + _ROLE + r")\b",
    re.IGNORECASE,
)
_VERB: Final = re.compile(
    r"\b(?P<verb>appoints|names|hires|promotes|taps|welcomes|joins)\b", re.IGNORECASE
)


def _core(token: str) -> str:
    return token.strip(_EDGE_PUNCT)


def _name_token(token: str) -> bool:
    """A capitalised token that is not a role or boundary word."""
    core = _core(token)
    return bool(core) and core[0].isupper() and core.casefold() not in _STOP_WORDS


def _tail(token: str) -> str:
    """Trailing punctuation of a token (``"Doe,"`` -> ``","``)."""
    return token[len(token.rstrip(_EDGE_PUNCT)) :]


def _ends_run(tokens: list[str], end: int) -> bool:
    """Whether a forward name run ending before ``tokens[end]`` stops at a safe boundary.

    Safe: the end of the headline, a boundary or role word, a separator (``-``,
    ``|``), or a colon on the run's last token. Anything else (a comma, ``and``, a
    lowercase word, a fifth capitalised token) leaves the run undelimited: it may not
    be the whole name, so it is not used and the caller falls back to the role phrase.
    """
    tail = _tail(tokens[end - 1])
    if ":" in tail:
        return True
    if end == len(tokens):
        return not any(mark in tail for mark in ",;")
    if any(mark in tail for mark in ",;.!?"):
        return False
    following = tokens[end]
    return following in _SEPARATORS or _core(following).casefold() in _STOP_WORDS


def _forward_run(tokens: list[str], start: int) -> int:
    """End index of the run of 1-4 name tokens starting at ``start`` (``start`` if none).

    Trailing punctuation closes the run, except the period of an initial or a short
    honorific (``Q.``, ``Dr.``, ``Jr.``).
    """
    end = start
    while end < len(tokens) and end - start < _MAX_RUN and _name_token(tokens[end]):
        end += 1
        tail = _tail(tokens[end - 1])
        if tail and not (tail == "." and len(_core(tokens[end - 1])) <= 3):
            break
    return end


def _after_verb(tokens: list[str], start: int) -> list[tuple[int, int]] | None:
    """Name runs right after a verb, following ``and`` / ``&`` to a second name.

    ``[]`` when no capitalised run follows the verb; ``None`` when one does but does
    not end at a safe boundary (:func:`_ends_run`), so it may hold an unbounded name.
    """
    found: list[tuple[int, int]] = []
    while True:
        end = _forward_run(tokens, start)
        if end == start:
            return found
        if _ends_run(tokens, end):
            return [*found, (start, end)]
        joined = end < len(tokens) and _core(tokens[end]).casefold() in _AND_WORDS
        if not joined or _tail(tokens[end - 1]):
            return None
        found.append((start, end))
        start = end + 1


def person_spans(title: str) -> list[tuple[int, int]]:
    """Token index ranges ``[start, end)`` of person-name runs in a whitespace-split title.

    Shapes (ADR 0016):

    * the run of 1-4 capitalised tokens right after a :data:`LEADERSHIP_VERBS` verb,
      kept only when it ends at a safe boundary (:func:`_ends_run`), plus a second run
      joined to it by ``and`` / ``&``;
    * after the first leadership verb, a run that follows ``and`` / ``&`` or a token
      ending in ``,`` / ``;`` and ends at a safe boundary (``..., [person] as COO``);
    * the run of 1-4 capitalised tokens right before ``joins`` (the nearest tokens,
      so a longer run still loses the name).

    Role and boundary words (``Chief``, ``CEO``, ``As``, ``New``, ...) never join a
    run, so title-case headlines keep their role. When any run after a verb is not
    delimited the result is ``[]``: keeping the rest of such a headline could keep
    part of a name, so :func:`redact_leadership` falls back to the role phrase.
    """
    tokens = title.split()
    spans: list[tuple[int, int]] = []
    seen_verb = False
    for index, token in enumerate(tokens):
        word = _core(token).casefold()
        if word in LEADERSHIP_VERBS:
            seen_verb = True
            after = _after_verb(tokens, index + 1)
            if after is None:
                return []
            spans.extend(after)
        elif word in _JOIN_VERBS:
            seen_verb = True
            start = index
            while start > 0 and index - start < _MAX_RUN and _name_token(tokens[start - 1]):
                start -= 1
            if start < index:
                spans.append((start, index))
        elif seen_verb and (word in _AND_WORDS or any(m in _tail(token) for m in ",;")):
            nxt = index + 1
            end = _forward_run(tokens, nxt)
            if end > nxt and _ends_run(tokens, end):
                spans.append((nxt, end))
    return spans


def _role_phrase(text: str) -> str:
    """The verb plus the role it names (``"appoints new chief revenue officer"``)."""
    verb = _VERB.search(text)
    if verb is None:
        return PERSON
    role = _ROLE_PHRASE.search(text, verb.end())
    if role is None:
        return f"{verb.group('verb')} {PERSON}"
    parts = (
        verb.group("verb"),
        role.group("conn"),
        role.group("quals").strip(),
        role.group("role"),
    )
    return " ".join(part for part in parts if part)


def redact_leadership(title: str) -> str:
    """The leadership headline tell stores: no person's name (ADR 0016, provisional).

    Every :func:`person_spans` run is replaced with :data:`PERSON` and the rest of the
    headline is kept. When no run is found, only the role phrase is kept (the verb, an
    ``as``/``to`` connector, qualifiers like ``new`` and the role), built from
    :data:`_ROLE_PHRASE`; without a recognisable role it is ``"<verb> [person]"``.
    The article URL stays the receipt for the full headline.
    """
    text = _WHITESPACE.sub(" ", title).strip()
    tokens = text.split(" ")
    spans = person_spans(text)
    if not spans:
        return _role_phrase(text)
    out: list[str] = []
    index = 0
    for start, end in sorted(spans):
        if start < index:
            continue
        out.extend(tokens[index:start])
        last = tokens[end - 1]
        trailing = last[len(last.rstrip(":")) :]
        out.append(PERSON + trailing)
        index = end
    out.extend(tokens[index:])
    return " ".join(out)
