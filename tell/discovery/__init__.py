"""Discovery: map a company domain to its job board (spec section 4) and seed companies.

* :mod:`tell.discovery.ats` finds the ATS board behind a domain and writes ``ats_boards``.
* :mod:`tell.discovery.yc` seeds ``companies`` from the public YC company directory.
* :mod:`tell.discovery.cli` is the ``tell discover`` typer sub-app.

The domain helpers live here because both modules normalise domains the same way:
the registrable domain (eTLD+1 per the public suffix list, private suffixes such as
``vercel.app`` included), lowercase, no ``www``. ``tldextract`` is configured with
its bundled suffix-list snapshot only, so nothing here ever touches the network.

:func:`platform_domain` recognises hosts that belong to a platform rather than to a
company (an App Store listing, a LinkedIn page, a ``*.vercel.app`` preview; ADR 0033):
tell's identity key is the company's *own* domain, so such a website seeds nothing.
"""

from __future__ import annotations

import re
from functools import lru_cache

import tldextract

__all__ = [
    "PLATFORM_DOMAINS",
    "domain_label",
    "is_platform_domain",
    "platform_domain",
    "registrable_domain",
]

PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        # App stores and the big consumer platforms whose pages a directory links to
        # (apps.apple.com, play.google.com, sites.google.com, docs.google.com, ...).
        "apple.com",
        "google.com",
        "microsoft.com",
        "amazon.com",
        # Social and profile pages.
        "linkedin.com",
        "facebook.com",
        "fb.com",
        "instagram.com",
        "x.com",
        "twitter.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "threads.net",
        "discord.com",
        "discord.gg",
        "t.me",
        "wa.me",
        # Code hosting and per-project static hosting (``<project>.<platform>``).
        "github.com",
        "github.io",
        "gitlab.io",
        "bitbucket.org",
        "vercel.app",
        "netlify.app",
        "herokuapp.com",
        "pages.dev",
        "web.app",
        "firebaseapp.com",
        "replit.app",
        "repl.co",
        "glitch.me",
        "fly.dev",
        "onrender.com",
        "azurewebsites.net",
        "amazonaws.com",
        "cloudfront.net",
        # Site builders (hosted subdomains).
        "wixsite.com",
        "wix.com",
        "webflow.io",
        "framer.website",
        "framer.ai",
        "framer.app",
        "carrd.co",
        "squarespace.com",
        "wordpress.com",
        "blogspot.com",
        "tumblr.com",
        "notion.site",
        "notion.so",
        "super.site",
        "softr.app",
        "glide.page",
        "bubbleapps.io",
        "typedream.app",
        "lovable.app",
        "myshopify.com",
        "godaddysites.com",
        "weebly.com",
        "mystrikingly.com",
        # Publishing, link hubs and forms.
        "medium.com",
        "substack.com",
        "linktr.ee",
        "bit.ly",
        "calendly.com",
        "typeform.com",
        "forms.gle",
        # Startup directories (a profile page, not the company's site).
        "ycombinator.com",
        "workatastartup.com",
        "angel.co",
        "wellfound.com",
        "crunchbase.com",
        "producthunt.com",
    }
)
"""Registrable domains (ICANN rules, private suffixes ignored) that host other people's
pages. A company website on one of them, or on a subdomain of one, is not the company's
own domain. Keep the list to platforms seen in directory data; ADR 0033."""

_NOT_ALNUM = re.compile(r"[^0-9a-z]")


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # suffix_list_urls=() and cache_dir=None: use the snapshot shipped with the
    # package, never download the public suffix list (tests run without network).
    return tldextract.TLDExtract(
        suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
    )


def registrable_domain(value: str) -> str | None:
    """``https://www.Acme.com/careers`` -> ``acme.com``; ``None`` when there is none.

    Accepts a bare host or a URL. IP addresses, ``localhost`` and anything without a
    public suffix return ``None``.
    """
    text = value.strip().lower()
    if not text:
        return None
    result = _extractor()(text)
    if not result.domain or not result.suffix:
        return None
    return result.top_domain_under_public_suffix


def domain_label(domain: str) -> str:
    """The registrable label of a domain: ``acme.co.uk`` -> ``acme``."""
    return _extractor()(domain.strip().lower()).domain


def platform_domain(value: str) -> str | None:
    """The :data:`PLATFORM_DOMAINS` entry a host or URL belongs to, else ``None``.

    Compared on the ICANN registrable domain, so ``apps.apple.com`` -> ``apple.com`` and
    ``acme.vercel.app`` -> ``vercel.app`` (the private-suffix view used for company domains
    would keep ``acme.vercel.app``).
    """
    text = value.strip().lower()
    if not text:
        return None
    result = _extractor()(text, include_psl_private_domains=False)
    if not result.domain or not result.suffix:
        return None
    top = result.top_domain_under_public_suffix
    return top if top in PLATFORM_DOMAINS else None


def _compact(text: str) -> str:
    return _NOT_ALNUM.sub("", text.casefold())


def is_platform_domain(value: str, *, name: str | None = None) -> bool:
    """Whether ``value`` is a platform's page rather than the company's own domain.

    ``name`` exempts the platform's own company: ``substack.com`` for a company named
    "Substack" is that company's domain (the name, letters and digits only, equals the
    platform's label), while ``substack.com`` for any other name is a newsletter page.
    The exemption needs the bare platform domain (``www.`` allowed); ``acme.substack.com``
    and ``acme.github.io`` are never the platform's own.
    """
    platform = platform_domain(value)
    if platform is None:
        return False
    if name is None:
        return True
    host = _extractor()(value.strip().lower())
    own = host.top_domain_under_public_suffix == platform and host.subdomain in ("", "www")
    return not (own and _compact(name) == _compact(domain_label(platform)))
