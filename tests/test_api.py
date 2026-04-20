from __future__ import annotations

import asyncio
import hashlib
import io
import os
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile, Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import main
from main import UploadOutcome, delete_all_tracks, delete_account, create_vault, get_tracks, get_user_id, health, upload_gpx, serve_impressum, serve_datenschutz, get_config
from models import User

# Compute TEST_USER_ID the same way get_user_id does, using the test pepper set in conftest.py
_TEST_SEED = "test-seed-phrase"
_TEST_PEPPER = os.environ["APP_SECRET_PEPPER"]
_hasher = hashlib.sha256()
_hasher.update(_TEST_SEED.strip().lower().encode())
_hasher.update(_TEST_PEPPER.encode())
TEST_USER_ID = _hasher.hexdigest()

class DummySession:
    def __init__(self, outcomes: list[UploadOutcome] | None = None, rowcount: int = 0):
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


# ── get_user_id unit tests ────────────────────────────────────────────────────

def _creds(seed: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=seed)


def test_get_user_id_returns_hex_digest() -> None:
    result = get_user_id(_creds("long-enough-seed-phrase"))
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_get_user_id_is_deterministic() -> None:
    assert get_user_id(_creds("my-seed-phrase-here")) == get_user_id(_creds("my-seed-phrase-here"))


def test_get_user_id_case_insensitive() -> None:
    assert get_user_id(_creds("My-Seed-Phrase-HERE")) == get_user_id(_creds("my-seed-phrase-here"))


def test_get_user_id_too_short_raises_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_user_id(_creds("short"))
    assert exc_info.value.status_code == 401


def test_get_user_id_exactly_min_length_accepted() -> None:
    seed = "a" * main.MIN_SEED_PHRASE_LEN
    result = get_user_id(_creds(seed))
    assert len(result) == 64


# ── delete_all_tracks ────────────────────────────────────────────────────────

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


# ── create_vault ─────────────────────────────────────────────────────────────

def test_create_vault_creates_new_user(monkeypatch):
    """When no user record exists, create_vault should insert one and return ok."""
    class NoUserSession(DummySession):
        def get(self, model, pk):
            return None  # user does not exist yet

    session = NoUserSession()
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    req = _mock_request("/auth/create", "POST")
    result = create_vault(request=req, user_id=TEST_USER_ID)

    assert result == {"status": "ok", "user_id": TEST_USER_ID}
    assert any(isinstance(obj, User) for obj in session.added), "a User should have been added"
    assert session.committed


def test_create_vault_is_idempotent(monkeypatch):
    """When user already exists, create_vault should not create a duplicate and still return ok."""
    session = DummySession()  # DummySession.get returns User for TEST_USER_ID

    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    req = _mock_request("/auth/create", "POST")
    result = create_vault(request=req, user_id=TEST_USER_ID)

    assert result == {"status": "ok", "user_id": TEST_USER_ID}
    assert not session.added, "no new User should be added when one already exists"


# ── delete_account ───────────────────────────────────────────────────────────

def test_delete_account_removes_existing_user_and_invalidates_cache(monkeypatch):
    """delete_account should delete the user record and invalidate the track cache."""
    deleted_objects = []

    class DeletableSession(DummySession):
        def delete(self, obj):
            deleted_objects.append(obj)

    session = DeletableSession()
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    invalidated = []
    monkeypatch.setattr(main.tracks_cache, "invalidate", lambda uid: invalidated.append(uid))

    req = _mock_request("/account", "DELETE")
    result = delete_account(request=req, user_id=TEST_USER_ID)

    assert result == {"status": "ok"}
    assert any(isinstance(obj, User) for obj in deleted_objects), "User should be deleted"
    assert session.committed
    assert TEST_USER_ID in invalidated


# ── legal pages ─────────────────────────────────────────────────────────────

# ── /api/config ──────────────────────────────────────────────────────────────

def test_get_config_returns_production_mode():
    """In the test suite (APP_ENV=production), config reports local_mode=False."""
    result = get_config()
    assert result["local_mode"] is False
    assert "max_files_per_request" in result
    assert result["max_files_per_request"] == 50


def test_get_config_local_mode(monkeypatch):
    """When LOCAL_MODE is patched to True, config reports local_mode=True."""
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    monkeypatch.setattr(main, "MAX_FILES_PER_REQUEST", 500)
    result = get_config()
    assert result["local_mode"] is True
    assert result["max_files_per_request"] == 500


def test_get_user_id_local_mode_returns_fixed_id(monkeypatch):
    """In local mode, get_user_id returns LOCAL_USER_ID without any credentials."""
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    result = get_user_id(None)
    assert result == main.LOCAL_USER_ID
    assert result == "local"


# ── legal pages ─────────────────────────────────────────────────────────────

def test_serve_impressum_returns_html_with_expected_content():
    """GET /impressum should return HTML containing key Impressum content."""
    response = serve_impressum()
    body = response.body.decode()
    assert "T Wenzel Consulting" in body
    assert "Impressum" in body
    assert "<!DOCTYPE html>" in body


def test_serve_datenschutz_returns_html_with_expected_content():
    """GET /datenschutz should return HTML containing key DSGVO content."""
    response = serve_datenschutz()
    body = response.body.decode()
    assert "Datenschutzerklärung" in body
    assert "<!DOCTYPE html>" in body


def test_delete_account_graceful_when_user_missing(monkeypatch):
    """delete_account should return ok even when the user does not exist in the DB.

    Scenario: new deployment with empty DB, user restores an old seed key.
    The hashed user_id doesn't match any record — delete should still succeed
    so the client can clear its localStorage and start fresh.
    """
    class NoUserSession(DummySession):
        def get(self, model, pk):
            return None  # user not found

    session = NoUserSession()
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    invalidated = []
    monkeypatch.setattr(main.tracks_cache, "invalidate", lambda uid: invalidated.append(uid))

    req = _mock_request("/account", "DELETE")
    result = delete_account(request=req, user_id=TEST_USER_ID)

    assert result == {"status": "ok"}
    assert not session.committed, "nothing to commit when user does not exist"
    assert TEST_USER_ID in invalidated, "cache should still be invalidated"
