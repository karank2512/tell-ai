from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from tell import db as tell_db
from tell.db import SessionLocal, claim_rows, make_engine, session_scope, upsert

metadata = MetaData()

items = Table(
    "p0_items",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
    Column("version", Integer, nullable=False, default=1),
)

queue = Table(
    "p0_queue",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("status", String, nullable=False, server_default="open"),
)


class Base(DeclarativeBase):
    pass


class Alias(Base):
    __tablename__ = "p0_aliases"

    alias: Mapped[str] = mapped_column(String, primary_key=True)
    company: Mapped[str] = mapped_column(String, nullable=False)


@pytest.fixture(scope="module", autouse=True)
def schema(engine: Engine) -> Iterator[None]:
    metadata.create_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def clean_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE p0_items, p0_queue, p0_aliases"))


def test_test_database_is_temporary(test_database_url: str, engine: Engine) -> None:
    assert "tell_test_" in test_database_url
    with engine.connect() as conn:
        name = conn.execute(text("SELECT current_database()")).scalar_one()
    assert name.startswith("tell_test_")
    assert len(name) == len("tell_test_") + 8


def test_make_engine_sets_pool_pre_ping(test_database_url: str) -> None:
    eng = make_engine(test_database_url)
    try:
        assert eng.pool._pre_ping is True
        assert eng.dialect.name == "postgresql"
        assert eng.dialect.driver == "psycopg"
    finally:
        eng.dispose()


def test_get_engine_is_cached(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> None:
    tell_db.get_engine.cache_clear()
    monkeypatch.setattr(tell_db, "make_engine", lambda: make_engine(test_database_url))
    try:
        assert tell_db.get_engine() is tell_db.get_engine()
    finally:
        tell_db.get_engine().dispose()
        tell_db.get_engine.cache_clear()


def test_session_scope_commits(engine: Engine) -> None:
    with session_scope(engine) as session:
        session.execute(items.insert().values(key="a", value="1"))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(items)).scalar_one() == 1


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="boom"), session_scope(engine) as session:
        session.execute(items.insert().values(key="a", value="1"))
        raise RuntimeError("boom")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(items)).scalar_one() == 0


def test_upsert_do_nothing_is_idempotent(db_session: Session) -> None:
    rows = [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}]
    assert upsert(db_session, items, rows, conflict_columns=["key"]) == 2
    assert upsert(db_session, items, rows, conflict_columns=["key"]) == 0
    assert (
        upsert(db_session, items, {"key": "a", "value": "changed"}, conflict_columns=["key"]) == 0
    )
    assert upsert(db_session, items, [], conflict_columns=["key"]) == 0
    stored = dict(db_session.execute(select(items.c.key, items.c.value)).all())
    assert stored == {"a": "1", "b": "2"}


def test_upsert_do_update_sets_only_named_columns(db_session: Session) -> None:
    upsert(db_session, items, {"key": "a", "value": "1", "version": 1}, conflict_columns=["key"])
    n = upsert(
        db_session,
        items,
        {"key": "a", "value": "2", "version": 99},
        conflict_columns=[items.c.key],
        update_columns=["value"],
    )
    assert n == 1
    row = db_session.execute(select(items.c.value, items.c.version)).one()
    assert tuple(row) == ("2", 1)


def test_upsert_do_update_with_where_guard(db_session: Session) -> None:
    upsert(db_session, items, {"key": "a", "value": "1", "version": 5}, conflict_columns=["key"])
    n = upsert(
        db_session,
        items,
        {"key": "a", "value": "stale", "version": 3},
        conflict_columns=["key"],
        update_columns=["value", "version"],
        update_where=items.c.version < 3,
    )
    assert n == 0
    assert db_session.execute(select(items.c.value)).scalar_one() == "1"


