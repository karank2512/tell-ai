"""Site evidence for issuer-first discovery (ADR 0035): does this domain belong to this CIK?

Issuer-first discovery (:mod:`tell.discovery.issuers`) guesses domains for a Form D issuer
and fetches a few pages of each. The filing-to-company link is exact by CIK, so precision
rests on one question, answered here from the fetched page text and the issuer's own
filing facts. Everything in this module is pure: no network, no database.

Tiers, per domain (:func:`assess`):

* **Tier A**: a page body states the issuer's **full legal name**: every token of the
  name, descriptors included, in order (a name from ``issuerPreviousNameList`` counts),
  followed by a legal form **of the issuer's own family** (``Inc`` / ``Incorporated``;
  ``Corp`` / ``Corporation``; ``PBC`` / ``Public Benefit Corporation``; ``Co.`` /
  ``Company``: :data:`LEGAL_FORM_FAMILIES`) that ends on a word boundary, so "Payload,
  including" is not "Payload, Inc", and that is not followed by another legal form
  (``cado Co., Ltd.`` is not ``Cado Inc.``; :data:`OTHER_LEGAL_FORMS`). The name may not be
  preceded by a letter, a digit, ``.``, ``@`` or ``/`` (``communion.co for sale``, an
  e-mail address, a URL path).
* **Tier B**: one page body states the **full brand** (the normalized issuer name, every
  token including descriptors: "Restara Medical", not "Restara") **and** an address in the
  Form D city (``City, ST`` / ``City, State``) or, failing the city, the Form D state with a
  ZIP code (``ST 12345``). Never when the Form D address is a registered-agent city
  (:data:`REGISTERED_AGENT_CITIES`): the agent's address says nothing about the company.
* **Tier C**: weaker: the distinctive brand words (the brand minus filler and descriptor
  tokens) appear in the title, the description or the body, or tier A/B text was found but
  a disqualifier fired. Tier C is never evidence: it opens a review item at most.

Disqualifiers (A/B become C; the reason is in :attr:`SiteEvidence.reasons`):

* ``different_state``: a US address the site states as its own (:func:`site_addresses`:
  ZIP-coded, or a street line plus ``City, ST``; not privacy-law boilerplate, not another
  entity's office, not a registered agent's) is in a state other than the Form D state.
  For tier A its city must differ too, and a Form D address in a registered-agent city is
  never compared (it says nothing about where the company is).
* ``older_site`` / ``stale_site`` (the age check, :func:`site_years`): the site's earliest
  copyright or founding year is more than :data:`AGE_SLACK_YEARS` years before the
  filing's year of incorporation, or its latest copyright year is before the filing year
  minus :data:`STALE_SLACK_YEARS`; only when the page states such years. An older company
  with the issuer's exact legal name (Core Automation, Inc., (c) 2001-2022, against a 2026
  issuer) fails it.
* ``other_legal_form``: the legal name is there, but with another family's form (the site
  says "Runwayz, Inc." for "Runwayz Corp."); recorded only when nothing better is found.

Always required for A/B: the domain is not a platform's
(:func:`tell.discovery.is_platform_domain`) and the homepage is not a parked or for-sale
page. Never evidence on their own: a matching ``<title>`` or meta
description (title-only), an ATS board name (the board only proposes a domain), and a
shared core word plus the same city (mangrove.ai is a trading app in Sacramento; the
issuer is Mangrove Therapeutics, also in Sacramento: the full brand is absent, so it is
tier C at most).

Page text is kept as blocks (:func:`parse_page`), one per block-level element, joined
with :data:`BLOCK_SEPARATOR`, and no name or address pattern crosses a separator. Run
together, "Move Fast" and "Way Biosolutions, Inc." read "FastWay Biosolutions, Inc." and
the match fails; that happened in the design's second pass. Control bytes are removed
before parsing (:func:`sanitize_markup`), and a page lxml still cannot read is an empty
page, never an exception.

Across domains (:func:`decide`): exactly one domain must qualify at A or B; two or more
make the issuer a conflict for review.

What may be stored is :meth:`SiteEvidence.record`: ``{path, span}``, the page path and the
matched legal-name (tier A) or brand (tier B) string, never surrounding text (it can hold a
street address or a phone number). Street addresses are read in memory only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html

from tell.discovery import is_platform_domain, registrable_domain
from tell.resolve.aliases import DESCRIPTOR_TOKENS, GENERIC_ALIAS_TOKENS
from tell.resolve.normalize import normalize_name

__all__ = [
    "AGE_SLACK_YEARS",
    "BLOCK_SEPARATOR",
    "LEGAL_FORMS",
    "LEGAL_FORM_FAMILIES",
    "LEGAL_PATHS",
    "MAX_PARSE_BYTES",
    "OTHER_LEGAL_FORMS",
    "PARKED_TEXT_LIMIT",
    "REGISTERED_AGENT_CITIES",
    "STALE_SLACK_YEARS",
    "THIRD_PARTY_WINDOW",
    "US_STATES",
    "Decision",
    "IssuerProfile",
    "SiteAddress",
    "SiteEvidence",
    "SitePage",
    "SiteYears",
    "Tier",
    "assess",
    "brand_words",
    "core_words",
    "decide",
    "is_parked",
    "legal_base",
    "legal_form_families",
    "legal_links",
    "parse_page",
    "sanitize_markup",
    "site_addresses",
    "site_years",
]

Tier = Literal["A", "B", "C"]
Outcome = Literal["linked", "several_domains", "tier_c", "no_domain"]

BLOCK_SEPARATOR = "\n"
"""Joins page blocks; no name or address pattern matches across it."""

LEGAL_PATHS: tuple[str, ...] = (
    "/terms",
    "/privacy",
    "/terms-of-service",
    "/privacy-policy",
    "/legal",
)
"""The fixed legal pages read after the homepage (plus the footer's terms/privacy links)."""

MAX_PARSE_BYTES = 2_000_000
"""Only the first this-many bytes of a page are parsed."""

PARKED_TEXT_LIMIT = 3000
"""A page with a parked/for-sale phrase is parked only when its text is shorter than this."""

LEGAL_FORM_FAMILIES: dict[str, tuple[str, ...]] = {
    "inc": ("inc", "incorporated"),
    "corp": ("corp", "corporation"),
    "pbc": ("pbc", "public benefit corporation"),
    "co": ("co", "company"),
}
"""The legal forms that end a tier A legal name, by family. The site's form must be in the
family of the issuer's own (``Cado Inc.`` is not ``cado Co., Ltd.``); ``co`` is written
``Co.`` (with its period) or ``Company``."""

LEGAL_FORMS: tuple[str, ...] = tuple(f for forms in LEGAL_FORM_FAMILIES.values() for f in forms)
"""Every legal form that can end a tier A legal name (:data:`LEGAL_FORM_FAMILIES`)."""

_STRIPPED_FORMS: frozenset[str] = frozenset(
    {"inc", "incorporated", "corp", "corporation", "pbc", "co", "company"}
    | {"llc", "ltd", "limited", "plc", "lp", "llp"}
)
"""Trailing tokens cut from the issuer's name to get its legal base (never the last one)."""

_FORM_FAMILY: dict[str, str] = {
    "inc": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corporation": "corp",
    "pbc": "pbc",
    "co": "co",
    "company": "co",
}

_FAMILY_SUFFIX: dict[str, str] = {
    "inc": r"inc\b\.?|incorporated\b",
    "corp": r"corp\b\.?|corporation\b",
    "pbc": r"pbc\b|p\.b\.c\.|public benefit corp(?:oration\b|\b\.?)",
    "co": r"co\.(?!\w)|(?-i:Company|COMPANY)\b",
}
"""Each family's forms as a pattern; ``\\b`` after the form keeps "including" out."""

_SUFFIX = "(?:" + "|".join(_FAMILY_SUFFIX.values()) + ")"
"""Any legal form of :data:`LEGAL_FORM_FAMILIES` after the name."""

OTHER_LEGAL_FORMS: tuple[str, ...] = (
    "Ltd",
    "Limited",
    "GmbH",
    "K.K.",
    "S.A.",
    "B.V.",
    "Pty",
    "LLC",
    "PLC",
)
"""A legal form that, right after the matched one, makes the match another entity's
(``cado Co., Ltd.``, ``Acme Inc. GmbH``): such a match is never tier A."""

_NOT_OTHER_FORM = (
    r"(?!\.?[ \t]*,?[ \t]*(?:ltd|limited|gmbh|pty|llc|l\.l\.c|plc|k\.[ ]?k|s\.[ ]?a|b\.[ ]?v)"
    r"(?![a-z]))"
)

_NAME_GAP = r"[ \-.'\u2019&+]{0,3}"
"""Between two brand tokens (tiers B and C): nothing (``WayBio``) or up to three spaces or
punctuation characters. Never a :data:`BLOCK_SEPARATOR`."""

_LEGAL_GAP = r"[ \-.'\u2019]{0,3}"
"""Between two legal-name tokens (tier A): nothing or up to three spaces, hyphens, periods
or apostrophes. ``&`` and ``+`` are allowed only where the issuer's own name has one
(:data:`_LEGAL_JOIN_GAP`), so a stored span never holds a token the legal name lacks."""

_LEGAL_JOIN_GAP = r"(?: ?[&+] ?|[ \-.'\u2019]{0,3})"

_NOT_AFTER = r"(?<![\w.@/])"
"""A name is not preceded by a word character, ``.``, ``@`` or ``/``."""

US_STATES: dict[str, str] = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "DC": "district of columbia",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new hampshire",
    "NJ": "new jersey",
    "NM": "new mexico",
    "NY": "new york",
    "NC": "north carolina",
    "ND": "north dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode island",
    "SC": "south carolina",
    "SD": "south dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
}
"""Form D ``stateOrCountry`` codes of the US states and DC, with their names (lower case)."""

