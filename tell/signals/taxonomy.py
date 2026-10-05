"""Role-family taxonomy and requisition fingerprints (spec section 7).

Phase 1 ships deliberately thin stubs so ``postings`` NOT NULL columns can be filled
by the collector base today; Phase 6 replaces the bodies without changing the
signatures, so ``tell/collectors/base.py`` never has to change.

TODO(Phase 6): implement the ordered keyword lists for the ten families, the
``is_intern`` tag, seniority-token stripping in ``normalize_title`` and the
city-and-country ``location_norm`` inside :func:`fingerprint`.
"""

from __future__ import annotations

import hashlib
import re

OTHER = "other"
"""The fall-through family when no keyword list matches (and, for now, always)."""

_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lower-case ``title`` and collapse runs of whitespace to one space.

    TODO(Phase 6): also strip seniority tokens (``senior``, ``sr``, ``staff``,
    ``principal``, ``lead``, ``ii``, ``iii``, roman numerals) per section 7.
    """
    return _WHITESPACE.sub(" ", title).strip().lower()


def role_family(title: str, department: str | None = None) -> str:
    """Return the role family for a posting title; ``"other"`` until Phase 6 lands.

    TODO(Phase 6): ordered keyword matching, first family wins; ``sales engineer``
    and ``solutions engineer`` to ``sales``; ``exec`` only when nothing else
    matched; non-English titles fall to ``other``.
    """
    _ = (normalize_title(title), department)
    return OTHER


def fingerprint(
    title_norm: str, department: str | None, location: str | None, remote: bool | None
) -> str:
    """``sha1(title_norm | department | location | remote_flag)`` as 40 hex chars.

    ``remote`` renders as ``"1"``, ``"0"`` or ``""`` (unknown); ``None`` text fields
    render as ``""``. The hash is a stable requisition identity, not a secret.

    TODO(Phase 6): normalize ``department`` and reduce ``location`` to city and
    country before hashing (section 7).
    """
    remote_flag = "" if remote is None else ("1" if remote else "0")
    joined = "|".join((title_norm, department or "", location or "", remote_flag))
    return hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()
