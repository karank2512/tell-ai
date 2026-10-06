"""The news title classifier against a labelled headline set (PLAN Phase 4 gate).

Headlines are realistic but invented: fictional companies and role words only, never
a person's name. Every row must classify exactly as labelled (category and rule), and
negatives must stay unclassified.

The leadership redaction tests (ADR 0016) need a name to redact: they use obvious
placeholder names ("Jane Doe" style) inside these test strings only, never in fixtures.
"""

from __future__ import annotations

import re

import pytest

from tell.collectors.news_classify import (
    CATEGORIES,
    PERSON,
    RULES,
    Category,
    classify,
    event_type,
    person_spans,
    redact_leadership,
)

# (title, expected category, expected rule name)
LABELLED: list[tuple[str, Category | None, str | None]] = [
    # --- leadership ----------------------------------------------------------------
    ("Brightloom appoints new chief revenue officer", "leadership", "leadership.appoints"),
    ("Corvana Health Appoints Chief Medical Officer", "leadership", "leadership.appoints"),
    (
        "Fernhill Robotics appoints former cloud executive to lead go-to-market",
        "leadership",
        "leadership.appoints",
    ),
    (
        "Tidewater Labs names veteran operator as chief operating officer",
        "leadership",
        "leadership.names_as",
    ),
    (
        "Quillstone names former payments executive as its new chief financial officer",
        "leadership",
        "leadership.names_as",
    ),
    ("Northgate names industry veteran as VP of Sales", "leadership", "leadership.names_as"),
    (
        "Halcyon Data names longtime board member as president",
        "leadership",
        "leadership.names_as",
    ),
    (
        "Former cloud executive joins Kestrel AI as chief technology officer",
        "leadership",
        "leadership.joins_as",
    ),
    (
        "Ex-fintech leader joins Ardent Pay as head of partnerships",
        "leadership",
        "leadership.joins_as",
    ),
    (
        "Lumen Freight hires former retail executive as chief marketing officer",
        "leadership",
        "leadership.hires_as",
    ),
    (
        "Pinecrest Software promotes head of product to chief executive",
        "leadership",
        "leadership.promotes_to",
    ),
    # Leadership outranks funding when a headline carries both.
    (
        "Orbital Grid appoints chief financial officer after raising Series B",
        "leadership",
        "leadership.appoints",
    ),
    (
        "Wren Health names former investor as chief financial officer ahead of funding push",
        "leadership",
        "leadership.names_as",
    ),
    (
        "Ex-banker joins Larkspur as head of finance after it secures funding",
        "leadership",
        "leadership.joins_as",
    ),
    # --- funding -------------------------------------------------------------------
    (
        "Quillstone Analytics raises $24M Series A to expand forecasting platform",
        "funding",
        "funding.raises",
    ),
    ("Mosaic Bio Raises $120 Million to Scale Manufacturing", "funding", "funding.raises"),
    ("Exclusive: Vantage Ops lands $40M Series B led by growth fund", "funding", "funding.series"),
    ("Cobalt Security announces Series C to expand in Europe", "funding", "funding.series"),
    ("Sprout Ledger announces $3M seed round", "funding", "funding.seed"),
    ("Glasswing secures seed funding from angel investors", "funding", "funding.seed"),
    (
        "Riverbend Energy completes funding round led by climate investors",
        "funding",
        "funding.funding_round",
    ),
    (
        "Arcadia Logistics closes oversubscribed $65M growth round",
        "funding",
        "funding.closes_round",
    ),
    (
        "Nimbus Dental secures $8 million to build clinic software",
        "funding",
        "funding.secures_dollars",
    ),
    (
        "Helio Robotics lands $15M funding to automate warehouses",
        "funding",
        "funding.dollar_amount",
    ),
    (
        "Strata Learning gets $2.5 million investment from education fund",
        "funding",
        "funding.dollar_amount",
    ),
    ("Orchard Grid secures funding to scale grid batteries", "funding", "funding.secures_funding"),
    (
        "Bramblewood secures new growth funding from climate investors",
        "funding",
        "funding.secures_funding",
    ),
    (
        "Copperleaf gets $4 million in funding from regional fund",
        "funding",
        "funding.gets_in_funding",
    ),
    ("Copperleaf Gets $4M In Funding", "funding", "funding.gets_in_funding"),
    (
        "Lattice Ops announces strategic raise from growth investors",
        "funding",
        "funding.announces",
    ),
    (
        "Bramble Foods announces new round led by food-tech fund",
        "funding",
        "funding.announces",
    ),
    (
        "Saltmarsh Bio announces $12M funding to expand trials",
        "funding",
        "funding.dollar_amount",
    ),
    ("Fieldnote receives strategic funding from logistics group", "funding", "funding.funding"),
    ("Kiteline lands new funding to grow its team", "funding", "funding.funding"),
    ("Exclusive: Tallgrass in talks for funding", "funding", "funding.funding"),
    # Funding outranks launch.
    ("Pathwise raises $10M and launches new analytics suite", "funding", "funding.raises"),
    ("Driftwood Labs launches with fresh funding", "funding", "funding.funding"),
    # --- launch --------------------------------------------------------------------
    ("Quillstone Analytics launches real-time anomaly alerts", "launch", "launch.launches"),
    ("Beacon Health introduces AI scribe for clinics", "launch", "launch.introduces"),
    ("Introducing Quillstone Forecast 2.0", "launch", "launch.introduces"),
    ("Harbor Labs unveils open-source vector database", "launch", "launch.unveils"),
    ("Trellis debuts mobile app for field technicians", "launch", "launch.debuts"),
    ("Sundial releases version 3 of its scheduling API", "launch", "launch.releases"),
    ("Meridian Pay rolls out instant payouts to all merchants", "launch", "launch.rolls_out"),
    # Launch outranks expansion.
    ("Coastline launches in Canada and opens Toronto office", "launch", "launch.launches"),
    # --- expansion -----------------------------------------------------------------
    (
        "Quillstone Analytics opens London office to serve European customers",
        "expansion",
        "expansion.opens_office",
    ),
    (
        "Redwood Systems opens second engineering office in Austin",
        "expansion",
        "expansion.opens_office",
    ),
    (
        "Polaris Freight expands to Mexico with cross-border service",
        "expansion",
        "expansion.expands_to",
    ),
    ("Wavelength expands into Southeast Asia", "expansion", "expansion.expands_to"),
    ("We are opening a new office in Austin", "expansion", "expansion.new_office"),
    ("Granite Cloud moves to new headquarters in Denver", "expansion", "expansion.new_office"),
    # --- negatives -----------------------------------------------------------------
    ("How Quillstone Analytics thinks about data quality at scale", None, None),
    ("Our engineering principles", None, None),
    ("Fernhill Robotics praises city council decision on drone corridors", None, None),
    ("Five takeaways from the analytics conference keynote", None, None),
    ("Cobalt Security reports third-quarter results", None, None),
    ("Tidewater Labs names its new product line", None, None),
    ("Opinion: the seed stage is changing", None, None),
    ("Kestrel AI CEO on why launching is the easy part", None, None),
    ("Customer story: scaling support with Beacon Health", None, None),
    ("Arcadia Logistics wins regional innovation award", None, None),
    ("Cobalt Security announces partnership with regional bank", None, None),
]


