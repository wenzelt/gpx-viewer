from __future__ import annotations

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from models import Base

DB_DSN = os.environ.get("DB_DSN")
if not DB_DSN:
    raise RuntimeError("DB_DSN environment variable is required")

engine_kwargs: dict[str, object] = {"echo": False, "future": True}

if DB_DSN.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    if ":memory:" in DB_DSN:
        engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["poolclass"] = NullPool

engine = create_engine(DB_DSN, **engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def _migrate_add_stats_columns(eng=None) -> None:
    """Add total_distance_m and total_elevation_gain_m to an existing tracks table.

    create_all() only creates missing tables; it does not alter existing ones.
    This migration is safe to call repeatedly (idempotent).
    """
    target = eng if eng is not None else engine
    with target.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(
                text("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS total_distance_m FLOAT")
            )
            conn.execute(
                text(
                    "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS total_elevation_gain_m FLOAT"
                )
            )
        elif conn.dialect.name == "sqlite":
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(tracks)"))}
            if "total_distance_m" not in existing:
                conn.execute(text("ALTER TABLE tracks ADD COLUMN total_distance_m FLOAT"))
            if "total_elevation_gain_m" not in existing:
                conn.execute(
                    text("ALTER TABLE tracks ADD COLUMN total_elevation_gain_m FLOAT")
                )


def init_db() -> None:
    # Ensure PostGIS extension exists when running against PostgreSQL
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.create_all(bind=engine)
    _migrate_add_stats_columns()
