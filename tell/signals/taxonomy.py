"""Role-family taxonomy and requisition fingerprints (spec section 7).

Role families
=============

A title is lower-cased, every run of characters other than ``a-z``, ``0-9`` and ``&``
becomes one space, and the result is matched against the ordered keyword lists in
:data:`FAMILY_KEYWORDS` (the spec section 7 table, verbatim, in table order).

* A keyword matches on word boundaries, with an optional plural / agent / gerund
  suffix (``s``, ``es``, ``ing``, ``er``, ``ers``): ``ae`` matches "Senior AE" and
  "AEs" but never "aerospace"; ``design`` matches "Designer"; ``software engineer``
  matches "Software Engineering Manager". Keywords of three letters or fewer (``ae``,
  ``sdr``, ``bi``, ``hr``, ...) take only the plural ``s``, and ``hr`` takes none
  ("20 hrs" is hours, not HR).
* A handful of spelling variants (:data:`KEYWORD_VARIANTS`, e.g. "front end" for
  ``frontend``, "business development representative" for ``business development
  rep``, "svp" for ``vp``) count as the spec keyword they spell.
* The first family (in table order) with a matching keyword wins, with one
  refinement: a match lying strictly inside a longer match from another family is
  ignored. Without it the ``gtm_ops`` keyword ``revenue operations`` could never win,
  because the ``sales`` keyword ``revenue`` always matches first; with it "Revenue
  Operations Manager" is ``gtm_ops`` while "Chief Revenue Officer" stays ``sales``.
* ``sales engineer`` / ``solutions engineer`` are in the ``sales`` list, which comes
  before ``engineering``, so they are ``sales`` (bought-for roles, per the spec).
* ``exec`` is last in the table, so it only wins when no other family's keyword is in
  the title ("Head of Talent" is ``finance_ops``; "VP Engineering" is ``exec``).
* The department hint (:data:`DEPARTMENT_HINTS`) is a tie-breaker only: it is consulted
  when no title keyword of any family (``exec`` included) matched. Two hints are
  shared by two families in the spec table (``marketing``: gtm_ops and marketing;
  ``data``: ml_ai and data); a department alone cannot tell the ops or ML variant from
  the core function, so the hint goes to the broader family (``marketing``, ``data``).
* Titles in a non-Latin script, or carrying one of a short list of unmistakably
  non-English role words (:data:`NON_ENGLISH_WORDS`: "développeur", "ingeniero",
  "responsable", ...; :func:`is_non_english`), are ``other`` until a translation table
  exists; so is anything that matches nothing. English titles with a German gender
  marker ("Software Engineer (m/w/d)") are English and matched normally.

``intern`` / ``contract`` postings are tagged by :func:`is_intern` and excluded from
velocity (the role family itself is unaffected).

Requisition fingerprints
========================

``fingerprint = sha1(title_norm|department_norm|location_norm|remote_flag)``:

* ``title_norm`` (:func:`normalize_title`): lower-cased, punctuation removed, seniority
  tokens (:data:`SENIORITY_TOKENS`, roman numerals ``i``-``x`` and level codes such
  as ``l5``, ``e4``, ``ic3``, bare ``2``) stripped, whitespace collapsed.
* ``department_norm`` (:func:`normalize_department`): lower-cased, punctuation removed.
* ``location_norm`` (:func:`normalize_location`): ``"city, country"``; see there.
* ``remote_flag``: ``"1"`` / ``"0"`` / ``""`` for ``True`` / ``False`` / ``None``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

__all__ = [
    "CA_PROVINCES",
    "COUNTRY_ALIASES",
    "DEPARTMENT_HINTS",
    "FAMILIES",
    "FAMILY_KEYWORDS",
    "INTERN_TOKENS",
    "KEYWORD_VARIANTS",
    "NON_ENGLISH_WORDS",
    "OTHER",
    "SENIORITY_TOKENS",
    "TARGET_FAMILIES_DEFAULT",
    "US_STATES",
    "fingerprint",
    "is_intern",
    "is_non_english",
    "normalize_department",
    "normalize_location",
    "normalize_title",
    "role_family",
]

OTHER = "other"
"""The fall-through family: no keyword and no department hint matched, or non-English."""

FAMILY_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "sales": (
        "account executive",
        "ae",
        "sdr",
        "bdr",
        "sales development",
        "business development rep",
        "account manager",
        "enterprise sales",
        "sales engineer",
        "solutions engineer",
        "revenue",
    ),
    "gtm_ops": (
        "revops",
        "revenue operations",
        "sales operations",
        "gtm engineer",
        "growth engineer",
        "marketing operations",
        "demand gen",
        "lifecycle",
    ),
    "marketing": ("marketing", "content", "brand", "product marketing", "seo", "events"),
    "customer": (
        "customer success",
        "csm",
        "support",
        "implementation",
        "onboarding",
        "solutions architect",
        "forward deployed",
    ),
    "engineering": (
        "software engineer",
        "backend",
        "frontend",
        "full stack",
        "platform",
        "infrastructure",
        "devops",
        "sre",
        "security engineer",
        "mobile",
    ),
    "ml_ai": (
        "machine learning",
        "ml engineer",
        "ai engineer",
        "research scientist",
        "applied scientist",
        "data scientist",
        "nlp",
        "llm",
    ),
    "data": ("data engineer", "analytics engineer", "data analyst", "bi"),
    "product_design": ("product manager", "product designer", "ux", "design"),
    "finance_ops": (
        "finance",
        "accounting",
        "controller",
        "people",
        "recruiter",
        "talent",
        "hr",
        "legal",
        "operations manager",
        "chief of staff",
    ),
    "exec": ("chief", "vp", "vice president", "head of", "director"),
}
"""Spec section 7, verbatim and in table order: the first family that matches wins."""

FAMILIES: tuple[str, ...] = tuple(FAMILY_KEYWORDS)
"""The ten role families in matching order (``other`` is not one of them)."""

TARGET_FAMILIES_DEFAULT: tuple[str, ...] = ("sales", "gtm_ops", "customer")
"""The ``default`` ICP profile's target families (weight 1.0 on ``hiring_velocity``)."""

