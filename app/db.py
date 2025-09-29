from __future__ import annotations

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from models import Base

DB_DSN = os.environ.get("DB_DSN")
if not DB_DSN:
    raise RuntimeError("DB_DSN environment variable is required")

# NullPool keeps containers simple and avoids stale connections on restarts
engine = create_engine(DB_DSN, echo=False, poolclass=NullPool, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def init_db() -> None:
    # Ensure PostGIS extension exists, then create tables
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.create_all(bind=engine)
