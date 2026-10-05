"""Database plumbing: engine, sessions, upsert and ``SKIP LOCKED`` claim helpers.

Models live in ``tell/models.py`` (Phase 1); this module is schema-agnostic and
works with any :class:`sqlalchemy.Table` or declarative model.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Row, Table, create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement, KeyedColumnElement

from tell.config import get_settings

TableLike = Table | type[DeclarativeBase]
"""A core ``Table`` or an ORM mapped class; both are accepted by every helper here."""


def make_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Create an engine with ``pool_pre_ping`` so stale pooled connections are recycled."""
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("future", True)
    return create_engine(url or get_settings().database_url, **kwargs)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine for ``DATABASE_URL`` (created once, cached)."""
    return make_engine()


SessionLocal: sessionmaker[Session] = sessionmaker(
    class_=Session, expire_on_commit=False, autoflush=False
)
"""Unbound session factory; :func:`session_scope` binds it to an engine."""


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Yield a session; commit on success, roll back on any exception, always close."""
    session = SessionLocal(bind=engine or get_engine())
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _table_of(target: TableLike) -> Table:
    if isinstance(target, Table):
        return target
    table = target.__table__
    if not isinstance(table, Table):
        msg = f"{target!r} is not mapped to a single Table"
        raise TypeError(msg)
    return table


def _column_names(table: Table, columns: Iterable[str | KeyedColumnElement[Any]]) -> list[str]:
    names: list[str] = []
    for col in columns:
        name = col if isinstance(col, str) else col.name
        if name not in table.c:
            msg = f"column {name!r} is not on table {table.name!r}"
            raise KeyError(msg)
        names.append(name)
    return names


def upsert(
    session: Session,
    target: TableLike,
    rows: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    conflict_columns: Sequence[str | KeyedColumnElement[Any]],
    update_columns: Sequence[str | KeyedColumnElement[Any]] | None = None,
    update_where: ColumnElement[bool] | None = None,
) -> int:
    """``INSERT ... ON CONFLICT`` for a table and its natural key.

    With ``update_columns`` omitted this is ``ON CONFLICT DO NOTHING`` (idempotent
    insert); with it, ``ON CONFLICT DO UPDATE SET col = EXCLUDED.col`` for each named
    column (optionally guarded by ``update_where``). Returns the number of rows
    inserted or updated as reported by Postgres (conflicting no-op rows are not counted).
    """
    table = _table_of(target)
    values = [dict(rows)] if isinstance(rows, Mapping) else [dict(r) for r in rows]
    if not values:
        return 0
    index_elements = _column_names(table, conflict_columns)
    stmt = pg_insert(table).values(values)
    if update_columns is None:
        stmt = stmt.on_conflict_do_nothing(index_elements=index_elements)
    else:
        set_names = _column_names(table, update_columns)
        if not set_names:
            msg = "update_columns must not be empty; omit it for DO NOTHING"
            raise ValueError(msg)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={name: stmt.excluded[name] for name in set_names},
            where=update_where,
        )
    # RETURNING makes the count driver-independent: conflicting DO NOTHING rows return
    # nothing, updated rows return their key, so len() is exactly "inserted or updated".
    returning = stmt.returning(*(table.c[name] for name in index_elements))
    return len(session.execute(returning).all())


def claim_rows(
    session: Session,
    target: TableLike,
    n: int,
    *,
    where: ColumnElement[bool] | None = None,
    order_by: Sequence[ColumnElement[Any]] | None = None,
) -> Sequence[Row[Any]]:
    """``SELECT ... FOR UPDATE SKIP LOCKED LIMIT n``: claim rows for this transaction.

    Rows locked by a concurrent transaction are skipped, so several workers can
    drain one queue without coordination. The claim holds until the session
    commits or rolls back.
    """
    if n < 1:
        return []
    table = _table_of(target)
    stmt = select(table)
    if where is not None:
        stmt = stmt.where(where)
    if order_by is not None:
        stmt = stmt.order_by(*order_by)
    stmt = stmt.limit(n).with_for_update(skip_locked=True)
    return session.execute(stmt).all()
