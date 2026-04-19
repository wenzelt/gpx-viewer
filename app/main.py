from __future__ import annotations

import io
import os
import re
import time
import json
import hashlib
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Callable, Any, Annotated, List
from collections.abc import Iterable
from threading import Lock
from pathlib import Path

import gpxpy
import sqlalchemy
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString, MultiLineString, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge
from sqlalchemy import select, exists, delete
from sqlalchemy.orm import Session

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from db import SessionLocal, init_db
from models import Track, User

# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------
MAX_UPLOAD_MB: int = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES: int = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
CORS_ALLOW_CREDENTIALS: bool = os.environ.get(
    "ALLOW_CREDENTIALS", "false"
).strip().lower() in {"1", "true", "yes", "on"}
TRACKS_PAGE_MAX: int = int(os.environ.get("TRACKS_PAGE_MAX", "1000"))
GEOM_SIMPLIFY_TOLERANCE: float = float(os.environ.get("GEOM_SIMPLIFY_TOLERANCE", "0.00002"))
# Server-side salt to prevent rainbow table attacks on seed phrase hashes.
# Must be set via environment variable — no insecure default allowed.
APP_SECRET_PEPPER: str = os.environ["APP_SECRET_PEPPER"]

APP_TITLE = "Local GPX Viewer"
APP_VERSION = "1.3"
MAX_FILES_PER_REQUEST: int = 50
MIN_SEED_PHRASE_LEN: int = 8

# ------------------------------------------------------------------------------
# App & Middleware
# ------------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db_with_retry()
    yield


app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=_lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

security = HTTPBearer()

def get_user_id(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    seed_phrase = credentials.credentials
    if len(seed_phrase) < MIN_SEED_PHRASE_LEN:
        raise HTTPException(status_code=401, detail=f"Seed phrase must be at least {MIN_SEED_PHRASE_LEN} characters")

    # Secure deterministic hash: seed_phrase + server_side_pepper
    hasher = hashlib.sha256()
    hasher.update(seed_phrase.strip().lower().encode())
    hasher.update(APP_SECRET_PEPPER.encode())
    return hasher.hexdigest()

logger = logging.getLogger("uvicorn.error")

if CORS_ALLOW_CREDENTIALS and ALLOWED_ORIGINS == ["*"]:
    logger.warning(
        "ALLOW_CREDENTIALS is true with wildcard ALLOWED_ORIGINS; "
        "forcing allow_credentials=False for safe/valid CORS behavior."
    )
    CORS_ALLOW_CREDENTIALS = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hardening Middleware: Security Headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable[[Request], Any]):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content Security Policy - restricts where scripts/styles can come from
    # and prevents inline scripts unless explicitly allowed.
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' unpkg.com; "
        "style-src 'self' 'unsafe-inline' unpkg.com fonts.googleapis.com api.fontshare.com; "
        "font-src 'self' fonts.gstatic.com api.fontshare.com; "
        "img-src 'self' data: *.basemaps.cartocdn.com unpkg.com user-gen-media-assets.s3.amazonaws.com; "
        "connect-src 'self';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response

STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


_CACHE_MAX_USERS = 1000


class TracksCache:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _get_user_state(self, user_id: str) -> dict[str, Any]:
        """Return (and create if needed) the cache state for user_id.

        MUST be called with self._lock held.
        Evicts the least-recently-used entry when the cache is at capacity.
        """
        if user_id not in self._cache:
            if len(self._cache) >= _CACHE_MAX_USERS:
                self._cache.popitem(last=False)  # evict LRU
            self._cache[user_id] = {"serialized_data": None, "etag": None, "version": 1}
        else:
            self._cache.move_to_end(user_id)
        return self._cache[user_id]

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            state = self._get_user_state(user_id)
            state["version"] += 1
            state["serialized_data"] = None
            state["etag"] = None

    def get_version(self, user_id: str) -> int:
        with self._lock:
            return self._get_user_state(user_id)["version"]

    def get_response(self, user_id: str, loader: Callable[[], tuple[bytes, str]]) -> Response:
        with self._lock:
            state = self._get_user_state(user_id)
            if state["serialized_data"] is None:
                state["serialized_data"], state["etag"] = loader()

            return Response(
                content=state["serialized_data"],
                media_type="application/json",
                headers={"ETag": state["etag"], "Cache-Control": "no-cache"},
            )