REGISTERED_AGENT_CITIES: frozenset[tuple[str, str]] = frozenset(
    {
        ("wilmington", "DE"),
        ("dover", "DE"),
        ("lewes", "DE"),
        ("newark", "DE"),
        ("sheridan", "WY"),
    }
)
"""Form D addresses that are usually a registered agent's, not the company's: tier B is
never accepted for an issuer whose Form D city is one of these."""

_PARKED = re.compile(
    r"domain (?:name )?(?:is )?for sale|buy this domain|this domain (?:may be|is) for sale"
    r"|\bfor sale\b|parked (?:free|domain)|hugedomains|afternic|sedo\.com|dan\.com"
    r"|domain has expired|under construction|coming soon",
    re.IGNORECASE,
)
"""Phrases of a parked, for-sale or placeholder page (checked only on short pages)."""

_STATE_NAMES = "|".join(
    re.escape(name) for name in sorted(US_STATES.values(), key=len, reverse=True)
)
_STATE_CODES = "|".join(sorted(US_STATES))
_ZIP = r"\d{5}(?:-\d{4})?(?!\d)"
_JURISDICTION_TAIL = re.compile(rf"\s*/\s*(?:{_STATE_CODES})\s*/?\s*$")
"""EDGAR's state tag after a conformed name (``Cicero, Inc /DE/``, ``AiGent, Inc. / DE``)."""
_ZIP_ADDRESS = re.compile(
    rf"(?<![A-Za-z])(?P<code>{_STATE_CODES}),? {_ZIP}"
    rf"|(?<![\w])(?P<name>(?i:{_STATE_NAMES})),? {_ZIP}"
)
"""A ZIP-coded US address: ``WI 53703`` or ``North Carolina 27606`` (codes upper case)."""

