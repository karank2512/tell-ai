"""Site evidence for issuer-first discovery (ADR 0035): does this domain belong to this CIK?

Issuer-first discovery (:mod:`tell.discovery.issuers`) guesses domains for a Form D issuer
and fetches a few pages of each. The filing-to-company link is exact by CIK, so precision
rests on one question, answered here from the fetched page text and the issuer's own
filing facts. Everything in this module is pure: no network, no database.

Tiers, per domain (:func:`assess`):

* **Tier A**: a page body states the issuer's **full legal name**: every token of the
  name, descriptors included, in order (a name from ``issuerPreviousNameList`` counts),
  followed by a legal form (``Inc`` / ``Incorporated`` / ``Corp`` / ``Corporation`` /
  ``PBC`` / ``Co.``) that ends on a word boundary, so "Payload, including" is not
  "Payload, Inc". The name may not be preceded by a letter, a digit, ``.``, ``@`` or ``/``
  (``communion.co for sale``, an e-mail address, a URL path).
* **Tier B**: one page body states the **full brand** (the normalized issuer name, every
  token including descriptors: "Restara Medical", not "Restara") **and** an address in the
  Form D city (``City, ST`` / ``City, State``) or, failing the city, the Form D state with a
  ZIP code (``ST 12345``). Never when the Form D address is a registered-agent city
  (:data:`REGISTERED_AGENT_CITIES`): the agent's address says nothing about the company.
* **Tier C**: weaker: the distinctive brand words (the brand minus filler and descriptor
  tokens) appear in the title, the description or the body, or tier A/B text was found but
  a disqualifier fired. Tier C is never evidence: it opens a review item at most.

Always required for A/B: the site states no US address (ZIP-coded, or a street line plus
``City, ST``) in a different state; the domain is not a platform's
(:func:`tell.discovery.is_platform_domain`) and the homepage is not a parked or for-sale
page. Never evidence on their own: a matching ``<title>`` or meta
description (title-only), an ATS board name (the board only proposes a domain), and a
shared core word plus the same city (mangrove.ai is a trading app in Sacramento; the
issuer is Mangrove Therapeutics, also in Sacramento: the full brand is absent, so it is
tier C at most).

Page text is kept as blocks (:func:`parse_page`), one per block-level element, joined
with :data:`BLOCK_SEPARATOR`, and no name or address pattern crosses a separator. Run
together, "Move Fast" and "Way Biosolutions, Inc." read "FastWay Biosolutions, Inc." and
the match fails; that happened in the design's second pass.

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
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html

from tell.discovery import is_platform_domain, registrable_domain
from tell.resolve.aliases import DESCRIPTOR_TOKENS, GENERIC_ALIAS_TOKENS
from tell.resolve.normalize import normalize_name

__all__ = [
    "BLOCK_SEPARATOR",
    "LEGAL_FORMS",
    "LEGAL_PATHS",
    "MAX_PARSE_BYTES",
    "PARKED_TEXT_LIMIT",
    "REGISTERED_AGENT_CITIES",
    "US_STATES",
    "Decision",
    "IssuerProfile",
    "SiteEvidence",
    "SitePage",
    "Tier",
    "assess",
    "brand_words",
    "core_words",
    "decide",
    "is_parked",
    "legal_base",
    "legal_links",
    "parse_page",
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

LEGAL_FORMS: tuple[str, ...] = ("inc", "incorporated", "corp", "corporation", "pbc", "co")
"""Legal forms that end a tier A legal name (``co`` only written ``Co.``)."""

_STRIPPED_FORMS: frozenset[str] = frozenset(
    {*LEGAL_FORMS, "company", "llc", "ltd", "limited", "plc", "lp", "llp"}
)
"""Trailing tokens cut from the issuer's name to get its legal base (never the last one)."""

_SUFFIX = r"(?:inc\b\.?|incorporated\b|corp\b\.?|corporation\b|pbc\b|co\.(?!\w))"
"""A legal form after the name; ``\\b`` after the form keeps "including" out."""

_NAME_GAP = r"[ \-.'\u2019&+]{0,3}"
"""Between two name tokens: nothing (``WayBio``) or up to three spaces/punctuation.
Never a :data:`BLOCK_SEPARATOR`."""

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
_ZIP_ADDRESS = re.compile(
    rf"(?<![A-Za-z])(?P<code>{_STATE_CODES}),? {_ZIP}"
    rf"|(?<![\w])(?P<name>(?i:{_STATE_NAMES})),? {_ZIP}"
)
"""A ZIP-coded US address: ``WI 53703`` or ``North Carolina 27606`` (codes upper case)."""