tracks_cache = TracksCache()


# ------------------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------------------
def init_db_with_retry(max_retries: int = 10, delay: int = 3) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            init_db()
            logger.info("✅ Database initialized")
            return
        except sqlalchemy.exc.OperationalError:
            logger.warning(
                "⚠️ DB not ready (attempt %s/%s), retrying...", attempt, max_retries
            )
            time.sleep(delay)
    raise RuntimeError("Database not ready after retries")


# ------------------------------------------------------------------------------
# Helpers (pure & testable)
# ------------------------------------------------------------------------------
_FILENAME_TAG_RE = re.compile(r".*-(\w+)\.gpx$", re.IGNORECASE)


def extract_tag(filename: str) -> str | None:
    m = _FILENAME_TAG_RE.match(filename)
    return m.group(1).lower() if m else None


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bytes_exceeds_limit(b: bytes, limit_bytes: int = MAX_UPLOAD_BYTES) -> bool:
    return len(b) > limit_bytes


def _coords_from_points(points: Iterable) -> list[tuple[float, float]]:
    # Filters out points without both lon/lat, keeps only 2D
    out: list[tuple[float, float]] = []
    for p in points:
        lon = getattr(p, "longitude", None)
        lat = getattr(p, "latitude", None)
        if lon is None or lat is None:
            continue
        out.append((float(lon), float(lat)))
    return out


def _lines_from_gpx(gpx: gpxpy.gpx.GPX) -> list[LineString]:
    lines: list[LineString] = []

    # Tracks → segments
    for trk in gpx.tracks:
        for seg in trk.segments:
            coords = _coords_from_points(seg.points)
            if len(coords) >= 2:
                lines.append(LineString(coords))

    # Routes
    for rte in gpx.routes:
        coords = _coords_from_points(rte.points)
        if len(coords) >= 2:
            lines.append(LineString(coords))

    return lines


def _merge_to_multilinestring(lines: list[LineString]) -> MultiLineString:
    """
    Merge lines and return a MultiLineString. Always returns MultiLineString.
    """
    if not lines:
        raise ValueError("No lines to merge")

    merged = linemerge(MultiLineString([list(ls.coords) for ls in lines]))
    if isinstance(merged, LineString):
        return MultiLineString([list(merged.coords)])
    if isinstance(merged, MultiLineString):
        # Normalize to plain python lists to avoid GeoAlchemy serialization quirks
        return MultiLineString([list(geom.coords) for geom in merged.geoms])

    # Fallback: wrap original lines
    return MultiLineString([list(ls.coords) for ls in lines])


def _track_name(gpx: gpxpy.gpx.GPX) -> str | None:
    try:
        return gpx.tracks[0].name or None
    except (IndexError, AttributeError):
        return None


def _is_duplicate_hash(db: Session, file_hash: str, user_id: str) -> bool:
    stmt = select(exists().where(Track.hash == file_hash).where(Track.user_id == user_id))
    return bool(db.execute(stmt).scalar())


@dataclass(slots=True)
class UploadOutcome:
    filename: str
    status: Literal["ok", "failed", "skipped"]
    reason: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"filename": self.filename, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _ensure_multilinestring(geometry: BaseGeometry) -> MultiLineString:
    if isinstance(geometry, MultiLineString):
        return geometry
    if isinstance(geometry, LineString):
        return MultiLineString([list(geometry.coords)])

    try:
        merged = linemerge(geometry)
    except Exception:
        merged = geometry

    if isinstance(merged, LineString):
        return MultiLineString([list(merged.coords)])
    if isinstance(merged, MultiLineString):
        return merged
    if hasattr(merged, "geoms"):
        lines = [geom for geom in merged.geoms if isinstance(geom, LineString)]
        if lines:
            return MultiLineString([list(line.coords) for line in lines])

    raise ValueError("Unsupported geometry type for track export")