_STREET_ADDRESS = re.compile(
    r"(?<![\w.])\d{1,6}[A-Za-z]? (?:[^\W_][\w.'#-]* ){0,5}[^\W_][\w.'#-]*,"
    r"(?: (?:Suite|Ste\.?|Unit|Floor|Fl\.?|#) ?[\w-]{1,8},)?"
    rf" (?P<city>[A-Z][a-z]+(?: [A-Z][a-z]+){{0,2}}),? (?:(?P<code>{_STATE_CODES})(?![A-Za-z])"
    rf"|(?P<name>(?i:{_STATE_NAMES}))(?!\w))"
)
"""A US street address without a ZIP code: a street line starting with a house number,
then ``City, ST`` / ``City State`` (``500 Main Street, Suite 4, Austin, TX``). The street
line is what makes it an address: a bare ``City, ST`` is often a venue or a law ("Shine
the Light, CA Civil Code"), not where the company is."""

_CITY_BEFORE = re.compile(
    r"(?<![\w])([A-Z][A-Za-z.'\u2019-]*(?: [A-Z][A-Za-z.'\u2019-]*){0,2}),? $"
)
"""The city right before a ZIP-coded address's state (``Charlotte, `` of ``Charlotte, NC
28273``), read from the text before the match."""

_LAW_STATE = re.compile(
    rf"(?<![\w])(?P<name>(?i:{_STATE_NAMES}))(?:['\u2019]s)? (?i:residents?\b|consumers?\b"
    r"|consumer privacy act|privacy rights act|civil code|privacy laws?\b|laws?\b"
    r"|shine the light|attorney general)"
    rf"|(?i:residents? of|laws of the state of|laws of) (?P<other>(?i:{_STATE_NAMES}))(?!\w)"
)
"""A state named by privacy-law boilerplate ("California residents", "California Consumer
Privacy Act", "residents of Nevada"): that state is the law's, not the site's address."""

_CALIFORNIA_LAW = re.compile(r"\bCCPA\b|\bCPRA\b|(?i:shine the light|civ(?:il|\.) code)")
"""California's privacy laws named without the state ("CCPA", "Shine the Light")."""

_ATTORNEY_GENERAL = re.compile(r"(?i:attorney general)")
"""A block that names an attorney general gives a regulator's address, never the site's."""

_ENTITY_FORM = re.compile(
    r"(?i:\b(?:inc|incorporated|corp|corporation|llc|l\.l\.c|ltd|limited|llp|plc|gmbh|pbc)\b)"
)
"""A legal form in running text: the address after it is that entity's (third-party check)."""

THIRD_PARTY_WINDOW = 200
"""How far before an address (characters, same block) a legal form attributes it."""

_YEAR = r"(?<!\d)(?P<{}>1[89]\d\d|20\d\d)(?!\d)"
_COPYRIGHT = re.compile(
    r"(?:(?i:\bcopyright\b)(?:[ ]?(?:©|\((?i:c)\)))?[ ]?"
    r"|(?:©|\((?i:c)\))[ ]?(?:[^\d©\n]{0,40}?[ ])?)"
    + _YEAR.format("first")
    + r"(?:[ ]?[-\u2013\u2014][ ]?"
    + _YEAR.format("last")
    + r")?"
)
"""A copyright notice's year or years (``© 2001 - 2022 Core Automation, Inc.``,
``Copyright © 2026``, ``© Acme Inc. 2024``). After the bare word "copyright" only a ``©``
may come before the year, so "Copyright Act of 1976" is no notice."""

_FOUNDED = re.compile(r"(?i:\b(?:founded|established|est\.?)(?: in)?) " + _YEAR.format("first"))
"""A founding year (``Founded in 2001``, ``Est. 1998``)."""

AGE_SLACK_YEARS = 2
"""A site whose earliest copyright or founding year is more than this many years before
the issuer's year of incorporation belongs to an older company (``older_site``)."""

STALE_SLACK_YEARS = 1
"""A site whose latest copyright year is before the filing year minus this belongs to a
company that stopped updating it before the filing (``stale_site``)."""

_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "button",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "option",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
"""Elements whose boundaries end a text block (``<a>``, ``<span>``, ``<b>`` do not)."""

_DROPPED_TAGS: tuple[str, ...] = ("script", "style", "noscript", "template", "svg", "head")
_LEGAL_LINK = re.compile(r"terms|privacy|legal", re.IGNORECASE)
_TOKEN = re.compile(r"[^\W_]+")
_WORD_SPLIT = re.compile(r"[\s-]+")


