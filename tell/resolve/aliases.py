"""The alias rule (ADR 0033): which normalized strings may stand for a company.

An alias is a normalized name (:func:`tell.resolve.normalize.normalize_name`) that the
matcher treats as a name of one company. Production showed what a weak alias costs: the
domain label of ``of.one`` made ``of`` an alias of OfOne, and every Form D fund named
"X Fund I, a series of Y LLC" scored 100 against it (``token_set_ratio`` scores a token
superset 100), so 28 of 69 review items in one day were OfOne. ``ai``, ``ad``, ``l`` and
``the`` did the rest. The rule:

* **Every alias** (company names, domain labels, board slugs, filing names, review
  names) must have at least :data:`MIN_ALIAS_LENGTH` letters or digits, must not be all
  digits, and must not consist only of filler tokens: :data:`GENERIC_ALIAS_TOKENS`, a
  number, or a single character ("of", "the fund", "capital partners ii").
* **A domain label** must also pass :func:`label_alias_problem`: when it has 4 letters or
  fewer it must not be a common English word (:data:`SHORT_ENGLISH_WORDS`; ``ramp.com`` is
  the company Ramp only when its *name* says so, and the name is an alias of its own), and
  the domain must not be a platform's (``apple.com`` for a company whose directory
  website is an App Store page, :func:`tell.discovery.is_platform_domain`).

:func:`alias_problem` returns the reason an alias fails (``too_short``, ``numeric``,
``generic``), :func:`label_alias_problem` adds ``short_word`` and ``platform_domain``;
both return ``None`` for a good alias. The rule is applied where aliases are written
(:func:`tell.resolve.match.add_aliases`, :func:`tell.resolve.match.backfill_aliases`),
where they are read (:class:`tell.resolve.match.AliasIndex` ignores failing aliases), and
by ``tell resolve prune-aliases`` for rows written before it existed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rapidfuzz.distance import Levenshtein

from tell.discovery import is_platform_domain

__all__ = [
    "DESCRIPTOR_TOKENS",
    "FUZZY_TOKEN_LENGTH",
    "GENERIC_ALIAS_TOKENS",
    "MIN_ALIAS_LENGTH",
    "SHORT_ENGLISH_WORDS",
    "SHORT_LABEL_LENGTH",
    "TOKEN_TYPO_EDITS",
    "alias_problem",
    "distinctive_tokens",
    "is_single_token",
    "label_alias_problem",
    "shares_distinctive_token",
]

MIN_ALIAS_LENGTH = 3
"""Letters and digits (spaces and hyphens not counted) an alias needs."""

SHORT_LABEL_LENGTH = 4
"""Domain labels this short are checked against :data:`SHORT_ENGLISH_WORDS`."""

GENERIC_ALIAS_TOKENS: frozenset[str] = frozenset(
    {
        # Articles, prepositions, conjunctions (English plus the Romance / Germanic ones
        # that start fund and company names): "a series of", "fund of funds", "la", "de".
        "a",
        "an",
        "and",
        "at",
        "by",
        "de",
        "del",
        "des",
        "du",
        "el",
        "et",
        "for",
        "in",
        "la",
        "le",
        "of",
        "on",
        "or",
        "the",
        "to",
        "und",
        "with",
        # Corporate filler: legal forms and words that name a structure, not a company.
        "co",
        "company",
        "corp",
        "inc",
        "llc",
        "llp",
        "lp",
        "ltd",
        "group",
        "holding",
        "holdings",
        "hq",
        "global",
        "international",
        "us",
        "usa",
        # Startup filler: "Acme AI", "Acme App", "acme.io", "Acme Labs".
        "ai",
        "app",
        "apps",
        "io",
        "lab",
        "labs",
        "tech",
        "technologies",
        "technology",
        "software",
        "systems",
        "solutions",
        # Fund and SPV filler: what Form D issuers that are vehicles, not companies, share.
        "fund",
        "funds",
        "capital",
        "partners",
        "venture",
        "ventures",
        "investment",
        "investments",
        "investors",
        "management",
        "advisors",
        "equity",
        "series",
        "spv",
        "trust",
        "opportunity",
        "opportunities",
        "master",
        "feeder",
        "offshore",
        "onshore",
        "portfolio",
        "secondaries",
        "properties",
        "realty",
        # Roman numerals of fund vintages ("Fund II", "Fund XII").
        "ii",
        "iii",
        "iv",
        "vi",
        "vii",
        "viii",
        "ix",
        "xi",
        "xii",
    }
)
"""Tokens that never identify a company on their own. An alias made only of these
(plus numbers and single characters) fails the rule. Kept short on purpose: every entry
is either a function word, a legal or structural form, startup naming filler, or a word
fund vehicles share; a distinctive word belongs nowhere near this list."""

DESCRIPTOR_TOKENS: frozenset[str] = frozenset(
    {
        # What a company does, not who it is: "LSK Technologies", "CB Therapeutics",
        # "AI Insurance". Production (2026-10-14) opened review items whose only shared word
        # with the issuer was one of these: token_set_ratio scores "Nimbus Technologies"
        # against "h3x technologies" 85.71, exactly the review floor (ADR 0034).
        "technologies",
        "technology",
        "tech",
        "therapeutics",
        "insurance",
        "health",
        "healthcare",
        "bio",
        "biotech",
        "biosciences",
        "bioscience",
        "sciences",
        "science",
        "robotics",
        "energy",
        "systems",
        "solutions",
        "software",
        "labs",
        "lab",
        "studio",
        "studios",
        "network",
        "networks",
        "data",
        "analytics",
        "security",
        "payments",
        "finance",
        "financial",
        "capital",
        "medical",
        "diagnostics",
        "pharma",
        "pharmaceuticals",
        "foods",
        "food",
        "services",
        "industries",
    }
)
"""Descriptor words: a company *category* that many unrelated companies share. Unlike
:data:`GENERIC_ALIAS_TOKENS` they may stand in an alias ("Beacon Health" keeps ``health``),
but sharing one with an issuer is no evidence that the two are the same company, so they do
not count towards :func:`shares_distinctive_token` (ADR 0034)."""

TOKEN_TYPO_EDITS = 1
"""Two distinctive tokens of at least :data:`FUZZY_TOKEN_LENGTH` characters count as shared
when they are at most this many edits apart (Levenshtein): ``acmee`` / ``acme``,
``beakon`` / ``beacon``, ``methodd`` / ``method``, so a one-letter typo still reaches a
human."""

FUZZY_TOKEN_LENGTH = 4
"""Shorter tokens are shared only when equal (``lsk`` / ``lsx`` is not a typo signal)."""


_SHORT_ENGLISH_WORDS_TEXT = """
    ace act add age aid aim air all amp ant any ape arc ark arm art ask awe axe bad bag
    ban bar bat bay bed bee bet bid big bin bio bit bow box boy bud bug bun bus buy cab
    can cap car cat cog cow cub cue cup cut dab day den dew dig dim dip doc doe dog dot
    dry dub due dye ear eat ebb egg ego elf elk elm end era eve eye fab fan far fat fax
    fee few fig fin fit fix fly foe fog fox fry fun fur gap gas gem get gig gin gum gun
    gut guy gym ham hat hay hen hey hip hit hog hop hot how hub hue hug hum hut ice icy
    ill imp ink inn ion ivy jam jar jaw jet job jog joy jug key kid kin kit lad lap law
    lay leg let lid lie lip lit log lot low mad man map mat max may mix mob mop mud mug
    nap net new nil nod not now nut oak oat odd off oil old one opt orb ore our out owl
    own pad pal pan par pat paw pay pea peg pen pep per pet pie pig pin pit ply pod pop
    pot pro pub pug put rag ram rap rat raw ray red rib rid rig rim rip rob rod rot row
    rub rug rum run rye sad sap saw say sea see set sew shy sip sir sit six ski sky sly
    sod son soy spa spy sub sum sun tab tag tan tap tar tax tea ten tie tin tip toe ton
    too top tow toy try tub tug two urn use van vat vet via vow wag war wax way web wed
    wet who why wig win wit wok won yak yam yes yet you zap zen zip zoo
    able acid acme acre aero ally alto apex aqua arch area army atom aura auto avid away
    axis axle baby back bake ball band bank bare bark barn base bath bead beam bean bear
    beat beef beer bell belt bend best bike bill bind bird bite blue blur boat body bold
    bolt bond bone book boom boot boss both bowl bulk bull burn bush busy buzz cafe cage
    cake calf call calm camp card care cart case cash cast cell chat chef chip city clay
    clip club clue coal coat code coin cold comb come cook cool copy cord core corn cost
    crew crop crow cube cure curl cute dare dark dash data date dawn deal dear deck deep
    deer desk dial dice diet dine dirt disc dish dive dock doll dome done door dose dove
    down draw drip drop drum dual duck dune dusk dust duty each earn ease east easy echo
    edge edit envy epic euro even ever exam exit face fact fade fair fall fame farm fast
    fate fear feat feed feel fern file fill film find fine fire firm fish fist five flag
    flat flex flip flow foam foil fold folk fond font food fool foot fork form fort four
    free frog fuel full fuse gain gala game gate gear gift give glad glow glue goal goat
    gold golf good grab gray grid grin grip grit grow gulf guru hack hail hair half hall
    halo hand hang hard harm harp hash have hawk haze head heal heap hear heat heel helm
    help herb herd here hero hide high hike hill hint hire hive hold hole holy home hood
    hook hope horn host hour huge hull hunt idea idle inch info iron isle item jade jazz
    join joke jump jury just keen keep kelp kick kind king kite knit knob knot know lace
    lake lamb lamp land lane last late lava lawn lazy lead leaf lean leap left lend lens
    less life lift like lily limb lime line link lion list live load loaf loan lock loft
    logo lone long look loop lore loss lost loud love luck lump lure lush made mail main
    make mall many mark mask mass mast math maze meal mean meat meet melt memo menu mesh
    mild milk mill mind mine mint mist moat mode mold mole mood moon more moss most moth
    move much mule muse myth nail name navy near neat neck need nest news next nice nine
    node none noon norm nose note nova oath odds okay once only open oral orca oval oven
    over pace pack page paid pain pair pale palm pane park part pass past path peak pear
    peer pick pier pike pile pine pink pipe plan play plot plug plum plus poem poet pole
    poll polo pond pony pool poor port pose post pour pray prey prop pull pulp pump pure
    push quad quay quiz race rack raft rage raid rail rain ramp rank rare rate read real
    reap rear reed reef reel rely rent rest rice rich ride rift ring riot rise risk road
    roam roar robe rock role roll roof room root rope rose ruby rule rush rust safe sage
    sail sale salt same sand save scan seal seam seat seed seek seen self sell send ship
    shoe shop shot show sick side sift sign silk sing sink site size skip slab slam sled
    slim slip slot slow snap snow soak soap soar sock soda sofa soft soil sole solo song
    soon sort soul soup sour span spin spot spur star stay stem step stew stir stop suit
    sure surf swan swap sway swim tail take tale talk tall tank tape task taxi team tear
    tell tend tent term test text then thin tick tide tidy tier tile till tilt time tint
    tiny tire toad toll tone tool toss tour town trap tray tree trek trim trio trip true
    tube tuna tune turn twin type unit used user vale vast veil vein vent verb very vest
    veto view vine visa void volt vote wade wage wait wake walk wall wand want ward warm
    warn warp wash wasp watt wave wavy weak wear weed week well west what when whip wide
    wild will wind wine wing wink wire wise wish wolf wood wool word work worm wrap wren
    yard yarn yawn year yell yoga yolk zero zinc zone zoom