def test_labelled_set_is_large_enough_and_covers_every_category() -> None:
    assert len(LABELLED) >= 40
    seen = {category for _, category, _ in LABELLED}
    assert seen == {*CATEGORIES, None}
    assert sum(1 for _, category, _ in LABELLED if category is None) >= 8


@pytest.mark.parametrize(("title", "category", "rule"), LABELLED, ids=[t for t, _, _ in LABELLED])
def test_labelled_titles(title: str, category: Category | None, rule: str | None) -> None:
    assert classify(title) == (category, rule)


def test_labelled_set_has_no_personal_names_or_addresses() -> None:
    for title, _, _ in LABELLED:
        assert "@" not in title
        assert not re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", title)


def test_whitespace_is_collapsed_before_matching() -> None:
    assert classify("Acme  opens\n  Berlin   office") == ("expansion", "expansion.opens_office")
    assert classify("Acme raises\tSeries A") == ("funding", "funding.raises")


def test_word_boundaries() -> None:
    assert classify("Acme praises partners") == (None, None)
    assert classify("Acme series f update") == (None, None)
    assert classify("Acme Series A") == ("funding", "funding.series")


def test_rule_names_are_unique_and_prefixed_by_category() -> None:
    names = [name for _, name, _ in RULES]
    assert len(names) == len(set(names))
    for category, name, _ in RULES:
        assert name.startswith(f"{category}.")
    order = [category for category, _, _ in RULES]
    assert order == sorted(order, key=CATEGORIES.index)


def test_bare_funding_keyword_is_a_whole_word() -> None:
    assert classify("Acme funding update") == ("funding", "funding.funding")
    assert classify("Acme is crowdfunding its next model") == (None, None)
    assert classify("Acme refunding customers after outage") == (None, None)


def test_event_type() -> None:
    assert [event_type(c) for c in CATEGORIES] == [
        "news_leadership",
        "news_funding",
        "news_launch",
        "news_expansion",
    ]


# --- leadership redaction (ADR 0016, provisional) -------------------------------------
# Placeholder names only, and only in these test strings.