# --- the issuer ------------------------------------------------------------------------------


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


@dataclass(frozen=True)
class _LegalName:
    """An issuer name split for tier A: its base tokens, the text between them, and the
    families (:data:`LEGAL_FORM_FAMILIES`) of the legal forms cut from its end."""

    tokens: tuple[str, ...]
    separators: tuple[str, ...]
    families: frozenset[str]
    other_form: bool = False
    """The name ends in a form outside every family (``LLC``, ``Ltd``): no tier A form fits."""


_PUBLIC_BENEFIT = ("public", "benefit")


def _legal_name(name: str) -> _LegalName:
    folded = _JURISDICTION_TAIL.sub("", _fold(name))
    matches = list(_TOKEN.finditer(folded))
    families: set[str] = set()
    other_form = False
    while len(matches) > 1 and matches[-1].group(0).casefold() in _STRIPPED_FORMS:
        form = matches.pop().group(0).casefold()
        tail = tuple(m.group(0).casefold() for m in matches[-2:])
        if form in ("corp", "corporation") and tail == _PUBLIC_BENEFIT and len(matches) > 2:
            del matches[-2:]
            families.add("pbc")
        elif form in _FORM_FAMILY:
            families.add(_FORM_FAMILY[form])
        else:
            other_form = True
    tokens = tuple(m.group(0) for m in matches)
    separators = tuple(folded[left.end() : right.start()] for left, right in pairwise(matches))
    return _LegalName(tokens, separators, frozenset(families), other_form and not families)


def legal_base(name: str) -> list[str]:
    """The issuer name's tokens without its trailing legal forms (never the last token) and
    without EDGAR's state tag (``/DE/``).

    ``"Saltbox Tinned Fish Co., PBC"`` -> ``["Saltbox", "Tinned", "Fish"]``;
    ``"Way Biosolutions, Inc."`` -> ``["Way", "Biosolutions"]``;
    ``"Cicero, Inc /DE/"`` -> ``["Cicero"]``.
    """
    return list(_legal_name(name).tokens)


def legal_form_families(name: str) -> frozenset[str]:
    """The families of the issuer's own legal forms (``"Cado Inc."`` -> ``{"inc"}``,
    ``"Saltbox Tinned Fish Co., PBC"`` -> ``{"co", "pbc"}``); empty when it states none of
    :data:`LEGAL_FORM_FAMILIES` (an ``LLC``, or no form at all)."""
    return _legal_name(name).families


def _legal_pattern(name: str, *, any_form: bool = False) -> re.Pattern[str] | None:
    """The tier A pattern of one legal name: its base tokens in order, then a legal form of
    its own family (any family when ``any_form`` or when the name states no form at all;
    none when it states only another form, such as ``LLC``), not followed by another legal
    form (:data:`OTHER_LEGAL_FORMS`)."""
    parts = _legal_name(name)
    if not parts.tokens or (parts.other_form and not any_form):
        return None
    body = re.escape(parts.tokens[0])
    for separator, token in zip(parts.separators, parts.tokens[1:], strict=True):
        gap = _LEGAL_JOIN_GAP if ("&" in separator or "+" in separator) else _LEGAL_GAP
        body += gap + re.escape(token)
    families = parts.families if parts.families and not any_form else LEGAL_FORM_FAMILIES
    suffix = "|".join(_FAMILY_SUFFIX[f] for f in LEGAL_FORM_FAMILIES if f in families)
    return re.compile(
        _NOT_AFTER + body + r",? ?(?:" + suffix + ")" + _NOT_OTHER_FORM, re.IGNORECASE
    )


def brand_words(name: str) -> list[str]:
    """The full brand: the normalized name's words, descriptors included (``restara medical``)."""
    return [w for w in _WORD_SPLIT.split(normalize_name(name)) if w]


def core_words(name: str) -> list[str]:
    """The distinctive brand words: :func:`brand_words` minus filler and descriptor tokens.

    Falls back to the brand words when nothing distinctive is left.
    """
    words = brand_words(name)
    core = [w for w in words if w not in GENERIC_ALIAS_TOKENS and w not in DESCRIPTOR_TOKENS]
    return core or words


def _words_pattern(words: Sequence[str]) -> str:
    return _NOT_AFTER + _NAME_GAP.join(re.escape(w) for w in words)


