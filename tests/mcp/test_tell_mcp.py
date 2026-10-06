"""tell-mcp (SPEC section 10 "MCP server", PLAN Phase 9).

The tools read the static JSON API; here it is served by respx from
``packages/tell-mcp/tests/fixtures/api/v0`` (a site rendered from the publish scenario,
plus one hand-written company, ``northwind.example``, with two role families and a
decayed signal). Every fixture is validated against the published schemas first, so the
tools are tested against the real contract. No test touches the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Coroutine, Iterator
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jsonschema
import pytest
import respx
from mcp.client import Client

from tell.publish.json_api import (
    COMPANY_SCHEMA_PATH,
    validate_company_json,
    validate_health_json,
    validate_watchlist_json,
    validate_weekly_json,
)
from tell_mcp import __version__
from tell_mcp import server as srv
from tell_mcp.client import (
    DEFAULT_BASE_URL,
    USER_AGENT,
    TellClient,
    TellError,
    normalize_domain,
    normalize_slug,
)
from tell_mcp.server import ToolError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "packages" / "tell-mcp" / "tests" / "fixtures"
API = FIXTURES / "api" / "v0"
BASE = "https://tell-data.test/site"
API_URL = "https://api.tell.test"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _company(domain: str) -> dict[str, Any]:
    data: dict[str, Any] = _load(API / "companies" / f"{domain}.json")
    return data


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _serve_fixture(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix(urlsplit(BASE).path)
    file = FIXTURES / path.lstrip("/")
    if not path.startswith("/api/v0/") or not file.is_file():
        return httpx.Response(404, text="Not Found")
    return httpx.Response(
        200, content=file.read_bytes(), headers={"content-type": "application/json"}
    )


@pytest.fixture
def overrides() -> dict[str, httpx.Response]:
    """Responses that replace the fixture tree for one test, by path under ``BASE``."""
    return {}


@pytest.fixture
def site(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, httpx.Response]
) -> Iterator[respx.MockRouter]:
    """The static API at ``BASE``, served from the fixtures; everything else 404s."""
    monkeypatch.setenv("TELL_BASE_URL", BASE)

    def serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(urlsplit(BASE).path)
        if path in overrides:
            return overrides[path]
        return _serve_fixture(request)

    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=BASE).mock(side_effect=serve)
        yield router


# --- fixtures follow the published contract ---------------------------------------------------


def test_company_fixtures_validate_against_the_published_schema() -> None:
    schema = _load(COMPANY_SCHEMA_PATH)
    files = sorted((API / "companies").glob("*.json"))
    assert len(files) >= 4
    for file in files:
        obj = _load(file)
        jsonschema.Draft202012Validator(schema).validate(obj)
        validate_company_json(obj)
        assert file.stem == obj["domain"]
        assert sum(c["contribution"] for c in obj["contributions"]) == pytest.approx(obj["score"])


def test_watchlist_weekly_and_health_fixtures_validate() -> None:
    validate_watchlist_json(_load(API / "watchlists" / "pilot.json"))
    validate_weekly_json(_load(API / "weekly" / "latest.json"))
    validate_health_json(_load(API / "health.json"))


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def test_fixtures_hold_no_personal_data() -> None:
    url = re.compile(r"https?://[^\s\"]+")
    for file in FIXTURES.rglob("*.json"):
        text = file.read_text(encoding="utf-8")
        assert "@" not in url.sub("", text), file
        keys = {k.lower() for k in _keys(json.loads(text))}
        assert not keys & {"email", "phone", "person", "owner", "first_name", "last_name"}, file


# --- normalisation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.com", "acme.com"),
        ("  ACME.com ", "acme.com"),
        ("https://www.Acme.com/careers?x=1", "acme.com"),
        ("http://acme.co.uk:8080", "acme.co.uk"),
        ("www.acme.io.", "acme.io"),
        ("//sub.acme.com/", "sub.acme.com"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "acme", "not a domain", "https://", "acme..com", "[::1]"])
def test_normalize_domain_rejects_non_domains(raw: str) -> None:
    with pytest.raises(ValueError, match="not a company domain"):
        normalize_domain(raw)


def test_normalize_slug() -> None:
    assert normalize_slug(" Pilot ") == "pilot"
    with pytest.raises(ValueError, match="not a watchlist slug"):
        normalize_slug("../etc")


# --- client -------------------------------------------------------------------------------------


def test_client_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TellClient.from_env({})
    assert client.base_url == DEFAULT_BASE_URL == "https://karank2512.github.io/tell-data"
    assert not client.uses_api
    assert client.timeout == 20.0
    assert USER_AGENT == "tell-mcp/0.1 (+https://github.com/karank2512/tell-ai)"
    # An API key alone does not switch to the API: there is no service URL yet.
    assert not TellClient.from_env({"TELL_API_KEY": "tell_x"}).uses_api


def test_client_company_sends_user_agent_and_normalises(site: respx.MockRouter) -> None:
    client = TellClient.from_env()
    company = run(client.company("https://www.Quillstone.example/about"))
    assert company is not None
    assert company["domain"] == "quillstone.example"
    request = site.calls.last.request
    assert request.url.path.endswith("/api/v0/companies/quillstone.example.json")
    assert request.headers["user-agent"] == USER_AGENT
    assert "authorization" not in request.headers


def test_client_404_is_none(site: respx.MockRouter) -> None:
    client = TellClient.from_env()
    assert run(client.company("unknown.example")) is None
    assert run(client.watchlist("private-list")) is None


def test_client_weekly_and_health(site: respx.MockRouter) -> None:
    client = TellClient.from_env()
    weekly = run(client.weekly())
    health = run(client.health())
    assert weekly is not None
    assert re.match(r"^\d{4}-W\d{2}$", weekly["week"])
    assert health is not None
    assert {s["source"] for s in health["sources"]} >= {"greenhouse", "edgar"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(403), "check TELL_API_KEY"),
        (httpx.Response(200, text="<html>"), "not JSON"),
        (httpx.Response(200, json=[1, 2]), "expected a JSON object"),
        (httpx.ConnectError("boom"), "could not reach"),
        (httpx.ReadTimeout("slow"), "timed out after 20s"),
    ],
)
def test_client_errors_raise_tell_error(response: Any, message: str) -> None:
    with respx.mock() as router:
        route = router.get(f"{BASE}/api/v0/companies/acme.com.json")
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        with pytest.raises(TellError, match=message):
            run(TellClient(base_url=BASE).company("acme.com"))


def test_client_prefers_the_api_with_key_and_url() -> None:
    client = TellClient.from_env(
        {"TELL_BASE_URL": BASE, "TELL_API_KEY": "tell_testkey", "TELL_API_URL": API_URL + "/"}
    )
    assert client.uses_api
    with respx.mock() as router:
        company_route = router.get(f"{API_URL}/v1/companies/acme.com").mock(
            return_value=httpx.Response(200, json=_company("northwind.example"))
        )
        market_route = router.get(f"{API_URL}/v1/in-market").mock(
            return_value=httpx.Response(200, json=[{"domain": "acme.com", "score": 1.0}])
        )
        weekly_route = router.get(f"{BASE}/api/v0/weekly/latest.json").mock(
            return_value=httpx.Response(200, json={"week": "2026-W40"})
        )
        assert run(client.company("www.acme.com")) is not None
        watchlist = run(client.watchlist("Pilot"))
        assert run(client.weekly()) == {"week": "2026-W40"}
    assert company_route.calls.last.request.headers["authorization"] == "Bearer tell_testkey"
    market = market_route.calls.last.request
    assert market.url.params["watchlist"] == "pilot"
    assert market.headers["authorization"] == "Bearer tell_testkey"
    assert watchlist == {"slug": "pilot", "companies": [{"domain": "acme.com", "score": 1.0}]}
    # The static site never receives the key.
    assert "authorization" not in weekly_route.calls.last.request.headers


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"companies": [{"domain": "a.com"}]}, [{"domain": "a.com"}]),
        ({"results": [{"domain": "b.com"}]}, [{"domain": "b.com"}]),
    ],
)
def test_client_api_watchlist_shapes(body: dict[str, Any], expected: list[Any]) -> None:
    client = TellClient(base_url=BASE, api_key="tell_k", api_url=API_URL)
    with respx.mock() as router:
        router.get(f"{API_URL}/v1/in-market").mock(return_value=httpx.Response(200, json=body))
        result = run(client.watchlist("pilot"))
    assert result is not None
    assert result["companies"] == expected
    assert result["slug"] == "pilot"


def test_client_api_watchlist_rejects_other_shapes() -> None:
    client = TellClient(base_url=BASE, api_key="tell_k", api_url=API_URL)
    with respx.mock() as router:
        router.get(f"{API_URL}/v1/in-market").mock(
            side_effect=[httpx.Response(200, json={"oops": 1}), httpx.Response(404)]
        )
        with pytest.raises(TellError, match="expected a list of companies"):
            run(client.watchlist("pilot"))
        assert run(client.watchlist("pilot")) is None


# --- watch_accounts -----------------------------------------------------------------------------


def test_watch_accounts_returns_the_prefilled_issue_form() -> None:
    out = run(
        srv.watch_accounts(
            ["https://www.Acme.com", "acme.com", "beta.io", "not a domain"], "Q4 Pipeline"
        )
    )
    assert out["status"] == "form_link"
    assert out["domains"] == ["acme.com", "beta.io"]
    assert out["invalid"] == ["not a domain"]
    assert out["slug"] == "q4-pipeline"
    link = out["form_url"]
    assert link.startswith(
        "https://github.com/karank2512/tell-ai/issues/new?template=watch-domains.yml&"
    )
    query = parse_qs(urlsplit(link).query)
    assert query["template"] == ["watch-domains.yml"]
    assert query["domains"] == ["acme.com\nbeta.io"]
    assert query["title"] == ["Watch these domains: Q4 Pipeline"]
    assert "Q4 Pipeline" in query["notes"][0]
    assert "+" not in link  # spaces are %20, which GitHub's form prefill decodes
    assert "nightly job" in out["message"]


def test_watch_accounts_uses_the_intake_form_with_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TELL_INTAKE_FORM_URL", "https://forms.test/viewform?entry.1={domains}&entry.2={watchlist}"
    )
    out = run(srv.watch_accounts(["acme.com", "beta.io"], "my list"))
    assert out["form_url"] == (
        "https://forms.test/viewform?entry.1=acme.com%0Abeta.io&entry.2=my%20list"
    )


def test_intake_link_appends_values_without_placeholders() -> None:
    assert srv.intake_link(["acme.com"], "x", form_url="https://forms.test/f") == (
        "https://forms.test/f?domains=acme.com&watchlist=x"
    )
    assert srv.intake_link(["a.com"], "y", form_url="https://forms.test/f?usp=pp").endswith(
        "?usp=pp&domains=a.com&watchlist=y"
    )


def test_watch_accounts_caps_at_fifty_domains() -> None:
    domains = [f"company{i}.example" for i in range(55)]
    out = run(srv.watch_accounts(domains, "big"))
    assert len(out["domains"]) == 50
    assert out["not_included"] == domains[50:]
    assert "separately" in out["message"]


@pytest.mark.parametrize(
    ("domains", "watchlist", "message"),
    [(["acme.com"], "  ", "non-empty"), (["nope"], "x", "no valid company domains")],
)
def test_watch_accounts_rejects_bad_input(domains: list[str], watchlist: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        run(srv.watch_accounts(domains, watchlist))


# --- in_market ----------------------------------------------------------------------------------


def test_in_market_defaults_filter_and_sort(site: respx.MockRouter) -> None:
    rows = run(srv.in_market("pilot"))
    # greenhouse-fixture.test scores 2.7 (< 6.0) and drops out.
    assert [r["domain"] for r in rows] == ["examplerobotics.example", "quillstone.example"]
    assert [r["score"] for r in rows] == [27.7, 7.5]
    assert set(rows[0]) == {"domain", "name", "score", "heat", "top_contributions"}
    assert rows[0]["heat"] == 100
    for row in rows:
        assert 1 <= len(row["top_contributions"]) <= 3
        for c in row["top_contributions"]:
            assert c["url"].startswith("https://")
            assert "postings" not in c
    assert len(rows[1]["top_contributions"]) == 3  # quillstone has five


def test_in_market_since_days_uses_the_top_contribution(site: respx.MockRouter) -> None:
    # as_of 2026-10-06: robotics' top signal is 2026-10-01, quillstone's 2026-10-03.
    rows = run(srv.in_market("pilot", since_days=3))
    assert [r["domain"] for r in rows] == ["quillstone.example"]
    assert run(srv.in_market("pilot", since_days=2)) == []


def test_in_market_min_score_and_families(site: respx.MockRouter) -> None:
    rows = run(srv.in_market("Pilot", min_score=0))
    assert [r["domain"] for r in rows] == [
        "examplerobotics.example",
        "quillstone.example",
        "greenhouse-fixture.test",
    ]
    sales = run(srv.in_market("pilot", min_score=0, families=["Sales"]))
    assert [r["domain"] for r in sales] == ["greenhouse-fixture.test"]
    assert run(srv.in_market("pilot", min_score=0, families=["engineering"])) == []
    assert run(srv.in_market("pilot", min_score=50)) == []


def _growth_watchlist() -> dict[str, Any]:
    pilot = _load(API / "watchlists" / "pilot.json")
    northwind = _company("northwind.example") | {"heat": 100}
    ghost = copy.deepcopy(northwind) | {"domain": "ghost.example", "score": 7.9, "heat": 50}
    ghost["contributions"][0]["occurred_at"] = "2026-09-01"
    tie = copy.deepcopy(northwind) | {"domain": "aaa.example", "heat": 100}
    pilot["companies"] = [ghost, northwind, tie, "junk", {"domain": "x.example"}]
    return dict(pilot)


def test_select_in_market_families_pick_the_top_family_contribution() -> None:
    watchlist = _growth_watchlist()
    rows = srv.select_in_market(watchlist, families=["engineering"])
    # Engineering's top contribution (2026-10-01) is within 7 days for all three copies.
    assert [r["domain"] for r in rows] == ["aaa.example", "northwind.example", "ghost.example"]
    sales = srv.select_in_market(watchlist, families=["sales"], since_days=7)
    # ghost.example's sales spike was moved to 2026-09-01: stale.
    assert [r["domain"] for r in sales] == ["aaa.example", "northwind.example"]
    assert sales[0]["top_contributions"][0]["role_family"] == "sales"


def test_select_in_market_without_as_of_uses_today() -> None:
    watchlist = _growth_watchlist()
    del watchlist["as_of"]
    rows = srv.select_in_market(watchlist, today=date(2026, 10, 30))
    assert rows == []
    rows = srv.select_in_market(watchlist, today=date(2026, 10, 8))
    assert [r["domain"] for r in rows] == ["aaa.example", "northwind.example"]


def test_in_market_unpublished_watchlist_is_a_tool_error(site: respx.MockRouter) -> None:
    with pytest.raises(ToolError, match="not published"):
        run(srv.in_market("private-list"))
    with pytest.raises(ToolError, match="not a watchlist slug"):
        run(srv.in_market("../../etc"))
    with pytest.raises(ToolError, match="since_days"):
        run(srv.in_market("pilot", since_days=-1))


def test_in_market_network_error_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELL_BASE_URL", BASE)
    with respx.mock() as router:
        router.get(url__startswith=BASE).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ToolError, match="could not reach"):
            run(srv.in_market("pilot"))


# --- explain ------------------------------------------------------------------------------------


def test_explain_returns_every_contribution_with_receipts(site: respx.MockRouter) -> None:
    out = run(srv.explain("https://www.northwind.example"))
    assert out["monitored"] is True
    assert out["domain"] == "northwind.example"
    assert [c["type"] for c in out["contributions"]] == [
        "hiring_velocity",
        "hiring_velocity",
        "funding",
    ]
    assert all(c["url"].startswith("https://") for c in out["contributions"])
    assert len(out["contributions"][0]["postings"]) == 2
    assert out["why_not_higher"] == _company("northwind.example")["why_not_higher"]
    assert out["account_page"] == f"{BASE}/companies/northwind.example.html"


def test_explain_unknown_domain_is_not_monitored(site: respx.MockRouter) -> None:
    out = run(srv.explain("Unknown.example"))
    assert out == {
        "domain": "unknown.example",
        "monitored": False,
        "message": out["message"],
    }
    assert "cannot see" in out["message"]
    with pytest.raises(ToolError, match="not a company domain"):
        run(srv.explain("nope"))


# --- events -------------------------------------------------------------------------------------


def test_events_lists_contributions_and_postings_newest_first(site: respx.MockRouter) -> None:
    out = run(srv.events("northwind.example"))
    assert out["monitored"] is True
    assert "v0 limitation" in out["note"]
    assert out["truncated"] is False
    found = out["events"]
    dates = [e["occurred_at"] for e in found]
    assert dates == sorted(dates, reverse=True)
    assert [(e["kind"], e["occurred_at"]) for e in found[:3]] == [
        ("contribution", "2026-10-04"),
        ("posting", "2026-10-04"),
        ("posting", "2026-10-03"),
    ]
    assert len(found) == 3 + 3  # three contributions, three counted postings
    assert all(e["url"].startswith("https://") for e in found)
    posting = next(e for e in found if e["kind"] == "posting")
    assert posting["type"] == "hiring_velocity"
    assert posting["role_family"] == "sales"


def test_events_filters_since_and_types(site: respx.MockRouter) -> None:
    recent = run(srv.events("northwind.example", since="2026-10-01"))
    assert {e["occurred_at"] for e in recent["events"]} <= {
        "2026-10-01",
        "2026-10-03",
        "2026-10-04",
    }
    postings = run(srv.events("northwind.example", types=["posting"]))
    assert {e["kind"] for e in postings["events"]} == {"posting"}
    funding = run(srv.events("northwind.example", types=["FUNDING"]))
    assert [e["type"] for e in funding["events"]] == ["funding"]
    with pytest.raises(ToolError, match="ISO date"):
        run(srv.events("northwind.example", since="last week"))


def test_events_unknown_domain_and_truncation(site: respx.MockRouter) -> None:
    out = run(srv.events("unknown.example"))
    assert out["monitored"] is False
    assert out["events"] == []
    company = _company("northwind.example")
    company["contributions"][0]["postings"] = [
        {"title": f"Role {i}", "posted_at": "2026-10-02", "url": f"https://jobs.test/{i}"}
        for i in range(250)
    ] + [{"title": "dupe", "posted_at": "2026-10-02", "url": "https://jobs.test/1"}, "junk"]
    found, truncated = srv.derive_events(company)
    assert truncated is True
    assert len(found) == srv.MAX_EVENTS == 200
    shorter, cut = srv.derive_events(company, types=["funding"])
    assert (len(shorter), cut) == (1, False)


# --- coverage -----------------------------------------------------------------------------------


def test_coverage_is_honest_per_domain(
    site: respx.MockRouter, overrides: dict[str, httpx.Response]
) -> None:
    overrides["/api/v0/companies/broken.example.json"] = httpx.Response(503)
    rows = run(
        srv.coverage(
            [
                "northwind.example",
                "https://greenhouse-fixture.test",
                "examplerobotics.example",
                "quillstone.example",
                "unknown.example",
                "not a domain",
                "broken.example",
            ]
        )
    )
    by = {r["domain"]: r for r in rows}
    assert [r["domain"] for r in rows][:5] == [
        "northwind.example",
        "greenhouse-fixture.test",
        "examplerobotics.example",
        "quillstone.example",
        "unknown.example",
    ]
    nw = by["northwind.example"]
    assert (nw["status"], nw["has_board"], nw["has_cik"], nw["has_feed"]) == (
        "monitored",
        True,
        True,
        True,
    )
    assert nw["board"] == "lever:northwind-example"
    assert "all monitored" in nw["note"]
    gh = by["greenhouse-fixture.test"]
    assert (gh["has_board"], gh["has_cik"], gh["has_feed"]) == (True, False, False)
    assert "Form D" in gh["note"]
    rb = by["examplerobotics.example"]
    assert (rb["has_board"], rb["board"], rb["has_cik"], rb["cik"]) == (
        False,
        None,
        True,
        "0009990001",
    )
    assert "hiring velocity is invisible" in rb["note"]
    assert by["quillstone.example"]["has_feed"] is True
    unknown = by["unknown.example"]
    assert unknown["status"] == "not monitored"
    assert unknown["has_board"] is None
    assert "cannot see" in unknown["note"]
    assert by["not a domain"]["status"] == "invalid domain"
    assert by["broken.example"]["status"] == "error"
    assert "HTTP 503" in by["broken.example"]["note"]


def test_coverage_limits_the_batch() -> None:
    with pytest.raises(ToolError, match="at most 200"):
        run(srv.coverage([f"c{i}.example" for i in range(201)]))


def test_coverage_row_tolerates_missing_sources() -> None:
    row = srv.coverage_row("acme.com", {"name": "Acme"})
    assert (row["has_board"], row["has_cik"], row["has_feed"]) == (False, False, False)


# --- why_now_email prompt -----------------------------------------------------------------------


def test_why_now_prompt_inlines_the_top_two_receipts(site: respx.MockRouter) -> None:
    text = run(srv.why_now_email("www.northwind.example"))
    company = _company("northwind.example")
    first, second, third = company["contributions"]
    assert "two sentences" in text
    assert first["url"] in text
    assert second["postings"][0]["url"] not in text
    assert "4 new sales roles" in text
    assert "3 new engineering roles" in text
    assert third["url"] not in text
    assert "Receipts:" in text


def test_why_now_prompt_without_data(
    site: respx.MockRouter, overrides: dict[str, httpx.Response]
) -> None:
    assert "do not invent one" in run(srv.why_now_email("unknown.example"))
    assert "Ask the user" in run(srv.why_now_email("nope"))
    quiet = _company("northwind.example") | {"contributions": [], "score": 0}
    assert "no live signal" in srv.why_now_text("northwind.example", quiet)
    overrides["/api/v0/companies/down.example.json"] = httpx.Response(502)
    assert "could not be fetched" in run(srv.why_now_email("down.example"))


# --- the server object --------------------------------------------------------------------------


def test_server_lists_exactly_five_tools_and_one_prompt() -> None:
    tools = asyncio.run(srv.mcp.list_tools())
    prompts = asyncio.run(srv.mcp.list_prompts())
    assert sorted(t.name for t in tools) == sorted(
        ["watch_accounts", "in_market", "explain", "events", "coverage"]
    )
    assert [p.name for p in prompts] == ["why_now_email"]
    assert [a.name for a in prompts[0].arguments or []] == ["domain"]
    for tool in tools:
        description = tool.description or ""
        assert "`url`" in description, tool.name
        assert "`explain`" in description, tool.name


def test_tool_input_schemas_match_the_spec() -> None:
    schemas = {t.name: t.input_schema for t in asyncio.run(srv.mcp.list_tools())}
    assert set(schemas["watch_accounts"]["required"]) == {"domains", "watchlist"}
    in_market = schemas["in_market"]
    assert in_market["required"] == ["watchlist"]
    assert in_market["properties"]["since_days"]["default"] == 7
    assert in_market["properties"]["min_score"]["default"] == 6.0
    assert set(in_market["properties"]) == {"watchlist", "since_days", "min_score", "families"}
    assert schemas["explain"]["required"] == ["domain"]
    assert set(schemas["events"]["properties"]) == {"domain", "since", "types"}
    assert schemas["coverage"]["required"] == ["domains"]


def test_network_errors_come_back_as_tool_errors_and_the_server_keeps_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELL_BASE_URL", BASE)

    async def session() -> tuple[Any, Any]:
        async with Client(srv.mcp) as client:
            failed = await client.call_tool("explain", {"domain": "acme.com"})
            ok = await client.call_tool("watch_accounts", {"domains": ["a.com"], "watchlist": "w"})
            return failed, ok

    with respx.mock() as router:
        router.get(url__startswith=BASE).mock(side_effect=httpx.ConnectError("offline"))
        failed, ok = asyncio.run(session())
    assert failed.is_error is True
    assert "could not reach" in failed.content[0].text
    assert ok.is_error is False


# --- entry point --------------------------------------------------------------------------------


def test_main_version_and_help_exit_before_serving(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_serve(*args: Any) -> None:
        raise AssertionError("the server must not start")

    monkeypatch.setattr(srv.mcp, "run", no_serve)
    with pytest.raises(SystemExit) as version:
        srv.main(["--version"])
    assert version.value.code == 0
    assert capsys.readouterr().out.strip() == f"tell-mcp {__version__}"
    with pytest.raises(SystemExit) as helped:
        srv.main(["--help"])
    assert helped.value.code == 0
    assert "TELL_BASE_URL" in capsys.readouterr().out


def test_main_runs_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(srv.mcp, "run", calls.append)
    srv.main([])
    assert calls == ["stdio"]
