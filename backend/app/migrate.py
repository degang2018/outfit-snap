"""Additive auto-migration.

`Base.metadata.create_all()` only creates MISSING TABLES - it never touches an existing
table.  That is why schema changes used to be applied by deleting the database file, which
threw away real user data.

This module closes that gap for the common case: it compares the models against the live
database and issues `ALTER TABLE ... ADD COLUMN` for anything missing, then backfills the
model's simple default.  Works on both SQLite and MySQL.

Scope: ADD COLUMN only.  Renames, drops, type changes and data backfills still need a real
migration (Alembic) - this just keeps development from destroying data.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .db import Base

log = logging.getLogger("app.migrate")


def _simple_default(column):
    """Return the model's scalar default (None for callables such as utcnow)."""
    default = column.default
    if default is None or default.is_callable or default.is_scalar is False:
        return None
    return default.arg


def ensure_schema(engine: Engine) -> list[str]:
    """Add every column the models declare but the database is missing. Returns what it added."""
    added: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue                                    # create_all already made it
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            ddl_type = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"))
                default = _simple_default(column)
                if default is not None:
                    value = json.dumps(default) if isinstance(default, (dict, list)) else default
                    conn.execute(
                        text(f"UPDATE {table.name} SET {column.name} = :v WHERE {column.name} IS NULL"),
                        {"v": value},
                    )
            present.add(column.name)
            added.append(f"{table.name}.{column.name}")
            log.warning("schema: added missing column %s.%s (%s)", table.name, column.name, ddl_type)

    return added


def check_integrity(engine: Engine) -> list[str]:
    """体检:表丢主键时插入数据后再也读不回来(SQLAlchemy 需要主键才能回查),
    这种问题很隐蔽,所以启动时直接大声报出来,而不是等某个接口 500。"""
    broken: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        pk = inspector.get_pk_constraint(table.name).get("constrained_columns") or []
        if not pk:
            broken.append(table.name)
            log.error(
                "schema: 表 %s 没有主键!对它写入后无法回读(接口会 500),需要重建该表", table.name
            )
    return broken
