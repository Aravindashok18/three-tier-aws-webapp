"""Data access: the schema and every SQL statement the app runs."""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, create_engine, delete, func, insert, select, text,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.schema import CreateTable

metadata = MetaData()

notes = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("author", String(60), nullable=False),
    Column("body", String(500), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

MAX_BODY = 500
MAX_AUTHOR = 60


def make_engine(url: URL, ssl_ca: Optional[str] = None) -> Engine:
    """Build the connection pool. For MySQL with an `ssl_ca`, the server certificate and host name are verified."""
    connect_args = {}
    if ssl_ca and url.get_backend_name() == "mysql":
        connect_args["ssl"] = {"ca": ssl_ca}
    # pool_pre_ping drops connections RDS closed during a failover or maintenance window
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, future=True, connect_args=connect_args)


def init_schema(engine: Engine, attempts: int = 1, delay: float = 3.0) -> None:
    """Create the tables if they are missing.

    Safe to run from several instances at once: it issues CREATE TABLE IF NOT
    EXISTS rather than checking first. With attempts > 1 it waits for a database
    that is still starting.
    """
    for attempt in range(1, attempts + 1):
        try:
            with engine.begin() as conn:
                for table in metadata.sorted_tables:
                    conn.execute(CreateTable(table, if_not_exists=True))
            return
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(delay)


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def list_notes(engine: Engine, limit: int = 100) -> list[dict]:
    query = select(notes).order_by(notes.c.id.desc()).limit(limit)
    with engine.connect() as conn:
        return [_row(r) for r in conn.execute(query)]


def add_note(engine: Engine, author: str, body: str) -> dict:
    with engine.begin() as conn:
        new_id = conn.execute(insert(notes).values(author=author, body=body)).inserted_primary_key[0]
        return _row(conn.execute(select(notes).where(notes.c.id == new_id)).one())


def delete_note(engine: Engine, note_id: int) -> bool:
    with engine.begin() as conn:
        return conn.execute(delete(notes).where(notes.c.id == note_id)).rowcount > 0


def count_notes(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(notes)).scalar_one()


def _row(row) -> dict:
    created: Optional[object] = row.created_at
    return {
        "id": row.id,
        "author": row.author,
        "body": row.body,
        "created_at": created.isoformat(sep=" ", timespec="seconds") if created else None,
    }
