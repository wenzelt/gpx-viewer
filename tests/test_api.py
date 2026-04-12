from __future__ import annotations

import asyncio
import io
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


def _make_point(lon: float, lat: float):
    """Minimal stand-in for a gpxpy TrackPoint."""
    return SimpleNamespace(longitude=lon, latitude=lat)


# ── _downsample_points ────────────────────────────────────────────────────────

def test_downsample_empty_list_returns_empty():
    assert _downsample_points([]) == []


def test_downsample_single_point_returned_unchanged():
    pt = _make_point(0, 0)
    assert _downsample_points([pt]) == [pt]


def test_downsample_keeps_ten_percent_deterministically():
    pts = [_make_point(float(i), 0.0) for i in range(100)]
    result = _downsample_points(pts, sample_percent=10)
    assert result == [
        pts[0],
        pts[1],
        pts[13],
        pts[25],
        pts[37],
        pts[50],
        pts[62],
        pts[74],
        pts[86],
        pts[99],
    ]


def test_downsample_preserves_endpoints():
    pts = [_make_point(float(i), 0.0) for i in range(25)]
    result = _downsample_points(pts, sample_percent=10)
    assert result[0] is pts[0]
    assert result[-1] is pts[-1]


def test_downsample_small_track_returns_endpoints_when_ten_percent_is_too_small():
    pts = [_make_point(float(i), 0.0) for i in range(5)]
    result = _downsample_points(pts, sample_percent=10)
    assert result == [pts[0], pts[-1]]


def test_downsample_preserves_two_point_track():
    pts = [_make_point(0, 0), _make_point(1, 1)]
    result = _downsample_points(pts, sample_percent=10)
    assert result[0] is pts[0]
    assert result[-1] is pts[-1]


def test_downsample_clamps_sampling_percent():
    pts = [_make_point(float(i), 0.0) for i in range(20)]
    assert _downsample_points(pts, sample_percent=0) == [pts[0], pts[-1]]
    assert _downsample_points(pts, sample_percent=200) == pts


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
