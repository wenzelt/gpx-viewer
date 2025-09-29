import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from models import Base

DB_DSN = os.environ.get("DB_DSN")

# Disable pooling to keep it simple in small containers
engine = create_engine(DB_DSN, echo=False, poolclass=NullPool, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def init_db():
    from sqlalchemy import text
    # Ensure PostGIS extension exists, then create tables
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.create_all(bind=engine)
