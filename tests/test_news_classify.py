"""The news title classifier against a labelled headline set (PLAN Phase 4 gate).

Headlines are realistic but invented: fictional companies and role words only, never
a person's name. Every row must classify exactly as labelled (category and rule), and
negatives must stay unclassified.
"""

from __future__ import annotations

import re

import pytest

from tell.collectors.news_classify import CATEGORIES, RULES, Category, classify, event_type

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
    # Funding outranks launch.
    ("Pathwise raises $10M and launches new analytics suite", "funding", "funding.raises"),
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


def test_event_type() -> None:
    assert [event_type(c) for c in CATEGORIES] == [
        "news_leadership",
        "news_funding",
        "news_launch",
        "news_expansion",
    ]
