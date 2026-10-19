"""No launch draft or ``numbers.yaml`` regains a Form D lead-time placeholder (ADR 0041).

``docs/launch/README.md`` states the rule: a lead-time median (the days between a Form D and its
first press mention), any "days before press" figure and a "filing first" share are quoted only
from a dated report with N of at least 100 and full 60-day follow-up that marks them quotable.
Until then the speed-vs-press fields the launch copy may use are the press-share claim's three,
``cohort_n``, ``n`` and ``press_share_pct``.

The scan fails when a file under ``docs/launch/`` has

* a ``{{ ... }}`` placeholder, or a YAML key, whose name matches ``median_lead``, ``lead_days``,
  ``lead_time`` or ``filing_first``; or
* a ``{{ ... }}`` placeholder that reads a ``speed_vs_press`` field other than those three, or a
  ``reports.speed_vs_press`` key in ``numbers.yaml`` other than those three (and ``report``).

It forbids names only. It never asserts that the three allowed fields exist, and it never reads
prose ("filing first", "lead-time"), so it does not care how the press-share bullet is worded.

It is green on a tree with the rule and on one without it. The scan of the real files is armed
by the rule's own decision record (any ``docs/decisions/*lead-time*.md``, ADR 0041). Without
that record the launch files may still carry ``median_lead_days``, so that one test is a strict
``xfail``; with it, the test is ordinary and any regained placeholder fails it. The ``xfail`` is
strict on purpose: if the files are clean while the record is missing (it was renamed past the
glob, or the placeholder was dropped without it) the unexpected pass fails the run, which says to
delete the mark, so the gate cannot go stale unnoticed. The helpers are exercised on synthetic
input either way.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_DIR = REPO_ROOT / "docs" / "launch"
DECISIONS_DIR = REPO_ROOT / "docs" / "decisions"

ALLOWED_FIELDS = frozenset({"cohort_n", "n", "press_share_pct"})
"""The press-share claim: "Of {cohort_n} Form D filings tracked, {n} ({press_share_pct}%) ..."."""

LEAD_TIME_NAME = re.compile(r"median_lead|lead_days|lead_time|filing_first", re.IGNORECASE)

PLACEHOLDER = re.compile(r"\{\{((?:(?!\{\{|\}\}).)*)\}\}", re.DOTALL)
"""One ``{{ ... }}``, possibly over several lines. It cannot contain another ``{{``, so the
README's "search the draft for ``{{``" does not swallow the text up to the next placeholder."""

SPEED_VS_PRESS = re.compile(r"speed_vs_press(?:\W+(\w+))?")
"""``speed_vs_press`` and the field after it in ``a.b``, ``a['b']`` or ``a["b"]`` form."""

RULE_RECORDED = any(DECISIONS_DIR.glob("*lead-time*.md"))
"""True once the tree carries the decision record of the rule (ADR 0041)."""


def _shown(inner: str) -> str:
    return "{{ " + " ".join(inner.split()) + " }}"


def _placeholder_problems(text: str) -> Iterator[str]:
    for match in PLACEHOLDER.finditer(text):
        inner = match.group(1)
        name = LEAD_TIME_NAME.search(inner)
        if name:
            yield f"{_shown(inner)} names a lead time ({name.group(0)})"
        for study in SPEED_VS_PRESS.finditer(inner):
            field = study.group(1)
            if field not in ALLOWED_FIELDS:
                what = f"speed_vs_press.{field}" if field else "the whole speed_vs_press report"
                yield f"{_shown(inner)} reads {what}, not one of {sorted(ALLOWED_FIELDS)}"


def _key_paths(node: object, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """The path of every mapping key in a parsed YAML document."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = (*path, str(key))
            yield here
            yield from _key_paths(value, here)
    elif isinstance(node, list):
        for item in node:
            yield from _key_paths(item, path)


def _yaml_problems(text: str) -> Iterator[str]:
    for document in yaml.safe_load_all(text):
        for path in _key_paths(document):
            name = LEAD_TIME_NAME.search(path[-1])
            if name:
                yield f"key {'.'.join(path)} names a lead time ({name.group(0)})"
        reports = document.get("reports") if isinstance(document, dict) else None
        study = reports.get("speed_vs_press") if isinstance(reports, dict) else None
        if isinstance(study, dict):
            for key in study:
                if str(key) not in ALLOWED_FIELDS | {"report"}:
                    yield (
                        f"key reports.speed_vs_press.{key} is not one of {sorted(ALLOWED_FIELDS)}"
                    )


def problems(texts: Mapping[str, str]) -> list[str]:
    """Every lead-time placeholder or key in ``texts`` (file name to contents)."""
    found: list[str] = []
    for name, text in sorted(texts.items()):
        found.extend(f"{name}: {p}" for p in _placeholder_problems(text))
        if name.endswith((".yaml", ".yml")):
            found.extend(f"{name}: {p}" for p in _yaml_problems(text))
    return found


def read_launch_texts(directory: Path) -> dict[str, str]:
    """Every text file under ``directory`` by relative name; binary files are skipped."""
    texts: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            texts[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # a recorded GIF, not a draft
            continue
    return texts


PRESS_SHARE_DRAFT = """\
# LinkedIn launch post

> What it caught in its first {{ reports.ops.weeks_live }} weeks:
>
> - Of {{ reports.speed_vs_press.cohort_n }} Form D filings tracked, {{ reports.speed_vs_press.n }}
>   ({{ reports.speed_vs_press.press_share_pct }}%) had verified press within 60 days
> - {{ reports.velocity_lead.spike_share_pct }}% of {{ reports.velocity_lead.n }} funding events

No lead-time figure goes in this post: no median of the days between a filing and its press, no
"days before the press", no "filing first" share, no `median_lead_days`. Search the draft for `{{`
and `[`; the README's `{{ ... }}` and `{{ reports.speed_vs_press.n }}` are examples.
"""

PRESS_SHARE_NUMBERS = """\
# Maps every {{ placeholder }} used in docs/launch/*.md to the methods-report line it comes from.
reports:
  speed_vs_press:
    report: tell-data/site/methods/speed-vs-press-[0-9]*.md
    # No lead-time number has a placeholder here (median or p75 of the days between a filing and
    # its press, "days before press", filing-first share).
    cohort_n: {field: "Cohort N", used_in: [linkedin-post.md], status: pending soak}
    n: {field: "Filings with any press", used_in: [linkedin-post.md], status: pending soak}
    press_share_pct: {field: "Share with press", used_in: [linkedin-post.md], status: pending soak}
  velocity_lead:
    report: tell-data/site/methods/velocity-lead-[0-9]*.md
    spike_share_pct: {field: "Share with a spike", used_in: [linkedin-post.md], status: pending soak}
inputs:
  links.repo: {value: "https://github.com/karank2512/tell-ai"}
"""

# What docs/launch held before ADR 0041: the shapes the guard exists to stop coming back.
OLD_NUMBERS = """\
reports:
  speed_vs_press:
    report: tell-data/site/methods/speed-vs-press-[0-9]*.md
    n:
      field: "Filings with any press within the window (the N behind the lag numbers)"
    median_lead_days:
      field: "Median of first_press_at - filing_date, days"
"""

OLD_DRAFT = (
    "> - {{ reports.speed_vs_press.n }} funding rounds a median "
    "{{ reports.speed_vs_press.median_lead_days }} days before the first press mention\n"
)


@pytest.mark.parametrize(
    ("name", "text", "needle"),
    [
        pytest.param("linkedin-post.md", OLD_DRAFT, "median_lead", id="old-draft-bullet"),
        pytest.param("numbers.yaml", OLD_NUMBERS, "median_lead_days", id="old-numbers-key"),
        pytest.param(
            "show-hn.md", "{{ reports.speed_vs_press.lead_days_p75 }}", "lead_days", id="p75"
        ),
        pytest.param(
            "show-hn.md", "{{ reports.noise.filing_first_share_pct }}", "filing_first", id="noise"
        ),
        pytest.param("README.md", "{{ delivery.lead_time_h }}", "lead_time", id="outside-a-study"),
        pytest.param(
            "show-hn.md", "{{ reports.speed_vs_press.p75_days }}", "p75_days", id="other-field"
        ),
        pytest.param("show-hn.md", "{{ reports.speed_vs_press }}", "whole", id="whole-report"),
        pytest.param(
            "show-hn.md",
            '{{ reports["speed_vs_press"]["median_days"] }}',
            "median_days",
            id="subscript",
        ),
        pytest.param(
            "linkedin-post.md",
            "{{ reports.speed_vs_press\n     .Median_Lead_Days }}",
            "Median_Lead_Days",
            id="multi-line-and-case",
        ),
        pytest.param(
            "numbers.yaml",
            "reports:\n  speed_vs_press:\n    p75_days: {status: pending soak}\n",
            "speed_vs_press.p75_days",
            id="yaml-other-field",
        ),
        pytest.param(
            "numbers.yaml",
            "reports:\n  ops:\n    lead_time_h: {status: pending soak}\n",
            "reports.ops.lead_time_h",
            id="yaml-other-study",
        ),
        pytest.param(
            "numbers.yaml",
            "inputs:\n  reports.speed_vs_press.filing_first_pct: {value: x}\n",
            "filing_first",
            id="yaml-inputs-key",
        ),
        pytest.param(
            "numbers.yaml",
            "# fill {{ reports.speed_vs_press.median_lead_days }} by hand\nreports: {}\n",
            "median_lead",
            id="yaml-comment",
        ),
    ],
)
def test_scan_flags_a_lead_time_placeholder_or_key(name: str, text: str, needle: str) -> None:
    found = problems({name: text})
    assert found, f"{name} was not flagged"
    assert needle in "\n".join(found)


def test_scan_accepts_the_press_share_claim_and_prose() -> None:
    texts = {"linkedin-post.md": PRESS_SHARE_DRAFT, "numbers.yaml": PRESS_SHARE_NUMBERS}
    assert problems(texts) == []


def test_scan_does_not_need_the_allowed_fields_to_exist() -> None:
    only_n = "reports:\n  speed_vs_press:\n    report: x\n    n: {status: pending soak}\n"
    assert problems({"numbers.yaml": only_n, "show-hn.md": "{{ reports.speed_vs_press.n }}"}) == []


def test_read_launch_texts_skips_binary_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "draft.md").write_text("{{ links.repo }}", encoding="utf-8")
    (tmp_path / "account-page.gif").write_bytes(b"GIF89a\x89\xff\xfe{{ reports.x }}")
    assert read_launch_texts(tmp_path) == {"sub/draft.md": "{{ links.repo }}"}


def test_the_launch_directory_is_scanned() -> None:
    names = set(read_launch_texts(LAUNCH_DIR))
    assert {"README.md", "numbers.yaml", "linkedin-post.md", "show-hn.md"} <= names


@pytest.mark.xfail(
    not RULE_RECORDED,
    reason="no lead-time ADR in docs/decisions yet (ADR 0041, PR #13): numbers.yaml and "
    "linkedin-post.md still carry median_lead_days",
    raises=AssertionError,
    strict=True,
)
def test_no_file_under_docs_launch_has_a_lead_time_placeholder() -> None:
    found = problems(read_launch_texts(LAUNCH_DIR))
    assert not found, (
        "docs/launch has a lead-time placeholder again (docs/launch/README.md, ADR 0041):\n  "
        + "\n  ".join(found)
    )