@dataclass(frozen=True)
class IssuerProfile:
    """What the issuer's Form D says about it: what :func:`assess` compares a site with.

    ``state`` is the Form D ``stateOrCountry`` code (``CA``); ``city`` as filed;
    ``year_of_inc`` and ``filed_year`` feed the age check (:func:`site_years`).
    """

    cik: str
    name: str
    previous_names: tuple[str, ...] = ()
    city: str | None = None
    state: str | None = None
    year_of_inc: int | None = None
    """The Form D ``yearOfInc`` (the age check reads it; ``None`` skips that half)."""
    filed_year: int | None = None
    """The year the filing was made (``None`` skips the stale-site half of the age check)."""

    @property
    def state_code(self) -> str | None:
        code = (self.state or "").strip().upper()
        return code if code in US_STATES else None

    @property
    def agent_address(self) -> bool:
        """The Form D address is a registered-agent city (tier B is never accepted)."""
        city = " ".join((self.city or "").split()).casefold()
        return (city, (self.state or "").strip().upper()) in REGISTERED_AGENT_CITIES

    def legal_patterns(self, *, any_form: bool = False) -> list[re.Pattern[str]]:
        """One tier A pattern per legal name (current, then previous).

        The site's legal form must be in the family of that name's own form (Inc /
        Incorporated, Corp / Corporation, PBC / Public Benefit Corporation, Co. / Company;
        any family when the name states none, or with ``any_form``) and may not be followed
        by another legal form (``cado Co., Ltd.``). A match is the name's tokens with at
        most three spaces or punctuation characters between them (``&`` or ``+`` only where
        the name has one), ``", "`` and the form, so a stored span never carries page text
        or a token beyond the legal name and its suffix.
        """
        out: list[re.Pattern[str]] = []
        for name in (self.name, *self.previous_names):
            pattern = _legal_pattern(name, any_form=any_form)
            if pattern is not None:
                out.append(pattern)
        return out

    def brand_pattern(self) -> re.Pattern[str] | None:
        words = brand_words(self.name)
        if not words:
            return None
        return re.compile(_words_pattern(words) + r"(?!\w)", re.IGNORECASE)

    def core_pattern(self) -> re.Pattern[str] | None:
        words = core_words(self.name)
        if not words:
            return None
        return re.compile(_words_pattern(words) + r"(?!\w)", re.IGNORECASE)

    def location_pattern(self) -> re.Pattern[str] | None:
        """``City, ST`` / ``City, State`` for the Form D city, else ``ST 12345`` / ``State 12345``."""
        code = self.state_code
        if code is None:
            return None
        state_name = re.escape(US_STATES[code])
        city_words = (self.city or "").split()
        if city_words:
            city = " ".join(re.escape(w) for w in city_words)  # never \s: no BLOCK_SEPARATOR
            return re.compile(
                rf"(?<![\w])(?i:{city}),? (?:{code}(?![A-Za-z])|(?i:{state_name})(?!\w))"
                rf"|(?<![A-Za-z]){code},? {_ZIP}|(?<![\w])(?i:{state_name}),? {_ZIP}"
            )
        return re.compile(rf"(?<![A-Za-z]){code},? {_ZIP}|(?<![\w])(?i:{state_name}),? {_ZIP}")


# --- pages -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SitePage:
    """One fetched page: its path, ``<title>``, meta description and visible text blocks."""

    path: str
    title: str = ""
    description: str = ""
    blocks: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The body text, blocks joined with :data:`BLOCK_SEPARATOR`."""
        return BLOCK_SEPARATOR.join(self.blocks)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SitePage:
        return cls(
            path=str(data.get("path") or "/"),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            blocks=tuple(str(b) for b in data.get("blocks") or ()),
        )


_CONTROL_BYTES = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""C0 control bytes (tab, newline and carriage return kept) and DEL: lxml refuses them in
any string it builds ("All strings must be XML compatible"), so a page holding one crashed
the parse during the 2026-10-15 shadow run."""

_WIDE_BOMS: tuple[bytes, ...] = (b"\xff\xfe", b"\xfe\xff")
"""UTF-16 / UTF-32 byte order marks: there a zero byte is part of a character."""


def sanitize_markup(content: bytes) -> bytes:
    """``content`` without control bytes (:data:`_CONTROL_BYTES`), cut to
    :data:`MAX_PARSE_BYTES`. A UTF-16 or UTF-32 page (by its byte order mark) is left alone:
    its zero bytes belong to characters."""
    content = content[:MAX_PARSE_BYTES]
    if content.startswith(_WIDE_BOMS):
        return content
    return _CONTROL_BYTES.sub(b"", content)


def _document(content: bytes) -> Any | None:
    if not content or not content.strip():
        return None
    try:
        return lxml_html.document_fromstring(sanitize_markup(content))
    except (etree.ParserError, ValueError):
        return None


def _squash(text: str) -> str:
    return " ".join(text.split())


def parse_page(content: bytes, path: str) -> SitePage:
    """Title, meta description and the visible text of ``content`` as blocks.

    Scripts, styles, templates, SVG and comments are dropped; every block-level element
    (:data:`_BLOCK_TAGS`) starts and ends a block; whitespace inside a block collapses to one
    space; empty blocks are dropped. Control bytes are removed first
    (:func:`sanitize_markup`); a page lxml still cannot read is an empty page (no evidence),
    never an exception.
    """
    try:
        return _parse_page(content, path)
    except (etree.LxmlError, ValueError):
        return SitePage(path)


def _parse_page(content: bytes, path: str) -> SitePage:
    document = _document(content)
    if document is None:
        return SitePage(path)
    title = _squash(document.findtext(".//title") or "")
    description = ""
    for meta in document.iter("meta"):
        key = (meta.get("name") or meta.get("property") or "").casefold()
        if key in ("description", "og:description") and meta.get("content"):
            description = _squash(meta.get("content") or "")
            break
    for element in list(document.iter(*_DROPPED_TAGS, etree.Comment)):
        element.drop_tree()
    parts: list[str] = []
    for event, element in etree.iterwalk(document, events=("start", "end")):
        named = isinstance(element.tag, str)
        block = named and element.tag.lower() in _BLOCK_TAGS
        if event == "start":
            if block:
                parts.append(BLOCK_SEPARATOR)
            if named and element.text:
                parts.append(element.text)
        else:
            if block:
                parts.append(BLOCK_SEPARATOR)
            if element.tail:
                parts.append(element.tail)
    blocks = tuple(
        squashed
        for squashed in (_squash(b) for b in "".join(parts).split(BLOCK_SEPARATOR))
        if squashed
    )
    return SitePage(path, title=title, description=description, blocks=blocks)


