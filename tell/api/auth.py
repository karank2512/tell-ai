"""Bearer keys and metering for the v1 service (spec section 10, ADR 0024).

Keys
====

A key is ``tell_`` followed by 32 random bytes in unpadded base64url (43 characters),
48 characters in total. Only ``sha256(key)`` (hex) is stored, in ``api_keys.key_hash``,
with the first 12 characters as ``key_prefix`` so an operator can tell keys apart in
``tell api usage``. The raw key is shown once, by ``tell api create-key``, and is never
written anywhere. ``owner`` is a handle (lowercase letters, digits, ``.``, ``_``, ``-``),
never an email address.

Authentication
==============

:func:`authenticate` reads ``Authorization: Bearer <key>``; a missing or malformed
header, an unknown key and a revoked key all answer 401 with ``WWW-Authenticate:
Bearer`` and the same detail, so a caller cannot probe which keys exist.

Metering
========

:func:`meter` runs before every handler. One statement upserts ``api_usage (key_id,
day, calls)`` for the current UTC day, incrementing ``calls`` only while it is below the
key's ``daily_quota``; when no row comes back the quota is spent and the request is
refused with 429 and a ``Retry-After`` that points at the next UTC midnight. Refused
calls are not counted. ``last_used_at`` is written at most once a minute per key.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from tell.models import ApiKey, ApiUsage

__all__ = [
    "DEFAULT_DAILY_QUOTA",
    "DISPLAY_PREFIX_LENGTH",
    "KEY_BYTES",
    "KEY_PREFIX",
    "LAST_USED_THROTTLE",
    "NewKey",
    "authenticate",
    "create_key",
    "generate_key",
    "hash_key",
    "key_prefix",
    "meter",
    "parse_bearer",
    "revoke_key",
    "touch",
    "usage_rows",
    "validate_owner",
]

KEY_PREFIX = "tell_"
KEY_BYTES = 32
DISPLAY_PREFIX_LENGTH = 12
DEFAULT_DAILY_QUOTA = 1000
LAST_USED_THROTTLE = timedelta(minutes=1)

_KEY_PATTERN = re.compile(r"^tell_[A-Za-z0-9_-]{43}$")
_OWNER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UNAUTHORIZED = "a valid API key is required: send 'Authorization: Bearer tell_...'"


# --- keys -------------------------------------------------------------------------------------


def generate_key() -> str:
    """A new raw key: ``tell_`` + base64url(32 random bytes), no padding."""
    raw = base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
    return KEY_PREFIX + raw.rstrip("=")


def hash_key(key: str) -> str:
    """``sha256(key)`` as lowercase hex: the only form of a key that is ever stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """The first 12 characters of a key, kept for display."""
    return key[:DISPLAY_PREFIX_LENGTH]


def validate_owner(owner: str) -> str:
    """``owner`` normalized to lowercase; ``ValueError`` unless it is a plain handle.

    Handles are 1-64 characters of ``[a-z0-9._-]`` starting with a letter or digit, so
    an email address (``@``) or a display name (spaces) is refused.
    """
    value = owner.strip().lower()
    if not _OWNER_PATTERN.fullmatch(value):
        msg = (
            f"owner must be a handle (1-64 of a-z, 0-9, '.', '_', '-'), never an email; "
            f"got {owner!r}"
        )
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class NewKey:
    """A freshly created key; ``key`` is the only copy of the raw secret."""

    id: int
    key: str
    key_prefix: str
    owner: str
    daily_quota: int


def create_key(session: Session, owner: str, daily_quota: int = DEFAULT_DAILY_QUOTA) -> NewKey:
    """Insert a key for ``owner`` (hash and prefix only); flushes, never commits."""
    handle = validate_owner(owner)
    if daily_quota < 0:
        msg = f"daily_quota must be >= 0, got {daily_quota}"
        raise ValueError(msg)
    key = generate_key()
    row = ApiKey(
        key_hash=hash_key(key),
        key_prefix=key_prefix(key),
        owner=handle,
        daily_quota=daily_quota,
    )
    session.add(row)
    session.flush()
    return NewKey(
        id=row.id, key=key, key_prefix=row.key_prefix, owner=handle, daily_quota=daily_quota
    )


