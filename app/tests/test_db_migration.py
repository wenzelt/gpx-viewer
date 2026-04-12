"""Tests for the DB migration that adds stats columns to the tracks table.

Scenario: a pre-existing tracks table (created before the stats feature) must
gain the two new columns when init_db / _migrate_add_stats_columns runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from db import _migrate_add_stats_columns


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