def legal_links(content: bytes, base_url: str, domain: str) -> list[str]:
    """Paths of the page's terms / privacy / legal links on ``domain`` (the footer's links).

    A link counts when its URL or its text names terms, privacy or legal and it stays on
    the same registrable domain. Paths come back in page order, without duplicates.
    """
    document = _document(content)
    if document is None:
        return []
    paths: list[str] = []
    for anchor in document.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        label = _squash(anchor.text_content() or "")
        if not (_LEGAL_LINK.search(href) or _LEGAL_LINK.search(label)):
            continue
        try:
            url = urljoin(base_url, href)
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or registrable_domain(url) != domain:
            continue
        path = parts.path or "/"
        if path != "/" and path not in paths:
            paths.append(path)
    return paths


def is_parked(page: SitePage, domain: str | None = None) -> bool:
    """A parked, for-sale or placeholder homepage.

    A parking phrase (:data:`_PARKED`) on a short page, or a page with no text at all but
    a title that is empty or the domain itself (``palermo.io`` titled "palermo.io").
    """
    text = " ".join((page.title, page.description, page.text))
    if len(page.text) < PARKED_TEXT_LIMIT and _PARKED.search(text) is not None:
        return True
    if page.blocks or page.description:
        return False
    title = page.title.strip().casefold()
    return not title or (domain is not None and title in (domain, f"www.{domain}"))


# --- one domain ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteEvidence:
    """What one domain's pages say about the issuer (:func:`assess`)."""

    domain: str
    tier: Tier | None
    path: str | None = None
    """The page that holds the tier A/B span (or the tier C mention)."""
    span: str | None = None
    """The matched legal name (tier A) or brand (tier B); for tier C the brand words seen."""
    brand: str | None = None
    """The company name as the site writes it (the span without its legal form)."""
    reasons: tuple[str, ...] = ()
    """Why the domain is not tier A/B (``parked``, ``platform_domain``, ``no_pages``,
    ``different_state``, ``older_site``, ``stale_site``, ``other_legal_form``,
    ``registered_agent_city``, ``title_only``, ``no_brand``)."""

    @property
    def qualifies(self) -> bool:
        """Tier A or B: site evidence."""
        return self.tier in ("A", "B")

    def record(self) -> dict[str, str] | None:
        """The stored evidence: ``{path, span}`` only (``issuer_discovery.evidence``)."""
        if self.path is None or self.span is None:
            return None
        return {"path": self.path, "span": self.span}


_TRAILING_FORM = re.compile(r",? ?" + _SUFFIX + r"$", re.IGNORECASE)


def _strip_form(span: str) -> str:
    return _TRAILING_FORM.sub("", span).strip(" ,") or span


def _legal_match(
    issuer: IssuerProfile, pages: Sequence[SitePage], *, any_form: bool = False
) -> tuple[str, str] | None:
    patterns = issuer.legal_patterns(any_form=any_form)
    for page in pages:
        for pattern in patterns:
            match = pattern.search(page.text)
            if match is not None:
                return page.path, match.group(0)
    return None


def _brand_with_address(issuer: IssuerProfile, pages: Sequence[SitePage]) -> tuple[str, str] | None:
    brand = issuer.brand_pattern()
    location = issuer.location_pattern()
    if brand is None or location is None:
        return None
    for page in pages:
        text = page.text
        found = brand.search(text)
        if found is not None and location.search(text) is not None:
            return page.path, found.group(0)
    return None


# --- addresses ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteAddress:
    """A US address a page states as the site's own (:func:`site_addresses`)."""

    state: str
    """The state code (``NC``)."""
    city: str | None = None
    """The city before the state, as written; ``None`` when none could be read."""


def _state_code(
    match: re.Match[str], code_group: str = "code", name_group: str = "name"
) -> str | None:
    code = match.group(code_group)
    if code is not None:
        return code
    name = (match.group(name_group) or "").casefold()
    return next((c for c, n in US_STATES.items() if n == name), None)


def _law_states(block: str) -> frozenset[str]:
    """States the block names as privacy-law boilerplate (:data:`_LAW_STATE`)."""
    states: set[str] = set()
    for match in _LAW_STATE.finditer(block):
        group = "name" if match.group("name") is not None else "other"
        name = (match.group(group) or "").casefold()
        states.update(c for c, n in US_STATES.items() if n == name)
    if _CALIFORNIA_LAW.search(block) is not None:
        states.add("CA")
    return frozenset(states)


def _names_issuer(issuer: IssuerProfile, text: str) -> bool:
    patterns = [
        issuer.brand_pattern(),
        issuer.core_pattern(),
        *issuer.legal_patterns(any_form=True),
    ]
    return any(p is not None and p.search(text) is not None for p in patterns)


