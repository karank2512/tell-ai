"""PII guard (spec section 12): no personal data in models, fixtures or collector code."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tell.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

FORBIDDEN_COLUMNS = {"email", "phone", "first_name", "last_name"}
FORBIDDEN_SUFFIXES = ("_email", "_phone")

# A URL ends at whitespace or at a quote / JSON delimiter, so in compact (single-line)
# JSON the token never swallows the fields that follow it. Cutting a real URL short at a
# comma only ever errs toward flagging, never toward hiding an ``@``.
URL_TOKEN = re.compile(r"""https?://[^\s"'<>,}\]]+""")

# The Form D XML element that lists officers and directors by name. Spelled out only
# here (as data, not as a stored field) so the guard can grep for it.
FORBIDDEN_FIELD = "related" + "PersonsList"


def _offending_columns() -> list[str]:
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            name = column.name.lower()
            if name in FORBIDDEN_COLUMNS or name.endswith(FORBIDDEN_SUFFIXES):
                offenders.append(f"{table.name}.{column.name}")
    return offenders


def test_no_pii_columns_in_any_model() -> None:
    assert _offending_columns() == []


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.rglob("*") if p.is_file())


def _at_signs_outside_urls(text: str) -> list[str]:
    stripped = URL_TOKEN.sub("", text)
    return [line for line in stripped.splitlines() if "@" in line]


def _json_strings(value: Any) -> Iterator[str]:
    """Every string in a parsed JSON document: keys and values, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)


def _offending_strings(path: Path) -> list[str]:
    """Strings (JSON) or lines (anything else) of a fixture holding an ``@`` outside a URL.

    JSON fixtures are parsed and every string walked individually, so a compact
    single-line file reports the exact offending value instead of the whole line. A
    ``.json`` file that does not parse falls back to the line scan.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            document = json.loads(text)
        except ValueError:
            pass
        else:
            return [s for s in _json_strings(document) if _at_signs_outside_urls(s)]
    return _at_signs_outside_urls(text)


def test_fixtures_dir_exists() -> None:
    assert FIXTURES_DIR.is_dir()


def test_no_at_sign_outside_urls_in_fixtures() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _fixture_files():
        found = _offending_strings(path)
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = [s[:120] for s in found[:3]]
    assert offenders == {}


def test_at_sign_detector_strips_urls_only() -> None:
    assert _at_signs_outside_urls("see https://example.com/@handle/x and nothing else") == []
    assert _at_signs_outside_urls('{"contact": "someone@example.com"}') == [
        '{"contact": "someone@example.com"}'
    ]
    # Compact JSON: the URL token must stop at the closing quote, not eat the line.
    compact = '{"url":"https://a.test/x","contact":"bob@example.com"}'
    assert _at_signs_outside_urls(compact) == ['{"url":"","contact":"bob@example.com"}']
    assert _at_signs_outside_urls('{"url":"https://a.test/@h","n":1}') == []
    # URL followed by other delimiters, then an address.
    assert _at_signs_outside_urls("<link>https://a.test/@h</link><mail>x@y.z</mail>") == [
        "<link></link><mail>x@y.z</mail>"
    ]
    assert _at_signs_outside_urls("a=https://a.test/@h,b=x@y.z") == ["a=,b=x@y.z"]


def test_offending_strings_walks_compact_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"url": "https://a.test/x", "jobs": [{"contact": "bob@example.com"}]})
    )
    assert _offending_strings(bad) == ["bob@example.com"]

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"url": "https://a.test/@handle", "jobs": [{"n": 1}]}))
    assert _offending_strings(good) == []

    key = tmp_path / "key.json"
    key.write_text(json.dumps({"someone@example.com": "https://a.test/x"}))
    assert _offending_strings(key) == ["someone@example.com"]

    broken = tmp_path / "broken.json"
    broken.write_text('{"url":"https://a.test/x","contact":"bob@example.com"')
    assert _offending_strings(broken) == ['{"url":"","contact":"bob@example.com"']

    xml = tmp_path / "feed.xml"
    xml.write_text("<a>https://a.test/@h</a>\n<b>bob@example.com</b>\n")
    assert _offending_strings(xml) == ["<b>bob@example.com</b>"]


def _non_comment_source(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        lines.append(code)
    return "\n".join(lines)


def _guarded_sources() -> list[Path]:
    files = [REPO_ROOT / "tell" / "models.py"]
    files.extend(sorted((REPO_ROOT / "tell" / "collectors").rglob("*.py")))
    return files


def test_related_persons_list_is_never_a_stored_field() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _guarded_sources()
        if FORBIDDEN_FIELD in _non_comment_source(path)
    ]
    assert offenders == []