KEYWORD_VARIANTS: Mapping[str, str] = {
    "business development representative": "business development rep",
    "rev ops": "revops",
    "sales ops": "sales operations",
    "marketing ops": "marketing operations",
    "demand generation": "demand gen",
    "front end": "frontend",
    "back end": "backend",
    "fullstack": "full stack",
    "site reliability": "sre",
    "svp": "vp",
    "evp": "vp",
    "avp": "vp",
}
"""Spellings that count as the spec keyword they spell (variant -> spec keyword)."""

DEPARTMENT_HINTS: tuple[tuple[str, str], ...] = (
    ("sales", "sales"),
    ("marketing", "marketing"),
    ("customer", "customer"),
    ("support", "customer"),
    ("engineering", "engineering"),
    ("data", "data"),
    ("research", "ml_ai"),
    ("product", "product_design"),
    ("operations", "gtm_ops"),
    ("g&a", "finance_ops"),
)
"""``(department word, family)`` in priority order; used only when no title keyword matched.

The spec table's hints, with the two shared hints (``marketing``, ``data``) given to
the broader family. A hint matches the department on word boundaries (plural allowed).
"""

SENIORITY_TOKENS: frozenset[str] = frozenset(
    {"senior", "sr", "staff", "principal", "lead"}
    | {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
)
"""Whole tokens :func:`normalize_title` drops (``sr.`` loses its dot first)."""

_LEVEL_TOKEN = re.compile(r"(?:l|e|ic)\d{1,2}|\d{1,2}")
"""Level codes dropped from titles: ``l5``, ``e4``, ``ic3`` and bare ``2`` / ``10``."""

INTERN_TOKENS: frozenset[str] = frozenset(
    {"intern", "interns", "internship", "internships", "contract", "contractor", "contractors"}
)
"""Tokens that tag a posting ``is_intern`` (title or employment commitment)."""

_CONTRACT_ROLE_NOUNS: frozenset[str] = frozenset(
    {
        "manager",
        "management",
        "administrator",
        "administration",
        "specialist",
        "analyst",
        "attorney",
        "counsel",
        "lawyer",
        "negotiator",
        "coordinator",
    }
)
"""In a *title*, ``contract`` followed by one of these names a legal role, not a contract job."""

US_STATES: frozenset[str] = frozenset(
    [
        "al",
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "dc",
        "fl",
        "ga",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "ms",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sc",
        "sd",
        "tn",
        "tx",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
        "wy",
    ]
)
"""Two-letter US state codes (plus DC): ``"City, ST"`` means the United States."""

CA_PROVINCES: frozenset[str] = frozenset(
    ["ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"]
)
"""Two-letter Canadian province codes: ``"City, ON"`` means Canada."""

COUNTRY_ALIASES: Mapping[str, str] = {
    "us": "us",
    "usa": "us",
    "u s": "us",
    "u s a": "us",
    "united states": "us",
    "united states of america": "us",
    "america": "us",
    "uk": "gb",
    "u k": "gb",
    "gb": "gb",
    "great britain": "gb",
    "united kingdom": "gb",
    "england": "gb",
    "scotland": "gb",
    "wales": "gb",
    "canada": "ca",
    "germany": "de",
    "deutschland": "de",
    "france": "fr",
    "india": "in",
    "ireland": "ie",
    "netherlands": "nl",
    "the netherlands": "nl",
    "spain": "es",
    "portugal": "pt",
    "italy": "it",
    "poland": "pl",
    "sweden": "se",
    "switzerland": "ch",
    "australia": "au",
    "singapore": "sg",
    "japan": "jp",
    "brazil": "br",
    "mexico": "mx",
    "israel": "il",
}
"""Country spellings folded to one code. Unknown countries are kept as written (lower-cased)."""

NON_ENGLISH_WORDS: frozenset[str] = frozenset(
    {
        # French
        "développeur",
        "developpeur",
        "développeuse",
        "ingénieur",
        "ingénieure",
        "responsable",
        "chargé",
        "chargée",
        "stagiaire",
        "alternance",
        "alternant",
        "comptable",
        "juriste",
        "commerciale",
        # Spanish / Portuguese
        "ingeniero",
        "ingeniera",
        "desarrollador",
        "desarrolladora",
        "desenvolvedor",
        "desenvolvedora",
        "engenheiro",
        "engenheira",
        "gerente",
        "analista",
        "vendedor",
        "responsável",
        "estagiário",
        "estagiária",
        # German
        "entwickler",
        "entwicklerin",
        "ingenieur",
        "ingenieurin",
        "leiter",
        "leiterin",
        "mitarbeiter",
        "mitarbeiterin",
        "vertrieb",
        "vertriebsmitarbeiter",
        "kundenbetreuer",
        "praktikant",
        "praktikantin",
        "praktikum",
        "werkstudent",
        "werkstudentin",
        "sachbearbeiter",
        "buchhalter",
        # Italian
        "sviluppatore",
        "sviluppatrice",
        "ingegnere",
        "responsabile",
        "addetto",
        "addetta",
        "impiegato",
    }
)
"""Latin-script words that mark a title as not English (ADR 0020).

A handful of role nouns and function words from French, Spanish, Portuguese, German and
Italian job titles; any one of them as a whole word sends the title to ``other``, as
the spec asks for non-English titles. Deliberately short: a word is listed only when
it is not also an English word ("commercial", "director", "manager" are not listed).
"""

_REMOTE_WORDS = frozenset({"remote", "anywhere", "distributed"})
_WORK_MODE_WORDS = re.compile(r"\b(?:hybrid|on ?site|in office|in person|office)\b")
_NON_TOKEN = re.compile(r"[^a-z0-9&]+")
_WHITESPACE = re.compile(r"\s+")
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SEGMENT_SPLIT = re.compile(r";|\||(?<![,\s])\s+or\s+")
"""Several locations in one string. " or " splits only when the word before it is not
followed by a comma, so "Portland, OR 97201" and "Eugene, OR - Hybrid" keep their state."""
_SUFFIX = r"(?:s|es|ing|er|ers)?"
_SHORT_SUFFIX = r"s?"
_SHORT_KEYWORD = 3
"""Keywords this short take only the plural suffix (``ae`` -> "AEs", never "aeing")."""
_NO_SUFFIX_KEYWORDS: frozenset[str] = frozenset({"hr"})
"""``hr`` + ``s`` is "hrs" (hours): "Barista (20 hrs)" is not an HR role."""
_WORD_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _render(text: str) -> str:
    """Lower-case; every run of characters outside ``[a-z0-9&]`` becomes one space."""
    return _NON_TOKEN.sub(" ", text.lower()).strip()


def _keyword_pattern(phrase: str) -> re.Pattern[str]:
    words = r"\s".join(re.escape(word) for word in phrase.split())
    if phrase in _NO_SUFFIX_KEYWORDS:
        suffix = ""
    elif len(phrase) <= _SHORT_KEYWORD:
        suffix = _SHORT_SUFFIX
    else:
        suffix = _SUFFIX
    return re.compile(rf"(?<![a-z0-9]){words}{suffix}(?![a-z0-9])")


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    [
        (family, _keyword_pattern(keyword))
        for family, keywords in FAMILY_KEYWORDS.items()
        for keyword in keywords
    ]
    + [
        (family, _keyword_pattern(variant))
        for variant, keyword in KEYWORD_VARIANTS.items()
        for family, keywords in FAMILY_KEYWORDS.items()
        if keyword in keywords
    ]
)
_HINT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![a-z0-9]){re.escape(word)}s?(?![a-z0-9])"), family)
    for word, family in DEPARTMENT_HINTS
)
_FAMILY_ORDER: Mapping[str, int] = {family: index for index, family in enumerate(FAMILIES)}