REDACTED: list[tuple[str, str]] = [
    # Span after the verb, up to " as ", " to ", " its ", " the " or the end.
    ("Acme appoints Jane Doe as chief revenue officer", "Acme appoints [person] as chief revenue officer"),
    ("Acme Appoints Jane Doe As Chief Revenue Officer", "Acme Appoints [person] As Chief Revenue Officer"),
    ("Acme names John Q. Roe its new CFO", "Acme names [person] its new CFO"),
    ("Acme names Ada Example the new head of sales", "Acme names [person] the new head of sales"),
    ("Acme hires Max Placeholder to lead sales", "Acme hires [person] to lead sales"),
    ("Acme promotes Jane Doe to president", "Acme promotes [person] to president"),
    ("Acme taps Jane Doe as CFO", "Acme taps [person] as CFO"),
    ("Acme welcomes Jane Doe to its board", "Acme welcomes [person] to its board"),
    ("Acme appoints Jane Doe", "Acme appoints [person]"),
    ("Acme names Dr. Jane Doe Chief Medical Officer", "Acme names [person] Chief Medical Officer"),
    ("Acme appoints Jane Doe-Roe: what it means", "Acme appoints [person]: what it means"),
    # "X joins Acme as CFO": the run before " joins".
    ("Jane Doe joins Acme as CFO", "[person] joins Acme as CFO"),
    ("Former Google exec Jane Doe joins Acme as CFO", "Former Google exec [person] joins Acme as CFO"),
    # Two people.
    ("Acme appoints Jane Doe and John Roe as co-chief executives", "Acme appoints [person] and [person] as co-chief executives"),
    ("Acme appoints Jane Doe as CFO, John Roe as COO", "Acme appoints [person] as CFO, [person] as COO"),
    ("Acme promotes Jane Doe to president, succeeding John Roe", "Acme promotes [person] to president, succeeding [person]"),
    # No delimited span: only the role phrase is kept.
    ("Brightloom appoints new chief revenue officer", "appoints new chief revenue officer"),
    ("Corvana Health Appoints Chief Medical Officer", "Appoints Chief Medical Officer"),
    ("Quillstone names former payments executive as chief financial officer", "names as chief financial officer"),
    ("Ex-fintech leader joins Ardent Pay as head of partnerships", "joins as head of partnerships"),
    ("Acme appoints Jane Doe, former Stripe executive, as CFO", "appoints as CFO"),
    ("Acme names VP of Engineering Jane Doe as CTO", "names VP of Engineering"),
    ("Acme taps former rival exec Jane Doe as CFO", "taps as CFO"),
    ("Acme appoints Jane Doe Ann Roe Max Poe Lee as CFO", "appoints as CFO"),
    ("Fernhill Robotics appoints former cloud executive to lead go-to-market", "appoints [person]"),
    ("Leadership update from the Acme team", PERSON),
]  # fmt: skip

PLACEHOLDER_NAMES = (
    "Jane",
    "Doe",
    "John",
    "Roe",
    "Ada",
    "Example",
    "Max",
    "Placeholder",
    "Ann",
    "Poe",
    "Lee",
)


@pytest.mark.parametrize(("title", "expected"), REDACTED, ids=[t for t, _ in REDACTED])
def test_redact_leadership(title: str, expected: str) -> None:
    assert redact_leadership(title) == expected


@pytest.mark.parametrize(("title", "expected"), REDACTED, ids=[t for t, _ in REDACTED])
def test_redacted_title_holds_no_capitalised_name_run(title: str, expected: str) -> None:
    redacted = redact_leadership(title)
    tokens = title.split()
    for start, end in person_spans(title):
        run = " ".join(tokens[start:end])
        assert run not in redacted
    words = set(re.findall(r"[\w'-]+", redacted))
    assert not words & set(PLACEHOLDER_NAMES), redacted


def test_person_spans_shapes() -> None:
    assert person_spans("Acme appoints Jane Doe as CFO") == [(2, 4)]
    assert person_spans("Jane Doe joins Acme as CFO") == [(0, 2)]
    assert person_spans("Acme hires Jane Doe away from rival") == []  # not delimited
    assert person_spans("Acme names Jane Doe, ex-rival CFO, as CFO") == []  # apposition
    assert person_spans("Acme appoints new chief revenue officer") == []
    assert person_spans("Acme Appoints Chief Medical Officer") == []


def test_redaction_collapses_whitespace_and_is_deterministic() -> None:
    title = "Acme  appoints\n Jane   Doe as CFO"
    assert redact_leadership(title) == "Acme appoints [person] as CFO"
    assert redact_leadership(title) == redact_leadership(title)


@pytest.mark.parametrize(
    "title",
    [t for t, c, _ in LABELLED if c == "leadership"],
)
def test_labelled_leadership_titles_are_never_stored_raw(title: str) -> None:
    redacted = redact_leadership(title)
    assert redacted
    assert redacted != title
