"""PII guard (spec section 12): no personal data in models, fixtures or collector code."""

from __future__ import annotations

import json
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tell.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURE_DIRS = (FIXTURES_DIR, REPO_ROOT / "packages" / "tell-mcp" / "tests" / "fixtures")
"""Every recorded-fixture tree in the repository, the tell-mcp workspace package included."""

FORBIDDEN_COLUMNS = {"email", "phone", "first_name", "last_name"}
FORBIDDEN_SUFFIXES = ("_email", "_phone")

# A URL ends at whitespace or at a quote / JSON delimiter, so in compact (single-line)
# JSON the token never swallows the fields that follow it. Cutting a real URL short at a
# comma only ever errs toward flagging, never toward hiding an ``@``. The authority
# (everything between ``://`` and the first ``/``, ``?`` or ``#``) is captured so an
# ``@`` there (``https://user@host/``: userinfo, i.e. an address or a login) is kept
# for flagging, while ``@`` in the path (``/@handle``) is stripped with the URL.
URL_TOKEN = re.compile(r"""https?://(?P<authority>[^\s"'<>,}\]/?#]*)[^\s"'<>,}\]]*""")

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
    return sorted(p for root in FIXTURE_DIRS for p in root.rglob("*") if p.is_file())


def _strip_url(match: re.Match[str]) -> str:
    """Remove a URL unless its authority carries userinfo, in which case leave it to be flagged."""
    return match.group(0) if "@" in match.group("authority") else ""


def _at_signs_outside_urls(text: str) -> list[str]:
    stripped = URL_TOKEN.sub(_strip_url, text)
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
    for root in FIXTURE_DIRS:
        assert root.is_dir(), root


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


def test_at_sign_in_url_userinfo_is_flagged() -> None:
    # Userinfo in the authority is an address or a login: it stays and is reported.
    assert _at_signs_outside_urls("https://bob@example.com/jobs") == [
        "https://bob@example.com/jobs"
    ]
    assert _at_signs_outside_urls("https://bob:secret@example.com/") == [
        "https://bob:secret@example.com/"
    ]
    assert _at_signs_outside_urls('{"u":"https://bob@mail.test/x?y=1"}') == [
        '{"u":"https://bob@mail.test/x?y=1"}'
    ]
    # An ``@`` after the authority (path, query, fragment) is still part of a clean URL.
    assert _at_signs_outside_urls("https://example.com/@handle") == []
    assert _at_signs_outside_urls("https://example.com/x?who=@handle") == []
    assert _at_signs_outside_urls("https://example.com/x#@top") == []
    assert _at_signs_outside_urls("https://example.com:8443/@handle") == []


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
    """The module's tokens with only ``COMMENT`` tokens removed, space-separated.

    Tokenizing (rather than cutting each line at ``#``) means a ``#`` inside a string
    literal does not hide the rest of that line, and a docstring or any other string
    is scanned like code: the element name may appear in a ``#`` comment and nowhere
    else. Tokens are joined with a space so two adjacent names never fuse into the
    forbidden spelling by accident.
    """
    with path.open("rb") as fh:
        tokens = tokenize.tokenize(fh.readline)
        return " ".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)


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


def test_source_scan_skips_only_comments(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden.py"
    hidden.write_text(f"X = 1  # {FORBIDDEN_FIELD} is fine in a comment\n")
    assert FORBIDDEN_FIELD not in _non_comment_source(hidden)

    in_string = tmp_path / "in_string.py"
    in_string.write_text(f'FIELDS = ["issuer#", "{FORBIDDEN_FIELD}"]  # a # in a string\n')
    assert FORBIDDEN_FIELD in _non_comment_source(in_string)

    in_docstring = tmp_path / "in_docstring.py"
    in_docstring.write_text(f'"""Never store {FORBIDDEN_FIELD}."""\n')
    assert FORBIDDEN_FIELD in _non_comment_source(in_docstring)

    as_name = tmp_path / "as_name.py"
    as_name.write_text(f"{FORBIDDEN_FIELD} = None\n")
    assert FORBIDDEN_FIELD in _non_comment_source(as_name)

    split_names = tmp_path / "split.py"
    split_names.write_text("related = 1\nPersonsList = 2\n")
    assert FORBIDDEN_FIELD not in _non_comment_source(split_names)
