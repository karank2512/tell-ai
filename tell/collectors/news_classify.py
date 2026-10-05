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
"""

from __future__ import annotations

import re
from typing import Final, Literal

__all__ = [
    "CATEGORIES",
    "RULES",
    "Category",
    "classify",
    "event_type",
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
    (
        "funding",
        "dollar_amount",
        r"\$\d+(?:\.\d+)?\s?(?:m|million|b|billion) (?:round|funding|investment)\b",
    ),
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
