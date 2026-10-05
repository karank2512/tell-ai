"""Alembic environment for tell.

Database URL resolution, first match wins:

1. ``alembic -x url=<sqlalchemy url> upgrade head`` (command line);
2. ``config.attributes["url"]`` (programmatic callers such as ``tests/conftest.py``);
3. ``sqlalchemy.url`` in the alembic config, when a caller set one;
4. ``DATABASE_URL`` from the environment or ``.env`` through :mod:`tell.config`.

A programmatic caller may also pass an open connection in
``config.attributes["connection"]`` to run migrations inside its own transaction.
Set ``config.attributes["skip_logging_config"] = True`` to leave the host process's
logging untouched (tests do this).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from tell.config import Settings
from tell.models import Base

config = context.config

if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def resolve_url() -> str:
    """Pick the database URL per the module docstring order."""
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("url")
    if url:
        return str(url)
    attr = config.attributes.get("url")
    if attr:
        return str(attr)
    main = config.get_main_option("sqlalchemy.url")
    if main:
        return main
    return Settings().database_url


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Only compare objects alembic itself did not create (the version table is implicit)."""
    _ = (obj, reflected, compare_to)
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection (``alembic upgrade --sql``)."""
    context.configure(
        url=resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection (own engine unless one is handed in)."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = resolve_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
