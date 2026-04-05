from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import List

from fastapi import UploadFile
from starlette.requests import Request

import main
from main import UploadOutcome, _downsample_points, delete_all_tracks, get_tracks, health, upload_gpx


class DummySession:
    def __init__(self, outcomes: List[UploadOutcome] | None = None, rowcount: int = 0):
        self.added: list = []
        self.outcomes = outcomes or []
        self.rowcount = rowcount
        self.committed = False
        self.rollback_called = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def add(self, obj):
        self.added.append(obj)

    def execute(self, _stmt):
        return SimpleNamespace(rowcount=self.rowcount, scalars=lambda: iter(()))

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rollback_called = True


def _upload_file(name: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(b"<gpx></gpx>"))


def _make_point(lon: float, lat: float, time: datetime | None = None):
    """Minimal stand-in for a gpxpy TrackPoint."""
    return SimpleNamespace(longitude=lon, latitude=lat, time=time)


def _ts(seconds_offset: int) -> datetime:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds_offset)


# ── _downsample_points ────────────────────────────────────────────────────────

def test_downsample_empty_list_returns_empty():
    assert _downsample_points([]) == []


def test_downsample_single_point_returned_unchanged():
    pt = _make_point(0, 0, _ts(0))
    assert _downsample_points([pt]) == [pt]


def test_downsample_time_based_keeps_first_last_and_interval():
    # 25 points at 1-second intervals → with interval=10 should keep 0,10,20,24
    pts = [_make_point(float(i), 0.0, _ts(i)) for i in range(25)]
    result = _downsample_points(pts, interval_seconds=10)
    kept_times = [p.time for p in result]
    assert kept_times[0] == _ts(0)
    assert kept_times[-1] == _ts(24)
    # point at t=10 and t=20 should be included
    assert _ts(10) in kept_times
    assert _ts(20) in kept_times
    # total points should be much fewer than 25
    assert len(result) <= 5


def test_downsample_time_based_no_skipped_if_already_sparse():
    # Points already 15s apart — all should be kept
    pts = [_make_point(float(i), 0.0, _ts(i * 15)) for i in range(5)]
    result = _downsample_points(pts, interval_seconds=10)
    assert result == pts


def test_downsample_fallback_index_based_when_no_timestamps():
    pts = [_make_point(float(i), 0.0, None) for i in range(25)]
    result = _downsample_points(pts, interval_seconds=10)
    # Should include index 0, 10, 20, and last (24)
    assert pts[0] in result
    assert pts[10] in result
    assert pts[20] in result
    assert pts[24] in result
    assert len(result) <= 5


def test_downsample_preserves_two_point_track():
    pts = [_make_point(0, 0, _ts(0)), _make_point(1, 1, _ts(1))]
    result = _downsample_points(pts, interval_seconds=10)
    assert result[0] is pts[0]
    assert result[-1] is pts[-1]


# ── existing API tests ────────────────────────────────────────────────────────

def test_health_endpoint_reports_status() -> None:
    payload = health()
    assert payload["status"] == "ok"
    assert "time_utc" in payload


def test_upload_gpx_success_invalidate_cache(monkeypatch):
    session = DummySession()
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    outcomes = [
        UploadOutcome("one.gpx", "ok"),
        UploadOutcome("two.gpx", "skipped", "Duplicate"),
    ]

    async def fake_process(file, db, seen):
        return outcomes.pop(0)

    monkeypatch.setattr(main, "_process_upload_file", fake_process)

    invalidated = []

    def fake_invalidate():
        invalidated.append(True)

    monkeypatch.setattr(main.tracks_cache, "invalidate", fake_invalidate)

    result = asyncio.run(upload_gpx(files=[_upload_file("one.gpx"), _upload_file("two.gpx")]))
    assert result["results"][0]["status"] == "ok"
    assert result["results"][1]["status"] == "skipped"
    assert session.committed
    assert invalidated, "tracks cache should invalidate when new data arrives"


def test_upload_gpx_integrity_error_converts_to_skip(monkeypatch):
    class IntegritySession(DummySession):
        def commit(self):
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("", {}, None)

    session = IntegritySession()
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    async def fake_process(file, db, seen):
        return UploadOutcome(file.filename, "ok")

    monkeypatch.setattr(main, "_process_upload_file", fake_process)

    result = asyncio.run(upload_gpx(files=[_upload_file("dup.gpx")]))
    entry = result["results"][0]
    assert entry["status"] == "skipped"
    assert "Duplicate" in entry["reason"]
    assert session.rollback_called


def test_get_tracks_uses_cache_headers(monkeypatch):
    payload = {"type": "FeatureCollection", "features": []}
    etag = "abc123"
    monkeypatch.setattr(main.tracks_cache, "get_payload", lambda loader: (payload, etag))

    response = get_tracks(Request({"type": "http", "headers": []}))
    assert response.status_code == 200
    assert response.headers["ETag"] == etag

    response_304 = get_tracks(Request({"type": "http", "headers": [(b"if-none-match", etag.encode())]}))
    assert response_304.status_code == 304
    assert response_304.headers["ETag"] == etag


def test_delete_all_tracks_invalidates_cache(monkeypatch):
    session = DummySession(rowcount=3)
    session.execute = lambda stmt: SimpleNamespace(rowcount=3)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    invalidated = []

    def fake_invalidate():
        invalidated.append(True)

    monkeypatch.setattr(main.tracks_cache, "invalidate", fake_invalidate)

    payload = delete_all_tracks()
    assert payload == {"status": "ok", "deleted": 3}
    assert session.committed
    assert invalidated