def _third_party(issuer: IssuerProfile, block: str, start: int) -> bool:
    """The address at ``start`` follows another entity's legal name in its block ("Example
    Cloud, Inc. a Delaware corporation having its principal place of business at ..."):
    the nearest legal form in the :data:`THIRD_PARTY_WINDOW` characters before it does not
    end a name of the issuer's."""
    window = block[max(0, start - THIRD_PARTY_WINDOW) : start]
    forms = list(_ENTITY_FORM.finditer(window))
    if not forms:
        return False
    last = forms[-1]
    return not _names_issuer(issuer, window[max(0, last.start() - 120) : last.start()])


def site_addresses(issuer: IssuerProfile, pages: Iterable[SitePage]) -> list[SiteAddress]:
    """The US addresses ``pages`` state as the site's own: ZIP-coded (:data:`_ZIP_ADDRESS`)
    or a street line plus ``City, ST`` (:data:`_STREET_ADDRESS`), block by block.

    Not the site's own, so left out: an address in a state the same block names as
    privacy-law boilerplate ("California residents", "CCPA"; :data:`_LAW_STATE`), any
    address in a block naming an attorney general, an address after another entity's legal
    name (a vendor's or processor's office; :func:`_third_party`), and an address in a
    registered-agent city (:data:`REGISTERED_AGENT_CITIES`: the agent's, not the company's).
    Not detected: an address abroad.
    """
    out: list[SiteAddress] = []
    for page in pages:
        for block in page.blocks:
            if _ATTORNEY_GENERAL.search(block) is not None:
                continue
            law = _law_states(block)
            for match in (*_ZIP_ADDRESS.finditer(block), *_STREET_ADDRESS.finditer(block)):
                code = _state_code(match)
                if code is None or code in law:
                    continue
                city = match.groupdict().get("city")
                if city is None:
                    before = _CITY_BEFORE.search(block[max(0, match.start() - 60) : match.start()])
                    city = before.group(1) if before is not None else None
                if city is not None and (_city_key(city), code) in REGISTERED_AGENT_CITIES:
                    continue
                if _third_party(issuer, block, match.start()):
                    continue
                address = SiteAddress(code, city)
                if address not in out:  # a street line with a ZIP matches both patterns
                    out.append(address)
    return out


def _city_key(city: str) -> str:
    return " ".join(city.split()).casefold()


def _same_city(filed: str | None, stated: str | None) -> bool:
    """The site's city is the filing's (``San Francisco`` in ``Mission Street San
    Francisco``); unknown on either side is not the same."""
    if not filed or not stated:
        return False
    want, got = _city_key(filed), _city_key(stated)
    return got == want or got.endswith(" " + want)


def _address_conflict(
    issuer: IssuerProfile, addresses: Iterable[SiteAddress], *, other_city: bool
) -> bool:
    """An address in a state other than the Form D state (for an issuer abroad, any US
    address); with ``other_city`` (tier A) only when its city differs from the filing's too."""
    own = issuer.state_code
    for address in addresses:
        if address.state == own:
            continue
        if other_city and _same_city(issuer.city, address.city):
            continue
        return True
    return False


# --- age -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteYears:
    """The years the site's copyright notices and founding lines state (:func:`site_years`)."""

    earliest: int | None = None
    """The earliest copyright or founding year."""
    latest_copyright: int | None = None
    """The latest copyright year (the end of a range such as ``2001 - 2022``)."""


def site_years(pages: Iterable[SitePage], *, until: int | None = None) -> SiteYears:
    """The earliest copyright or founding year and the latest copyright year in the page
    bodies (:data:`_COPYRIGHT`, :data:`_FOUNDED`), ignoring years after ``until``."""
    starts: list[int] = []
    copyrights: list[int] = []
    for page in pages:
        for block in page.blocks:
            for match in _COPYRIGHT.finditer(block):
                years = [int(y) for y in (match.group("first"), match.group("last")) if y]
                years = [y for y in years if until is None or y <= until]
                copyrights.extend(years)
                starts.extend(years[:1])
            for match in _FOUNDED.finditer(block):
                year = int(match.group("first"))
                if until is None or year <= until:
                    starts.append(year)
    return SiteYears(min(starts, default=None), max(copyrights, default=None))


def _age_conflict(issuer: IssuerProfile, pages: Sequence[SitePage]) -> str | None:
    """``older_site`` when the site's earliest copyright or founding year is more than
    :data:`AGE_SLACK_YEARS` before the issuer's year of incorporation; ``stale_site`` when
    its latest copyright year is before the filing year minus :data:`STALE_SLACK_YEARS`.
    ``None`` when the pages state no such year or the filing lacks the year compared."""
    if issuer.year_of_inc is None and issuer.filed_year is None:
        return None
    until = issuer.filed_year + 1 if issuer.filed_year is not None else None
    years = site_years(pages, until=until)
    if (
        issuer.year_of_inc is not None
        and years.earliest is not None
        and years.earliest < issuer.year_of_inc - AGE_SLACK_YEARS
    ):
        return "older_site"
    if (
        issuer.filed_year is not None
        and years.latest_copyright is not None
        and years.latest_copyright < issuer.filed_year - STALE_SLACK_YEARS
    ):
        return "stale_site"
    return None


# --- the tier --------------------------------------------------------------------------------


def _mentions(pattern: re.Pattern[str] | None, page: SitePage) -> str | None:
    if pattern is None:
        return None
    for text in (page.title, page.description, page.text):
        found = pattern.search(text)
        if found is not None:
            return found.group(0)
    return None