_STREET_ADDRESS = re.compile(
    r"(?<![\w.])\d{1,6}[A-Za-z]? (?:[^\W_][\w.'#-]* ){0,5}[^\W_][\w.'#-]*,"
    r"(?: (?:Suite|Ste\.?|Unit|Floor|Fl\.?|#) ?[\w-]{1,8},)?"
    rf" [A-Z][a-z]+(?: [A-Z][a-z]+){{0,2}},? (?:(?P<code>{_STATE_CODES})(?![A-Za-z])"
    rf"|(?P<name>(?i:{_STATE_NAMES}))(?!\w))"
)
"""A US street address without a ZIP code: a street line starting with a house number,
then ``City, ST`` / ``City State`` (``500 Main Street, Suite 4, Austin, TX``). The street
line is what makes it an address: a bare ``City, ST`` is often a venue or a law ("Shine
the Light, CA Civil Code"), not where the company is."""

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


def legal_base(name: str) -> list[str]:
    """The issuer name's tokens without its trailing legal forms (never the last token).

    ``"Saltbox Tinned Fish Co., PBC"`` -> ``["Saltbox", "Tinned", "Fish"]``;
    ``"Way Biosolutions, Inc."`` -> ``["Way", "Biosolutions"]``.
    """
    tokens = _TOKEN.findall(_fold(name))
    while len(tokens) > 1 and tokens[-1].casefold() in _STRIPPED_FORMS:
        tokens.pop()
    return tokens


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

    ``state`` is the Form D ``stateOrCountry`` code (``CA``); ``city`` as filed.
    """

    cik: str
    name: str
    previous_names: tuple[str, ...] = ()
    city: str | None = None
    state: str | None = None

    @property
    def state_code(self) -> str | None:
        code = (self.state or "").strip().upper()
        return code if code in US_STATES else None

    @property
    def agent_address(self) -> bool:
        """The Form D address is a registered-agent city (tier B is never accepted)."""
        city = " ".join((self.city or "").split()).casefold()
        return (city, (self.state or "").strip().upper()) in REGISTERED_AGENT_CITIES

    def legal_patterns(self) -> list[re.Pattern[str]]:
        """One tier A pattern per legal name (current, then previous).

        A match is at most the name's tokens, three characters between tokens, ``", "`` and
        the longest form (``Incorporated``), so a stored span can never carry page text
        beyond the legal name and its suffix.
        """
        out: list[re.Pattern[str]] = []
        for name in (self.name, *self.previous_names):
            base = legal_base(name)
            if base:
                pattern = _words_pattern(base) + r",? ?" + _SUFFIX
                out.append(re.compile(pattern, re.IGNORECASE))
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


def _document(content: bytes) -> Any | None:
    if not content or not content.strip():
        return None
    try:
        return lxml_html.document_fromstring(content[:MAX_PARSE_BYTES])
    except (etree.ParserError, ValueError):
        return None


def _squash(text: str) -> str:
    return " ".join(text.split())


def parse_page(content: bytes, path: str) -> SitePage:
    """Title, meta description and the visible text of ``content`` as blocks.

    Scripts, styles, templates, SVG and comments are dropped; every block-level element
    (:data:`_BLOCK_TAGS`) starts and ends a block; whitespace inside a block collapses to one
    space; empty blocks are dropped.
    """
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
    """Why the domain is not tier A/B (``parked``, ``platform_domain``, ``different_state``,
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


def _legal_match(issuer: IssuerProfile, pages: Sequence[SitePage]) -> tuple[str, str] | None:
    patterns = issuer.legal_patterns()
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


def _other_state(issuer: IssuerProfile, pages: Iterable[SitePage]) -> bool:
    """Some page states a US address in a state other than the Form D state: ZIP-coded
    (:data:`_ZIP_ADDRESS`) or a street line plus ``City, ST`` (:data:`_STREET_ADDRESS`).

    Not detected: an address abroad (no reliable pattern; a tier A name still needs the
    issuer's full legal name with a US legal form on a domain derived from that name).
    """
    own = issuer.state_code
    for page in pages:
        text = page.text
        for match in (*_ZIP_ADDRESS.finditer(text), *_STREET_ADDRESS.finditer(text)):
            code = match.group("code")
            if code is None:
                name = (match.group("name") or "").casefold()
                code = next((c for c, n in US_STATES.items() if n == name), None)
            if code is not None and code != own:
                return True
    return False


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
    other_state = _other_state(issuer, pages)
    legal = _legal_match(issuer, pages)
    if legal is not None:
        if not other_state:
            path, span = legal
            return SiteEvidence(domain, "A", path, span, _strip_form(span))
        reasons.append("different_state")
    brand = _brand_with_address(issuer, pages)
    if brand is not None:
        if issuer.agent_address:
            reasons.append("registered_agent_city")
        elif other_state:
            if "different_state" not in reasons:
                reasons.append("different_state")
        else:
            path, span = brand
            return SiteEvidence(domain, "B", path, span, span)
    if legal is None and _legal_in_metadata(issuer, pages):
        reasons.append("title_only")
    return _tier_c(issuer, domain, pages, reasons)


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