"""

SHORT_ENGLISH_WORDS: frozenset[str] = frozenset(_SHORT_ENGLISH_WORDS_TEXT.split())
"""Common English words of 3-4 letters. A domain label this short that is one of them
(``ramp``, ``flow``, ``mesh``) is a word before it is a company, so it does not become an
alias from the domain alone; the company's name still does. Longer labels and short
labels that are not words (``brex``, ``deel``) are kept."""

_TOKEN_SPLIT = re.compile(r"[\s-]+")


def _tokens(alias: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(alias) if t]


def is_single_token(alias: str) -> bool:
    """One whitespace-delimited token (what ``token_set_ratio`` sees as one word)."""
    return " " not in alias.strip()


def _filler(token: str) -> bool:
    return token in GENERIC_ALIAS_TOKENS or token.isdigit() or len(token) == 1


def alias_problem(alias: str) -> str | None:
    """Why the normalized ``alias`` may not stand for a company, or ``None`` when it may.

    ``too_short``: fewer than :data:`MIN_ALIAS_LENGTH` letters and digits. ``numeric``:
    digits only. ``generic``: every token is filler (:data:`GENERIC_ALIAS_TOKENS`, a
    number, or a single character).
    """
    tokens = _tokens(alias)
    compact = "".join(tokens)
    if len(compact) < MIN_ALIAS_LENGTH:
        return "too_short"
    if compact.isdigit():
        return "numeric"
    if all(_filler(token) for token in tokens):
        return "generic"
    return None


def label_alias_problem(label: str, domain: str) -> str | None:
    """Why the domain label ``label`` of ``domain`` may not become an alias, or ``None``.

    The :func:`alias_problem` reasons, plus ``short_word`` (a common English word of at most
    :data:`SHORT_LABEL_LENGTH` letters) and ``platform_domain`` (the domain hosts other
    people's pages, so its label names the platform, not the company).
    """
    problem = alias_problem(label)
    if problem is not None:
        return problem
    if len(label) <= SHORT_LABEL_LENGTH and label in SHORT_ENGLISH_WORDS:
        return "short_word"
    if is_platform_domain(domain):
        return "platform_domain"
    return None


def distinctive_tokens(alias: str) -> set[str]:
    """The tokens of a normalized name that can identify a company.

    Not filler (:data:`GENERIC_ALIAS_TOKENS`, a number, a single character) and not a
    descriptor (:data:`DESCRIPTOR_TOKENS`): ``"nimbus technologies"`` -> ``{"nimbus"}``,
    ``"ai insurance"`` -> ``set()``.
    """
    return {t for t in _tokens(alias) if not _filler(t) and t not in DESCRIPTOR_TOKENS}


def _same_token(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < FUZZY_TOKEN_LENGTH:
        return False
    return Levenshtein.distance(a, b, score_cutoff=TOKEN_TYPO_EDITS) <= TOKEN_TYPO_EDITS


def shares_distinctive_token(names: Iterable[str], aliases: Iterable[str]) -> bool:
    """Whether any of ``names`` and any of ``aliases`` (normalized forms) share a distinctive token.

    Shared means equal, or for tokens of at least :data:`FUZZY_TOKEN_LENGTH` characters at
    most :data:`TOKEN_TYPO_EDITS` edit apart. An R5 review candidate must
    pass this (ADR 0034): "Nimbus Technologies" shares only a descriptor with
    ``h3x technologies`` and is no candidate; "Methodd Inc" still is one for ``method``.
    """
    left: set[str] = set()
    for name in names:
        left |= distinctive_tokens(name)
    right: set[str] = set()
    for alias in aliases:
        right |= distinctive_tokens(alias)
    return any(_same_token(a, b) for a in left for b in right)
