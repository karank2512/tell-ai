"""Company-name normalization (spec section 6, steps 1-5).

Every name is normalized before any comparison, in this order:

1. Unicode NFKC, casefold, strip accents.
2. Remove legal suffixes at the **end** only (``inc``, ``inc.``, ``incorporated``,
   ``llc``, ``l.l.c.``, ``ltd``, ``limited``, ``corp``, ``corporation``, ``co``,
   ``company``, ``plc``, ``gmbh``, ``s.a.``, ``pbc``, ``holdings``) plus the common
   non-US forms in :data:`INTERNATIONAL_SUFFIXES` (``pte``, ``pty``, ``pvt``,
   ``private``, ``ltda``, ``sas``, ``sarl``, ``ag``, ``ab``, ``as``, ``aps``, ``bv``,
   ``nv``, ``oy``, ``srl``, ``s.l.``, ``llp``, ``lp``, ...; ADR 0018), and ``labs`` only
   when another word precedes it (``Acme Labs`` -> ``acme``; ``Labs`` stays).
   Stripping repeats, so ``Acme Holdings, Inc.`` -> ``acme`` and ``Acme Private
   Limited`` -> ``acme``; the last remaining word is never stripped.
3. Remove punctuation except internal hyphens; collapse whitespace; drop a leading ``the``.
   Brackets and quotes are cut *before* step 2 tokenises the name, so a suffix glued to
   one (``Acme (Inc.)``, ``"Acme Inc"``) is still recognised as a suffix.
4. :func:`normalize_name_alt`: a second form with digits spelled out (``3`` -> ``three``)
   and ``tech`` / ``technology`` / ``technologies`` unified to ``technologies``; ``ai`` is kept.
5. :func:`domain_label`: ``tldextract(domain).domain`` with the same punctuation rules.

Everything here is pure and deterministic; no network, no database.
"""

from __future__ import annotations

import re
import unicodedata

from tell.discovery import domain_label as _registrable_label

__all__ = [
    "INTERNATIONAL_SUFFIXES",
    "LEGAL_SUFFIXES",
    "SPEC_SUFFIXES",
    "domain_label",
    "normalize_name",
    "normalize_name_alt",
    "slug_words",
]

SPEC_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "l.l.c",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "gmbh",
        "s.a",
        "pbc",
        "holdings",
    }
)
"""The spec section 6 step 2 list (compared after trailing ``.``/``,`` are cut)."""

INTERNATIONAL_SUFFIXES: frozenset[str] = frozenset(
    {
        # Singapore / Australia / India / Malaysia
        "pte",
        "pty",
        "pvt",
        "private",
        "sdn",
        "bhd",
        # Latin America / Iberia / France / Italy
        "ltda",
        "s.a.p.i",
        "s.a.b",
        "c.v",
        "de",  # only ever reached as the tail of "S.A.P.I. de C.V." once "c.v" is cut
        "s.l",
        "sl",
        "s.r.l",
        "srl",
        "sas",
        "s.a.s",
        "sarl",
        "s.a.r.l",
        "s.p.a",
        # Germanic / Nordic / Benelux
        "ag",
        "ug",
        "ab",
        "as",
        "a/s",
        "aps",
        "oy",
        "oyj",
        "bv",
        "b.v",
        "nv",
        "n.v",
        # Japan
        "kk",
        "k.k",
        # partnerships
        "llp",
        "l.l.p",
        "lp",
        "l.p",
    }
)
"""Non-US legal forms stripped like the spec's list (ADR 0018).

The spec names ``gmbh`` and ``s.a.``; Form D issuers and YC companies also file as
``Pte. Ltd.``, ``Private Limited``, ``SAS``, ``ApS``, ``AG``, ``AB``, ``AS``, ``Ltda.``,
``S.L.`` and so on, and a suffix left in the name reads as an extra token that the
superset guard (rightly) refuses to auto-link. Only forms that are not ordinary English
words are listed (``spa`` and ``se`` are not), and like every suffix they are cut from
the end only and never as the last remaining word.
"""

LEGAL_SUFFIXES: frozenset[str] = SPEC_SUFFIXES | INTERNATIONAL_SUFFIXES
"""Every legal suffix removed from the end of a name."""

CONDITIONAL_SUFFIXES: frozenset[str] = frozenset({"labs"})
"""Suffixes removed only when another word precedes them."""

DIGIT_WORDS: dict[str, str] = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

TOKEN_SYNONYMS: dict[str, str] = {
    "tech": "technologies",
    "technology": "technologies",
    "technologies": "technologies",
}
"""Step 4 token unification; ``ai`` is deliberately absent (kept as is)."""

_APOSTROPHES = re.compile("[\u0027\u2019\u02bc`]")  # ASCII, curly and modifier apostrophes
_BRACKETS_QUOTES = re.compile(
    '[()\\[\\]{}"\u201c\u201d\u00ab\u00bb,]'
)  # cut before suffix stripping
_NON_WORD = re.compile(r"[^\w\s-]", re.UNICODE)
_LOOSE_HYPHEN = re.compile(r"(?<!\w)-+|-+(?!\w)")
_WHITESPACE = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")
_SLUG_SEPARATORS = re.compile(r"[-_.]+")


def _fold(text: str) -> str:
    """Step 1: NFKC, casefold, drop combining marks (accents)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    decomposed = unicodedata.normalize("NFD", folded)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped)


def _strip_suffixes(tokens: list[str]) -> list[str]:
    """Step 2: cut legal suffixes off the end, repeatedly, never emptying the name."""
    while len(tokens) > 1:
        last = tokens[-1].strip(".,-")
        if not last or last in LEGAL_SUFFIXES or last in CONDITIONAL_SUFFIXES:
            tokens.pop()
            continue
        break
    return tokens


def _punctuation(text: str) -> str:
    """Step 3: punctuation out (internal hyphens stay), whitespace collapsed."""
    text = _APOSTROPHES.sub("", text)
    text = _NON_WORD.sub(" ", text)
    text = text.replace("_", " ")
    text = _LOOSE_HYPHEN.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalize_name(name: str) -> str:
    """Steps 1-3 of the spec's normalization; ``""`` for an empty or punctuation-only name."""
    folded = _fold(name)
    tokens = _strip_suffixes(_BRACKETS_QUOTES.sub(" ", folded).split())
    cleaned = _punctuation(" ".join(tokens))
    words = cleaned.split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _spell_digits(token: str) -> str:
    return _DIGIT.sub(lambda m: DIGIT_WORDS[m.group(0)], token)


def normalize_name_alt(name: str) -> str:
    """Step 4: the normalized name with digits spelled out and ``tech`` tokens unified."""
    words = [
        TOKEN_SYNONYMS.get(_spell_digits(w), _spell_digits(w)) for w in normalize_name(name).split()
    ]
    return " ".join(words)


def domain_label(domain: str) -> str:
    """Step 5: the registrable label of ``domain`` (``acme.co.uk`` -> ``acme``), punctuation rules applied."""
    return _punctuation(_fold(_registrable_label(domain)))


def slug_words(identifier: str) -> str:
    """An ATS board identifier as words: ``acme-labs`` / ``acme_labs`` -> ``acme labs``.

    Board tokens are slugs, not names; turning separators into spaces lets the
    legal-suffix and ``labs`` rules apply to them (``acme-labs-inc`` -> ``acme``).
    """
    return _SLUG_SEPARATORS.sub(" ", identifier).strip()