async def _process_upload_file(
    upload_file: UploadFile,
    db: Session,
    seen_hashes: set[str],
    user_id: str,
) -> UploadOutcome:
    filename = upload_file.filename or "unknown.gpx"

    try:
        content: bytes = await upload_file.read()
    except Exception:
        logger.exception("Failed to read %s", filename)
        return UploadOutcome(filename, "failed", "Failed to read upload")

    if bytes_exceeds_limit(content):
        msg = f"File exceeds {MAX_UPLOAD_MB} MB"
        logger.warning("%s rejected: %s", filename, msg)
        return UploadOutcome(filename, "failed", msg)

    file_hash = compute_sha256(content)

    if file_hash in seen_hashes:
        logger.info("%s skipped (duplicate in request)", filename)
        return UploadOutcome(filename, "skipped", "Duplicate track (request)")

    if _is_duplicate_hash(db, file_hash, user_id):
        logger.info("%s skipped (duplicate)", filename)
        return UploadOutcome(filename, "skipped", "Duplicate track")

    try:
        gpx = gpxpy.parse(io.BytesIO(content))
    except Exception as parse_err:
        logger.error("Failed to parse %s: %s", filename, parse_err)
        return UploadOutcome(filename, "failed", "Invalid GPX")

    lines = _lines_from_gpx(gpx)
    if not lines:
        logger.warning("%s has no valid track points", filename)
        return UploadOutcome(filename, "skipped", "No valid track points")

    total_distance_m = 0.0
    total_uphill_m = 0.0
    for trk in gpx.tracks:
        total_distance_m += trk.length_2d()
        total_uphill_m += trk.get_uphill_downhill().uphill
    for rte in gpx.routes:
        total_distance_m += rte.length_2d()
        total_uphill_m += rte.get_uphill_downhill().uphill

    try:
        mls = _merge_to_multilinestring(lines)
        track = Track(
            name=_track_name(gpx),
            filename=filename,
            tag=extract_tag(filename),
            hash=file_hash,
            geom=from_shape(mls, srid=4326),
            total_distance_m=total_distance_m,
            total_elevation_gain_m=total_uphill_m,
            user_id=user_id,
        )
        db.add(track)
    except Exception:
        logger.exception("Unexpected failure while handling %s", filename)
        return UploadOutcome(filename, "failed", "Failed to store track")

    seen_hashes.add(file_hash)
    logger.info("%s saved successfully ✅", filename)
    return UploadOutcome(filename, "ok")


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/upload", response_model=None)
@limiter.limit("20/second")
async def upload_gpx(
    request: Request,
    files = File(...),
    user_id: str = Depends(get_user_id),
):
    """Upload one or more GPX files and store them in PostGIS."""
    upload_files: list[UploadFile] = files if isinstance(files, list) else [files]

    if len(upload_files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {MAX_FILES_PER_REQUEST} per request)",
        )

    outcomes: list[UploadOutcome] = []
    seen_hashes: set[str] = set()

    with SessionLocal() as db:
        # Implicitly ensure user exists
        user = db.get(User, user_id)
        if not user:
            user = User(id=user_id)
            db.add(user)
            db.commit()
            db.refresh(user)

        for upload_file in upload_files:
            logger.info("📂 Upload received: %s", upload_file.filename or "unknown.gpx")
            outcome = await _process_upload_file(upload_file, db, seen_hashes, user_id)
            outcomes.append(outcome)

        try:
            db.commit()
        except sqlalchemy.exc.IntegrityError as ie:
            db.rollback()
            logger.warning("Integrity error on commit (likely duplicate): %s", ie)
            for outcome in outcomes:
                if outcome.status == "ok":
                    outcome.status = "skipped"
                    outcome.reason = "Duplicate (constraint)"
        except Exception:
            db.rollback()
            logger.exception("❌ DB commit failed")
            raise HTTPException(status_code=500, detail="Internal server error")
        else:
            if any(outcome.status == "ok" for outcome in outcomes):
                tracks_cache.invalidate(user_id)

    return {"results": [outcome.as_dict() for outcome in outcomes]}