# --- titles -------------------------------------------------------------------------------


def is_non_english(title: str) -> bool:
    """True when ``title`` has no ASCII letters, mostly non-Latin letters, or a listed word.

    "Mostly" means fewer than half of the letters are ASCII (Cyrillic, CJK, Arabic,
    Hebrew and so on). Latin-script titles are flagged only when a whole word is in
    :data:`NON_ENGLISH_WORDS` ("Développeur Backend", "Ingeniero de Software"); an
    accented word alone is not enough ("Zoë's Café Manager" is English).
    """
    letters = [char for char in title if char.isalpha()]
    if not letters:
        return True
    ascii_letters = sum(1 for char in letters if char.isascii())
    if ascii_letters * 2 < len(letters):
        return True
    words = {w for w in _WORD_SPLIT.split(title.casefold()) if w}
    return bool(words & NON_ENGLISH_WORDS)


def normalize_title(title: str) -> str:
    """Lower-case, strip punctuation and seniority tokens, collapse whitespace.

    Seniority tokens are :data:`SENIORITY_TOKENS` (``senior``, ``sr``/``sr.``,
    ``staff``, ``principal``, ``lead``, roman numerals ``i``-``x``) and level codes
    (``l5``, ``e4``, ``ic3``, bare one- or two-digit numbers). Idempotent.
    """
    tokens = _render(title).split()
    kept = [t for t in tokens if t not in SENIORITY_TOKENS and not _LEVEL_TOKEN.fullmatch(t)]
    return " ".join(kept)


