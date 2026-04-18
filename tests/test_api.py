from __future__ import annotations

import asyncio
import io
import hashlib
from types import SimpleNamespace
from typing import List, Any

from fastapi import UploadFile, Response
from starlette.requests import Request

import main
from main import UploadOutcome, delete_all_tracks, get_tracks, health, upload_gpx
from models import User

TEST_USER_ID = hashlib.sha256(b"test-seed-phrase").hexdigest()

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

    def get(self, model, pk):
        if model == User and pk == TEST_USER_ID:
            return User(id=TEST_USER_ID)
        return None

    def refresh(self, obj):
        pass

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

def _mock_request(path="/", method="GET"):
    return Request({
        "type": "http",
        "path": path,
        "method": method,
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })


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

    async def fake_process(file, db, seen, user_id):
        return outcomes.pop(0)

    monkeypatch.setattr(main, "_process_upload_file", fake_process)

    invalidated = []

    def fake_invalidate(user_id):
        invalidated.append(True)

    monkeypatch.setattr(main.tracks_cache, "invalidate", fake_invalidate)

    req = _mock_request("/upload", "POST")
    result = asyncio.run(upload_gpx(request=req, files=[_upload_file("one.gpx"), _upload_file("two.gpx")], user_id=TEST_USER_ID))
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

    async def fake_process(file, db, seen, user_id):
        return UploadOutcome(file.filename, "ok")

    monkeypatch.setattr(main, "_process_upload_file", fake_process)

    req = _mock_request("/upload", "POST")
    result = asyncio.run(upload_gpx(request=req, files=[_upload_file("dup.gpx")], user_id=TEST_USER_ID))
    entry = result["results"][0]
    assert entry["status"] == "skipped"
    assert "Duplicate" in entry["reason"]
    assert session.rollback_called


def test_get_tracks_uses_cache_headers(monkeypatch):
    serialized = b'{"type": "FeatureCollection", "features": []}'
    monkeypatch.setattr(main, "_build_tracks_serialized", lambda user_id, **kwargs: serialized)

    # Calculate expected etag for non-default query path
    etag = main.compute_sha256(serialized)

    # Use a non-default query to bypass tracks_cache version based ETag
    req = _mock_request("/tracks")
    response = get_tracks(req, limit=100, user_id=TEST_USER_ID)
    assert response.status_code == 200
    assert response.headers["ETag"] == etag

    req_304 = Request({
        "type": "http",
        "path": "/tracks",
        "method": "GET",
        "headers": [(b"if-none-match", etag.encode())],
        "client": ("127.0.0.1", 12345),
    })
    response_304 = get_tracks(req_304, limit=100, user_id=TEST_USER_ID)
    assert response_304.status_code == 304
    assert response_304.headers["ETag"] == etag


def test_delete_all_tracks_invalidates_cache(monkeypatch):
    session = DummySession(rowcount=3)
    session.execute = lambda stmt: SimpleNamespace(rowcount=3)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    invalidated = []

    def fake_invalidate(user_id):
        invalidated.append(True)

    monkeypatch.setattr(main.tracks_cache, "invalidate", fake_invalidate)

    req = _mock_request("/delete_all", "DELETE")
    payload = delete_all_tracks(request=req, user_id=TEST_USER_ID)
    assert payload == {"status": "ok", "deleted": 3}
    assert session.committed
    assert invalidated