def test_upsert_accepts_orm_models(db_session: Session) -> None:
    assert upsert(db_session, Alias, {"alias": "acme", "company": "x"}, conflict_columns=["alias"])
    assert upsert(
        db_session,
        Alias,
        {"alias": "acme", "company": "y"},
        conflict_columns=[Alias.__table__.c.alias],
        update_columns=["company"],
    )
    assert db_session.get(Alias, "acme") is not None
    assert db_session.execute(select(Alias.company)).scalar_one() == "y"


def test_upsert_validates_columns(db_session: Session) -> None:
    with pytest.raises(KeyError, match="nope"):
        upsert(db_session, items, {"key": "a", "value": "1"}, conflict_columns=["nope"])
    with pytest.raises(ValueError, match="update_columns"):
        upsert(
            db_session,
            items,
            {"key": "a", "value": "1"},
            conflict_columns=["key"],
            update_columns=[],
        )


def test_table_of_rejects_unmapped() -> None:
    class NotMapped:
        __table__ = "not a table"

    with pytest.raises(TypeError, match="not mapped"):
        tell_db._table_of(NotMapped)  # type: ignore[arg-type]  # deliberately wrong type


def test_claim_rows_skip_locked(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(queue.insert(), [{"id": i} for i in range(1, 6)])

    first = SessionLocal(bind=engine)
    second = SessionLocal(bind=engine)
    try:
        claimed_first = claim_rows(first, queue, 2, order_by=[queue.c.id])
        assert [r.id for r in claimed_first] == [1, 2]

        # A second transaction must skip rows locked by the first one.
        claimed_second = claim_rows(second, queue, 10, order_by=[queue.c.id])
        assert [r.id for r in claimed_second] == [3, 4, 5]

        # Nothing left for a third worker while both hold their locks.
        third = SessionLocal(bind=engine)
        try:
            assert claim_rows(third, queue, 10) == []
        finally:
            third.rollback()
            third.close()

        first.execute(queue.update().where(queue.c.id.in_([1, 2])).values(status="done"))
        first.commit()
    finally:
        first.rollback()
        first.close()
        second.rollback()
        second.close()

    with engine.connect() as conn:
        done = conn.execute(select(queue.c.id).where(queue.c.status == "done")).scalars().all()
    assert sorted(done) == [1, 2]


def test_claim_rows_where_and_zero(db_session: Session) -> None:
    db_session.execute(
        queue.insert(),
        [{"id": 1, "status": "done"}, {"id": 2, "status": "open"}, {"id": 3, "status": "open"}],
    )
    rows = claim_rows(db_session, queue, 5, where=queue.c.status == "open", order_by=[queue.c.id])
    assert [r.id for r in rows] == [2, 3]
    assert claim_rows(db_session, queue, 0) == []


def test_claim_rows_does_not_block_concurrent_worker(engine: Engine) -> None:
    """A blocked worker would hang; SKIP LOCKED returns immediately with the rest."""
    with engine.begin() as conn:
        conn.execute(queue.insert(), [{"id": i} for i in range(1, 4)])
    holder = SessionLocal(bind=engine)
    result: list[int] = []
    try:
        claim_rows(holder, queue, 1, order_by=[queue.c.id])

        def worker() -> None:
            with session_scope(engine) as s:
                result.extend(r.id for r in claim_rows(s, queue, 10, order_by=[queue.c.id]))

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "claim_rows blocked on a locked row"
    finally:
        holder.rollback()
        holder.close()
    assert result == [2, 3]


def test_transactions_lift_the_idle_in_transaction_timeout(test_database_url: str) -> None:
    """Neon kills connections idle in a transaction for 5 minutes; every tell transaction
    lifts that for itself with SET LOCAL, and the server default returns after commit."""
    engine = make_engine(
        test_database_url,
        connect_args={"options": "-c idle_in_transaction_session_timeout=300000"},
    )
    try:
        with session_scope(engine) as session:
            inside = session.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
        with engine.connect() as conn:
            after = conn.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
    finally:
        engine.dispose()
    assert inside == "0"
    assert after == "5min"