def _title_family(rendered: str) -> str | None:
    matches: list[tuple[str, int, int]] = []
    for family, pattern in _PATTERNS:
        matches.extend((family, m.start(), m.end()) for m in pattern.finditer(rendered))
    if not matches:
        return None

    def masked(match: tuple[str, int, int]) -> bool:
        family, start, end = match
        return any(
            other != family
            and o_start <= start
            and end <= o_end
            and (o_end - o_start) > (end - start)
            for other, o_start, o_end in matches
        )

    live = [match for match in matches if not masked(match)]
    return min(live, key=lambda match: _FAMILY_ORDER[match[0]])[0]


def _department_family(department: str | None) -> str | None:
    if not department:
        return None
    rendered = _render(department)
    for pattern, family in _HINT_PATTERNS:
        if pattern.search(rendered):
            return family
    return None


def role_family(title: str, department: str | None = None) -> str:
    """The role family of a posting title (see the module docstring for the rules)."""
    if is_non_english(title):
        return OTHER
    family = _title_family(_render(title))
    if family is not None:
        return family
    return _department_family(department) or OTHER


def is_intern(title: str, commitment: str | None = None) -> bool:
    """True for intern / internship / contract / contractor postings (excluded from velocity).

    ``commitment`` is the provider's employment type when it has one (Lever
    ``commitment``, Ashby ``employmentType``, SmartRecruiters ``typeOfEmployment``);
    any :data:`INTERN_TOKENS` token in it tags the posting. In the title the same
    tokens tag it, except ``contract`` directly followed by a role noun ("Contract
    Manager", "Contracts Attorney" is not tagged anyway) which names a legal role.
    """
    if commitment:
        # "FullTime" / "PartTime" split on case so the tokens are words.
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", commitment)
        if INTERN_TOKENS & set(_render(spaced).split()):
            return True
    words = _render(title).split()
    for index, word in enumerate(words):
        if word not in INTERN_TOKENS:
            continue
        following = words[index + 1] if index + 1 < len(words) else ""
        if word == "contract" and following in _CONTRACT_ROLE_NOUNS:
            continue
        return True
    return False