def _tier_c(
    issuer: IssuerProfile, domain: str, pages: Sequence[SitePage], reasons: list[str]
) -> SiteEvidence:
    """Tier C when the brand (or its distinctive words) shows anywhere, else nothing."""
    for pattern in (issuer.brand_pattern(), issuer.core_pattern()):
        for page in pages:
            seen = _mentions(pattern, page)
            if seen is not None:
                return SiteEvidence(domain, "C", page.path, seen, seen, tuple(reasons))
    return SiteEvidence(domain, None, reasons=(*reasons, "no_brand"))


def assess(
    issuer: IssuerProfile, domain: str, pages: Sequence[SitePage], *, platform: bool | None = None
) -> SiteEvidence:
    """The evidence tier ``pages`` (homepage first) of ``domain`` give for ``issuer``.

    ``platform`` defaults to :func:`tell.discovery.is_platform_domain` of ``domain``.
    """
    reasons: list[str] = []
    if platform if platform is not None else is_platform_domain(domain):
        return SiteEvidence(domain, None, reasons=("platform_domain",))
    if not pages:
        return SiteEvidence(domain, None, reasons=("no_pages",))
    if is_parked(pages[0], domain):
        return SiteEvidence(domain, None, reasons=("parked",))
    addresses = site_addresses(issuer, pages)
    age = _age_conflict(issuer, pages)
    legal = _legal_match(issuer, pages)
    if legal is not None:
        found = _tier_a(issuer, domain, legal, addresses=addresses, age=age, reasons=reasons)
        if found is not None:
            return found
    elif _legal_match(issuer, pages, any_form=True) is not None:
        reasons.append("other_legal_form")
    brand = _brand_with_address(issuer, pages)
    if brand is not None:
        found = _tier_b(issuer, domain, brand, addresses=addresses, age=age, reasons=reasons)
        if found is not None:
            return found
    if legal is None and _legal_in_metadata(issuer, pages):
        reasons.append("title_only")
    return _tier_c(issuer, domain, pages, reasons)


def _tier_a(
    issuer: IssuerProfile,
    domain: str,
    legal: tuple[str, str],
    *,
    addresses: Sequence[SiteAddress],
    age: str | None,
    reasons: list[str],
) -> SiteEvidence | None:
    """Tier A for the legal-name match ``legal`` unless a disqualifier fires (added to
    ``reasons``): an address in another city and state (not checked for a registered-agent
    filing address, which says nothing about the company), or the age check."""
    if not issuer.agent_address and _address_conflict(issuer, addresses, other_city=True):
        reasons.append("different_state")
        return None
    if age is not None:
        reasons.append(age)
        return None
    path, span = legal
    return SiteEvidence(domain, "A", path, span, _strip_form(span))


def _tier_b(
    issuer: IssuerProfile,
    domain: str,
    brand: tuple[str, str],
    *,
    addresses: Sequence[SiteAddress],
    age: str | None,
    reasons: list[str],
) -> SiteEvidence | None:
    """Tier B for the brand-plus-address match ``brand`` unless a disqualifier fires: a
    registered-agent filing address, an address in another state, or the age check."""
    if issuer.agent_address:
        reason: str | None = "registered_agent_city"
    elif _address_conflict(issuer, addresses, other_city=False):
        reason = "different_state"
    else:
        reason = age
    if reason is not None:
        if reason not in reasons:
            reasons.append(reason)
        return None
    path, span = brand
    return SiteEvidence(domain, "B", path, span, span)


def _legal_in_metadata(issuer: IssuerProfile, pages: Iterable[SitePage]) -> bool:
    """The legal name appears only in a ``<title>`` or meta description (never evidence)."""
    patterns = issuer.legal_patterns()
    return any(
        pattern.search(text) is not None
        for page in pages
        for text in (page.title, page.description)
        for pattern in patterns
    )


# --- across domains --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The issuer's outcome over every domain assessed (:func:`decide`)."""

    outcome: Outcome
    evidence: SiteEvidence | None = None
    """The single qualifying domain's evidence (``linked``)."""
    domains: tuple[str, ...] = ()
    """``several_domains``: the qualifying domains; ``tier_c``: the tier C domains."""
    assessed: tuple[SiteEvidence, ...] = field(default=())

    @property
    def tier(self) -> Tier | None:
        if self.evidence is not None:
            return self.evidence.tier
        if self.outcome == "tier_c":
            return "C"
        return None


def decide(assessments: Sequence[SiteEvidence]) -> Decision:
    """``linked`` when exactly one domain qualifies (A or B), ``several_domains`` when more
    do, else ``tier_c`` when some domain is tier C, else ``no_domain``.

    Tier A beats tier B only within one domain; two domains at any of A/B are a conflict.
    """
    assessed = tuple(assessments)
    qualifying: dict[str, SiteEvidence] = {}
    for item in assessed:
        if item.qualifies and item.domain not in qualifying:
            qualifying[item.domain] = item
    if len(qualifying) == 1:
        return Decision("linked", next(iter(qualifying.values())), assessed=assessed)
    if len(qualifying) > 1:
        return Decision("several_domains", domains=tuple(qualifying), assessed=assessed)
    tier_c = tuple(dict.fromkeys(item.domain for item in assessed if item.tier == "C"))
    if tier_c:
        return Decision("tier_c", domains=tier_c, assessed=assessed)
    return Decision("no_domain", assessed=assessed)
