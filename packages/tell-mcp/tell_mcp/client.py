"""HTTP client for tell's public data: the static JSON API v0, later the FastAPI service.

The v0 API is static JSON on GitHub Pages (``docs/SPEC.md`` section 10)::

    GET {TELL_BASE_URL}/api/v0/companies/{domain}.json
    GET {TELL_BASE_URL}/api/v0/watchlists/{slug}.json
    GET {TELL_BASE_URL}/api/v0/weekly/latest.json
    GET {TELL_BASE_URL}/api/v0/health.json

When both ``TELL_API_KEY`` and ``TELL_API_URL`` are set, company and watchlist reads go
to the FastAPI service instead (``GET /v1/companies/{domain}`` and
``GET /v1/in-market?watchlist=...``, ``Authorization: Bearer <key>``); weekly and health
always come from the static files, which the service never replaces.

A missing file (HTTP 404) is ``None``: tell does not publish that domain or watchlist.
Every other failure (DNS, timeout, 5xx, a body that is not a JSON object) raises
:class:`TellError` with a one-line message fit to show an agent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from tell_mcp import __version__

__all__ = [
    "DEFAULT_BASE_URL",
    "TIMEOUT_S",
    "USER_AGENT",
    "TellClient",
    "TellError",
    "normalize_domain",
    "normalize_slug",
]

DEFAULT_BASE_URL = "https://karank2512.github.io/tell-data"
"""Public GitHub Pages root of the ``tell-data`` repo (``TELL_BASE_URL``)."""

TIMEOUT_S = 20.0
USER_AGENT = f"tell-mcp/{__version__.rsplit('.', 1)[0]} (+https://github.com/karank2512/tell-ai)"

_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


class TellError(Exception):
    """A read from tell failed for a reason other than "not published" (404)."""


def normalize_domain(value: str) -> str:
    """``https://www.Acme.com/careers`` -> ``acme.com``.

    Lowercases, strips whitespace, a scheme, userinfo, a port, any path/query/fragment, a
    leading ``www.`` and a trailing dot. Raises ``ValueError`` when what is left is not a
    hostname with at least one dot (the shape ``docs/api/company.schema.json`` accepts).
    """
    text = value.strip().lower()
    if not _SCHEME.match(text) and not text.startswith("//"):
        text = "//" + text
    host = urlsplit(text).hostname or ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not _DOMAIN.match(host):
        msg = f"not a company domain: {value!r} (expected something like acme.com)"
        raise ValueError(msg)
    return host


def normalize_slug(value: str) -> str:
    """A public watchlist slug, lowercased; ``ValueError`` unless it matches ``[a-z0-9-]``."""
    slug = value.strip().lower()
    if not _SLUG.match(slug):
        msg = f"not a watchlist slug: {value!r} (expected lowercase letters, digits and dashes)"
        raise ValueError(msg)
    return slug


class TellClient:
    """Reads tell's published JSON; one short-lived ``httpx.AsyncClient`` per call.

    ``transport`` exists for tests (an ``httpx.MockTransport``); production code leaves it
    ``None``. respx-based tests patch httpx globally and need nothing.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        *,
        timeout: float = TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or None
        self.api_url = api_url.rstrip("/") if api_url else None
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TellClient:
        """Build from ``TELL_BASE_URL``, ``TELL_API_KEY`` and ``TELL_API_URL``."""
        env = os.environ if environ is None else environ
        return cls(
            base_url=env.get("TELL_BASE_URL") or None,
            api_key=env.get("TELL_API_KEY") or None,
            api_url=env.get("TELL_API_URL") or None,
        )

    # --- public reads -------------------------------------------------------------------------

    @property
    def uses_api(self) -> bool:
        """True when company and watchlist reads go to the FastAPI service."""
        return bool(self.api_key and self.api_url)

    def account_page(self, domain: str) -> str:
        """The human-readable account page for ``domain`` on the static site."""
        return f"{self.base_url}/companies/{quote(normalize_domain(domain), safe='.')}.html"

    def company_url(self, domain: str) -> str:
        """Where :meth:`company` reads ``domain`` from."""
        name = quote(normalize_domain(domain), safe=".")
        if self.uses_api:
            return f"{self.api_url}/v1/companies/{name}"
        return f"{self.base_url}/api/v0/companies/{name}.json"

    def watchlist_url(self, slug: str) -> str:
        """Where :meth:`watchlist` reads ``slug`` from (the static file, for links)."""
        return f"{self.base_url}/api/v0/watchlists/{normalize_slug(slug)}.json"

    async def company(self, domain: str) -> dict[str, Any] | None:
        """The per-domain object of SPEC section 9, or ``None`` when it is not published."""
        return await self._get_object(self.company_url(domain))

    async def watchlist(self, slug: str) -> dict[str, Any] | None:
        """A public watchlist (``{slug, name, profile, as_of, companies: [...]}``) or ``None``."""
        name = normalize_slug(slug)
        if not self.uses_api:
            return await self._get_object(self.watchlist_url(name))
        url = f"{self.api_url}/v1/in-market"
        body = await self._get(url, params={"watchlist": name, "min_score": "0"})
        if body is None:
            return None
        if isinstance(body, list):
            return {"slug": name, "companies": body}
        if isinstance(body, dict):
            body.setdefault("slug", name)
            if isinstance(body.get("companies"), list):
                return body
            if isinstance(body.get("results"), list):
                body["companies"] = body.pop("results")
                return body
        msg = f"unexpected response from {url}: expected a list of companies"
        raise TellError(msg)

    async def weekly(self) -> dict[str, Any] | None:
        """``api/v0/weekly/latest.json`` (always the static file)."""
        return await self._get_object(f"{self.base_url}/api/v0/weekly/latest.json")

    async def health(self) -> dict[str, Any] | None:
        """``api/v0/health.json`` (always the static file)."""
        return await self._get_object(f"{self.base_url}/api/v0/health.json")

    # --- plumbing -----------------------------------------------------------------------------

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.uses_api and self.api_url and url.startswith(self.api_url):
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _get_object(self, url: str) -> dict[str, Any] | None:
        body = await self._get(url)
        if body is None:
            return None
        if not isinstance(body, dict):
            msg = f"unexpected response from {url}: expected a JSON object"
            raise TellError(msg)
        return body

    async def _get(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, transport=self._transport
            ) as http:
                response = await http.get(url, params=params, headers=self._headers(url))
        except httpx.TimeoutException as exc:
            msg = f"timed out after {self.timeout:g}s reading {url}"
            raise TellError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"could not reach {url}: {type(exc).__name__}: {exc}"
            raise TellError(msg) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            msg = f"HTTP {response.status_code} from {url}: check TELL_API_KEY"
            raise TellError(msg)
        if response.status_code >= 400:
            msg = f"HTTP {response.status_code} from {url}"
            raise TellError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"response from {url} is not JSON"
            raise TellError(msg) from exc