# --- fingerprints ----------------------------------------------------------------------------


def normalize_department(department: str | None) -> str:
    """Lower-cased department with punctuation removed (``&`` kept); ``""`` for ``None``."""
    return _render(department) if department else ""


def _country(text: str) -> str:
    key = _render(text.replace(".", " "))
    key = _WHITESPACE.sub(" ", key)
    return COUNTRY_ALIASES.get(key, key)


def _strip_digits(text: str) -> str:
    return _WHITESPACE.sub(" ", re.sub(r"\d+", " ", text)).strip()


def _join(city: str, country: str) -> str:
    return ", ".join(part for part in (city, country) if part)


def normalize_location(location: str | None) -> str:
    """Reduce a provider location to ``"city, country"`` (either part may be missing).

    Only the first of several locations counts (split on ``;``, ``|`` and a
    space-delimited " or " that does not follow a comma, so "Portland, OR 97201" and
    "Eugene, OR - Hybrid" keep their state code). Parenthesised notes and work-mode
    words ("Hybrid", "On-site") are dropped and postal codes removed. Then:

    * any segment naming ``remote`` / ``anywhere`` / ``distributed`` -> city
      ``remote`` plus whatever country is left ("Remote - US", "US (Remote)",
      "Remote, United States" are all ``"remote, us"``);
    * ``"City, Region, ..., Country"`` -> first and last part;
    * ``"City, XX"`` -> ``us`` when ``XX`` is a US state code, ``ca`` when it is a
      Canadian province code, else ``XX`` read as a country. A two-letter country code
      that is also a state or province code ("Toronto, CA", "Berlin, DE", "Bangalore,
      IN") is read as the state: ATS boards write "City, ST" far more often than
      "City, CC", each board formats its locations consistently, and the fingerprint
      only needs to be stable per board (ADR 0020 records the collision);
    * a single part -> a country when it is a known country spelling, else a city.

    Countries fold through :data:`COUNTRY_ALIASES` ("USA", "United States" -> ``us``).
    """
    if not location:
        return ""
    lowered = location.lower()
    segment = next((s for s in _SEGMENT_SPLIT.split(lowered) if s.strip()), "")
    remote = bool(_REMOTE_WORDS & set(_render(segment).split()))
    if remote:
        rest = _render(segment)
        for word in _REMOTE_WORDS:
            rest = re.sub(rf"\b{word}\b", " ", rest)
        rest = _strip_digits(_WORK_MODE_WORDS.sub(" ", rest))
        return _join("remote", _country(rest) if rest else "")
    segment = _PAREN.sub(" ", segment)
    parts = [_strip_digits(_WORK_MODE_WORDS.sub(" ", _render(p))) for p in segment.split(",")]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        only = parts[0] if parts else ""
        return _country(only) if only in COUNTRY_ALIASES else only
    city, last = parts[0], parts[-1]
    if len(parts) == 2 and last in US_STATES:
        country = "us"
    elif len(parts) == 2 and last in CA_PROVINCES:
        country = "ca"
    else:
        country = _country(last)
    return _join(city, country)


def fingerprint(
    title_norm: str, department: str | None, location: str | None, remote: bool | None
) -> str:
    """``sha1(title_norm|department_norm|location_norm|remote_flag)`` as 40 hex chars.

    ``title_norm`` is passed through :func:`normalize_title` again (it is idempotent),
    so a caller handing in a raw title gets the same requisition identity.
    ``department`` and ``location`` are reduced by :func:`normalize_department` and
    :func:`normalize_location`; ``remote`` renders as ``"1"``, ``"0"`` or ``""``. The
    hash is a stable requisition identity, not a secret.
    """
    remote_flag = "" if remote is None else ("1" if remote else "0")
    joined = "|".join(
        (
            normalize_title(title_norm),
            normalize_department(department),
            normalize_location(location),
            remote_flag,
        )
    )
    return hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()
