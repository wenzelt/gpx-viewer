"""Tests for DB migrations.

Covers:
- _migrate_add_stats_columns: adds total_distance_m / total_elevation_gain_m
- _migrate_add_user_isolation: adds user_id column and backfills legacy rows
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from db import _migrate_add_stats_columns, _migrate_add_user_isolation


OLD_TRACKS_DDL = """
CREATE TABLE tracks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    tag      TEXT,
    hash     TEXT NOT NULL,
    name     TEXT,
    created_at DATETIME NOT NULL,
    geom     TEXT
);
"""


def _make_engine():
    """Return a fresh in-memory SQLite engine isolated per test."""
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _column_names(conn) -> set[str]:
    return {row[1] for row in conn.execute(text("PRAGMA table_info(tracks)"))}


class TestMigrateAddStatsColumns:
    def test_adds_total_distance_m_to_old_table(self) -> None:
        eng = _make_engine()
        with eng.begin() as conn:
            conn.execute(text(OLD_TRACKS_DDL))
            assert "total_distance_m" not in _column_names(conn)

        _migrate_add_stats_columns(eng)

        with eng.connect() as conn:
            assert "total_distance_m" in _column_names(conn)

    def test_adds_total_elevation_gain_m_to_old_table(self) -> None:
        eng = _make_engine()
        with eng.begin() as conn:
            conn.execute(text(OLD_TRACKS_DDL))
            assert "total_elevation_gain_m" not in _column_names(conn)

        _migrate_add_stats_columns(eng)

        with eng.connect() as conn:
            assert "total_elevation_gain_m" in _column_names(conn)

    def test_idempotent_on_new_table(self) -> None:
        """Running migration on a table that already has the columns is a no-op."""
        eng = _make_engine()
        with eng.begin() as conn:
            conn.execute(text(OLD_TRACKS_DDL))
            conn.execute(text("ALTER TABLE tracks ADD COLUMN total_distance_m FLOAT"))
            conn.execute(text("ALTER TABLE tracks ADD COLUMN total_elevation_gain_m FLOAT"))

        # Should not raise even though columns already exist
        _migrate_add_stats_columns(eng)

        with eng.connect() as conn:
            cols = _column_names(conn)
            assert "total_distance_m" in cols
            assert "total_elevation_gain_m" in cols

    def test_new_columns_default_to_null(self) -> None:
        """Rows inserted before migration exist with NULL for both stats columns."""
        eng = _make_engine()
        with eng.begin() as conn:
            conn.execute(text(OLD_TRACKS_DDL))
            conn.execute(
                text("INSERT INTO tracks (hash, created_at) VALUES ('abc123', '2024-01-01')")
            )

        _migrate_add_stats_columns(eng)

        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT total_distance_m, total_elevation_gain_m FROM tracks WHERE hash='abc123'")
            ).fetchone()
            assert row is not None
            assert row[0] is None
            assert row[1] is None

    def test_migrated_columns_accept_float_values(self) -> None:
        """After migration, stats columns can store float values."""
        eng = _make_engine()
        with eng.begin() as conn:
            conn.execute(text(OLD_TRACKS_DDL))

        _migrate_add_stats_columns(eng)

        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tracks (hash, created_at, total_distance_m, total_elevation_gain_m) "
                    "VALUES ('xyz', '2024-06-01', 12345.6, 789.0)"
                )
            )
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT total_distance_m, total_elevation_gain_m FROM tracks WHERE hash='xyz'")
            ).fetchone()
            assert row[0] == pytest.approx(12345.6)
            assert row[1] == pytest.approx(789.0)


# ── _migrate_add_user_isolation (SQLite) ─────────────────────────────────────

OLD_TRACKS_WITH_STATS_DDL = """
CREATE TABLE tracks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    tag      TEXT,
    hash     TEXT NOT NULL,
    name     TEXT,
    created_at DATETIME NOT NULL,
    geom     TEXT,
    total_distance_m FLOAT,
    total_elevation_gain_m FLOAT
);
"""

USERS_DDL = """
CREATE TABLE users (
    id         TEXT PRIMARY KEY,
    created_at DATETIME NOT NULL
);
"""


def _make_engine_with_users():
    """Fresh in-memory SQLite engine with both users and tracks tables."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with eng.begin() as conn:
        conn.execute(text(USERS_DDL))
        conn.execute(text(OLD_TRACKS_WITH_STATS_DDL))
    return eng


class TestMigrateAddUserIsolation:
    def test_adds_user_id_column(self) -> None:
        eng = _make_engine_with_users()
        with eng.connect() as conn:
            cols = _column_names(conn)
            assert "user_id" not in cols

        _migrate_add_user_isolation(eng)

        with eng.connect() as conn:
            assert "user_id" in _column_names(conn)

    def test_backfills_existing_rows_with_legacy_user(self) -> None:
        eng = _make_engine_with_users()
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO tracks (hash, created_at) VALUES ('abc', '2024-01-01')"))

        _migrate_add_user_isolation(eng)

        with eng.connect() as conn:
            row = conn.execute(text("SELECT user_id FROM tracks WHERE hash='abc'")).fetchone()
            assert row is not None
            assert row[0] == "legacy-vault"

    def test_legacy_user_created_in_users_table(self) -> None:
        eng = _make_engine_with_users()
        _migrate_add_user_isolation(eng)

        with eng.connect() as conn:
            row = conn.execute(text("SELECT id FROM users WHERE id='legacy-vault'")).fetchone()
            assert row is not None

    def test_idempotent_when_column_already_exists(self) -> None:
        eng = _make_engine_with_users()
        # Pre-add the column so migration sees it already present
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE tracks ADD COLUMN user_id VARCHAR"))
        # Should not raise
        _migrate_add_user_isolation(eng)
        with eng.connect() as conn:
            assert "user_id" in _column_names(conn)

    def test_rows_added_after_migration_store_user_id(self) -> None:
        eng = _make_engine_with_users()
        _migrate_add_user_isolation(eng)

        with eng.begin() as conn:
            conn.execute(
                text("INSERT INTO tracks (hash, created_at, user_id) VALUES ('new', '2024-06-01', 'user-123')")
            )
        with eng.connect() as conn:
            row = conn.execute(text("SELECT user_id FROM tracks WHERE hash='new'")).fetchone()
            assert row[0] == "user-123"