def revoke_key(session: Session, ref: str, now: datetime) -> ApiKey:
    """Revoke the key whose id or display prefix is ``ref``; returns it.

    ``LookupError`` when nothing matches or a prefix matches more than one key.
    Revoking an already revoked key keeps its first ``revoked_at``. Flushes only.
    """
    text = ref.strip()
    if text.isdigit():
        rows = list(session.scalars(select(ApiKey).where(ApiKey.id == int(text))))
    else:
        rows = list(session.scalars(select(ApiKey).where(ApiKey.key_prefix == text)))
    if not rows:
        msg = f"no API key with id or prefix {ref!r}"
        raise LookupError(msg)
    if len(rows) > 1:
        msg = f"prefix {ref!r} matches {len(rows)} keys; revoke by id"
        raise LookupError(msg)
    row = rows[0]
    if row.revoked_at is None:
        row.revoked_at = now
    session.flush()
    return row


# --- authentication -----------------------------------------------------------------------------


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def parse_bearer(authorization: str | None) -> str | None:
    """The key in ``Bearer <key>`` (scheme case-insensitive), or ``None`` if malformed."""
    if not authorization:
        return None
    scheme, _, credentials = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    key = credentials.strip()
    return key if _KEY_PATTERN.fullmatch(key) else None


def authenticate(session: Session, authorization: str | None) -> ApiKey:
    """The live :class:`ApiKey` behind an ``Authorization`` header, else HTTP 401."""
    key = parse_bearer(authorization)
    if key is None:
        raise _unauthorized()
    row = session.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(key))).one_or_none()
    if row is None or row.revoked_at is not None:
        raise _unauthorized()
    return row


# --- metering -------------------------------------------------------------------------------


def _seconds_to_midnight(now: datetime) -> int:
    tomorrow = datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(), UTC)
    return max(1, int((tomorrow - now).total_seconds()))


def meter(session: Session, key: ApiKey, now: datetime) -> int:
    """Count one call for ``key`` today (UTC); HTTP 429 once ``daily_quota`` is spent.

    Returns today's call count including this one. Atomic under concurrency: the
    increment and the quota check are one ``INSERT ... ON CONFLICT DO UPDATE ... WHERE``.
    Flushes; the caller commits.
    """
    day = now.astimezone(UTC).date()
    calls: int | None = None
    if key.daily_quota > 0:
        insert = pg_insert(ApiUsage).values(key_id=key.id, day=day, calls=1)
        upsert = insert.on_conflict_do_update(
            index_elements=[ApiUsage.key_id, ApiUsage.day],
            set_={"calls": ApiUsage.calls + 1},
            where=ApiUsage.calls < key.daily_quota,
        ).returning(ApiUsage.calls)
        calls = session.execute(upsert).scalar_one_or_none()
    if calls is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"daily quota of {key.daily_quota} calls used up; resets at 00:00 UTC",
            headers={"Retry-After": str(_seconds_to_midnight(now))},
        )
    touch(key, now)
    session.flush()
    return int(calls)


def touch(key: ApiKey, now: datetime) -> bool:
    """Set ``last_used_at`` unless it was written less than a minute ago; True if set."""
    last = key.last_used_at
    if last is not None and now - last < LAST_USED_THROTTLE:
        return False
    key.last_used_at = now
    return True


# --- reporting -------------------------------------------------------------------------------


def usage_rows(session: Session, since: date, owner: str | None = None) -> list[dict[str, Any]]:
    """Calls per key and day since ``since`` (inclusive), newest day first.

    ``[{key_id, key_prefix, owner, day, calls, daily_quota, revoked}]``; never the hash.
    """
    stmt = (
        select(
            ApiKey.id,
            ApiKey.key_prefix,
            ApiKey.owner,
            ApiUsage.day,
            ApiUsage.calls,
            ApiKey.daily_quota,
            ApiKey.revoked_at,
        )
        .join(ApiUsage, ApiUsage.key_id == ApiKey.id)
        .where(ApiUsage.day >= since)
        .order_by(ApiUsage.day.desc(), ApiKey.id)
    )
    if owner is not None:
        stmt = stmt.where(func.lower(ApiKey.owner) == owner.strip().lower())
    return [
        {
            "key_id": int(key_id),
            "key_prefix": prefix,
            "owner": key_owner,
            "day": day.isoformat(),
            "calls": int(calls),
            "daily_quota": int(quota),
            "revoked": revoked_at is not None,
        }
        for key_id, prefix, key_owner, day, calls, quota, revoked_at in session.execute(stmt)
    ]