def _build_tracks_serialized(
    user_id: str,
    limit: int | None = None,
    offset: int = 0
) -> bytes:
    features: list[dict[str, Any]] = []
    stmt = (
        select(
            Track.id,
            Track.name,
            Track.filename,
            Track.tag,
            Track.created_at,
            Track.geom,
            Track.total_distance_m,
            Track.total_elevation_gain_m,
        )
        .where(Track.user_id == user_id)
        .order_by(Track.id)
        .offset(offset)
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    with SessionLocal() as db:
        for (
            track_id,
            track_name,
            track_filename,
            track_tag,
            track_created_at,
            track_geom,
            track_distance_m,
            track_elevation_gain_m,
        ) in db.execute(stmt):
            try:
                raw_geom = to_shape(track_geom)
                if GEOM_SIMPLIFY_TOLERANCE > 0:
                    raw_geom = raw_geom.simplify(GEOM_SIMPLIFY_TOLERANCE, preserve_topology=True)
                geometry = _ensure_multilinestring(raw_geom)
            except ValueError as err:
                logger.warning("Skipping track %s: %s", track_id, err)
                continue

            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": track_id,
                        "name": track_name or track_filename or f"Track {track_id}",
                        "tag": track_tag,
                        "created_at": (track_created_at.isoformat() + "Z")
                        if track_created_at
                        else None,
                        "total_distance_m": track_distance_m,
                        "total_elevation_gain_m": track_elevation_gain_m,
                    },
                    "geometry": mapping(geometry),
                }
            )

    payload = {"type": "FeatureCollection", "features": features}
    return json.dumps(jsonable_encoder(payload)).encode("utf-8")


@app.get("/tracks")
@limiter.limit("20/second")
def get_tracks(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=TRACKS_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_user_id),
) -> Response:
    """Return all tracks as a GeoJSON FeatureCollection."""

    is_default_query = limit is None and offset == 0
    if is_default_query:
        # Check ETag before loading/serializing
        etag = str(tracks_cache.get_version(user_id))
        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"}
            )
        
        return tracks_cache.get_response(
            user_id,
            lambda: (_build_tracks_serialized(user_id), etag)
        )

    # For non-default queries, we don't cache but still provide a deterministic ETag
    serialized = _build_tracks_serialized(user_id, limit=limit, offset=offset)
    etag = compute_sha256(serialized)

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"}
        )

    return Response(
        content=serialized,
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@app.delete("/delete_all")
@limiter.limit("5/minute")
def delete_all_tracks(request: Request, user_id: str = Depends(get_user_id)) -> dict[str, int | str]:
    with SessionLocal() as db:
        result = db.execute(delete(Track).where(Track.user_id == user_id))
        rowcount = result.rowcount
        deleted_count = rowcount if rowcount is not None and rowcount >= 0 else 0
        db.commit()
    tracks_cache.invalidate(user_id)
    return {"status": "ok", "deleted": deleted_count}


@app.post("/auth/create")
@limiter.limit("10/minute")
def create_vault(request: Request, user_id: str = Depends(get_user_id)) -> dict[str, str]:
    """Explicitly initialize a new vault/user."""
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            user = User(id=user_id)
            db.add(user)
            db.commit()
    return {"status": "ok", "user_id": user_id}


@app.delete("/account")
@limiter.limit("5/minute")
def delete_account(request: Request, user_id: str = Depends(get_user_id)) -> dict[str, str]:
    """Delete the entire account and all associated tracks."""
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user:
            db.delete(user)
            db.commit()
    tracks_cache.invalidate(user_id)
    return {"status": "ok"}
