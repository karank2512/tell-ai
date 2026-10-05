"""Discovery: map a company domain to its job board (spec section 4) and seed companies.

* :mod:`tell.discovery.ats` finds the ATS board behind a domain and writes ``ats_boards``.
* :mod:`tell.discovery.yc` seeds ``companies`` from the public YC company directory.
* :mod:`tell.discovery.cli` is the ``tell discover`` typer sub-app.

The domain helpers live here because both modules normalise domains the same way:
the registrable domain (eTLD+1 per the public suffix list, private suffixes such as
``vercel.app`` included), lowercase, no ``www``. ``tldextract`` is configured with
its bundled suffix-list snapshot only, so nothing here ever touches the network.
"""

from __future__ import annotations

from functools import lru_cache

import tldextract

__all__ = ["domain_label", "registrable_domain"]


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
