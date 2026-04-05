from __future__ import annotations

import io
import os
import re
import time
import json
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Callable, Any
from collections.abc import Iterable
from threading import Lock
from pathlib import Path

import gpxpy
import sqlalchemy
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString, MultiLineString, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge
from sqlalchemy import select, exists, delete
from sqlalchemy.orm import Session

from db import SessionLocal, init_db
from models import Track

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
APP_TITLE = "Local GPX Viewer"
APP_VERSION = "1.1"

# ------------------------------------------------------------------------------
# App & Middleware
# ------------------------------------------------------------------------------
app = FastAPI(title=APP_TITLE, version=APP_VERSION)

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

STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class TracksCache:
    def __init__(self) -> None:
        self._lock = Lock()
        self._payload: dict[str, Any] | None = None
        self._etag: str | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None
            self._etag = None

    def get_payload(
        self, loader: Callable[[], tuple[dict[str, Any], str]]
    ) -> tuple[dict[str, Any], str]:
        with self._lock:
            if self._payload is not None and self._etag is not None:
                return self._payload, self._etag
            # Keep computation under lock to prevent concurrent cache misses
            # from recomputing the same expensive payload.
            payload, etag = loader()
            self._payload = payload
            self._etag = etag
            return payload, etag


tracks_cache = TracksCache()


# ------------------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    init_db_with_retry()


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


def _downsample_points(points: list, interval_seconds: int = 10) -> list:
    """Return a subset of points sampled at most every *interval_seconds*.

    When points carry timestamps, keeps a point only when at least
    *interval_seconds* have elapsed since the last kept point (always keeps
    the first and last point so the track shape is preserved).

    Falls back to every-Nth index sampling when timestamps are absent.
    """
    if not points:
        return points

    has_time = getattr(points[0], "time", None) is not None

    if has_time:
        kept: list = [points[0]]
        last_time = points[0].time
        for p in points[1:-1]:
            if p.time is None:
                continue
            delta = (p.time - last_time).total_seconds()
            if delta >= interval_seconds:
                kept.append(p)
                last_time = p.time
        if len(points) > 1:
            kept.append(points[-1])
        return kept

    # Fallback: keep every Nth point plus the last
    kept = points[::interval_seconds]
    if points[-1] is not kept[-1]:
        kept = list(kept) + [points[-1]]
    return kept


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


def _lines_from_gpx(gpx: gpxpy.gpx.GPX, downsample_interval: int = 10) -> list[LineString]:
    lines: list[LineString] = []

    # Tracks → segments
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts = _downsample_points(seg.points, downsample_interval)
            coords = _coords_from_points(pts)
            if len(coords) >= 2:
                lines.append(LineString(coords))

    # Routes
    for rte in gpx.routes:
        pts = _downsample_points(rte.points, downsample_interval)
        coords = _coords_from_points(pts)
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


def _is_duplicate_hash(db: Session, file_hash: str) -> bool:
    stmt = select(exists().where(Track.hash == file_hash))
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

    if _is_duplicate_hash(db, file_hash):
        logger.info("%s skipped (duplicate)", filename)
        return UploadOutcome(filename, "skipped", "Duplicate track")

    try:
        gpx = gpxpy.parse(io.StringIO(content.decode("utf-8", errors="ignore")))
    except Exception as parse_err:
        logger.error("Failed to parse %s: %s", filename, parse_err)
        return UploadOutcome(filename, "failed", "Invalid GPX")

    lines = _lines_from_gpx(gpx)
    if not lines:
        logger.warning("%s has no valid track points", filename)
        return UploadOutcome(filename, "skipped", "No valid track points")

    try:
        mls = _merge_to_multilinestring(lines)
        track = Track(
            name=_track_name(gpx),
            filename=filename,
            tag=extract_tag(filename),
            hash=file_hash,
            geom=from_shape(mls, srid=4326),
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
    return {"status": "ok", "time_utc": datetime.utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/upload")
async def upload_gpx(
    files: list[UploadFile] = File(...),
) -> dict[str, list[dict[str, str]]]:
    """Upload one or more GPX files and store them in PostGIS."""

    outcomes: list[UploadOutcome] = []
    seen_hashes: set[str] = set()

    with SessionLocal() as db:
        for upload_file in files:
            logger.info("📂 Upload received: %s", upload_file.filename or "unknown.gpx")
            outcome = await _process_upload_file(upload_file, db, seen_hashes)
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
        except Exception as commit_err:
            db.rollback()
            logger.exception("❌ DB commit failed")
            raise HTTPException(
                status_code=500, detail=f"DB commit failed: {commit_err}"
            )
        else:
            if any(outcome.status == "ok" for outcome in outcomes):
                tracks_cache.invalidate()

    return {"results": [outcome.as_dict() for outcome in outcomes]}


def _build_tracks_payload(
    limit: int | None = None, offset: int = 0
) -> tuple[dict[str, Any], str]:
    features: list[dict[str, Any]] = []
    stmt = (
        select(
            Track.id,
            Track.name,
            Track.filename,
            Track.tag,
            Track.created_at,
            Track.geom,
        )
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
        ) in db.execute(stmt):
            try:
                geometry = _ensure_multilinestring(to_shape(track_geom))
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
                    },
                    "geometry": mapping(geometry),
                }
            )

    payload = {"type": "FeatureCollection", "features": features}
    encoded = jsonable_encoder(payload)
    etag = compute_sha256(
        json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return encoded, etag


@app.get("/tracks")
def get_tracks(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=TRACKS_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """Return all tracks as a GeoJSON FeatureCollection."""

    is_default_query = limit is None and offset == 0
    if is_default_query:
        payload, etag = tracks_cache.get_payload(_build_tracks_payload)
    else:
        payload, etag = _build_tracks_payload(limit=limit, offset=offset)

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"}
        )

    response = JSONResponse(payload)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.delete("/delete_all")
def delete_all_tracks() -> dict[str, int | str]:
    with SessionLocal() as db:
        result = db.execute(delete(Track))
        rowcount = result.rowcount
        deleted_count = rowcount if rowcount is not None and rowcount >= 0 else 0
        db.commit()
    tracks_cache.invalidate()
    return {"status": "ok", "deleted": deleted_count}
