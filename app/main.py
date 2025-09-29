from __future__ import annotations

import io
import os
import re
import time
import hashlib
import logging
from datetime import datetime
from typing import Iterable, List, Optional

import gpxpy
import sqlalchemy
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from sqlalchemy import select, exists, delete
from sqlalchemy.orm import Session

from db import SessionLocal, init_db
from models import Track

# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------
MAX_UPLOAD_MB: int = int(os.environ.get("MAX_UPLOAD_MB", "50"))
ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
APP_TITLE = "Local GPX Viewer"
APP_VERSION = "1.1"

# ------------------------------------------------------------------------------
# App & Middleware
# ------------------------------------------------------------------------------
app = FastAPI(title=APP_TITLE, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

logger = logging.getLogger("uvicorn.error")

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
            logger.warning("⚠️ DB not ready (attempt %s/%s), retrying...", attempt, max_retries)
            time.sleep(delay)
    raise RuntimeError("Database not ready after retries")

# ------------------------------------------------------------------------------
# Helpers (pure & testable)
# ------------------------------------------------------------------------------
_FILENAME_TAG_RE = re.compile(r".*-(\w+)\.gpx$", re.IGNORECASE)

def extract_tag(filename: str) -> Optional[str]:
    m = _FILENAME_TAG_RE.match(filename)
    return m.group(1).lower() if m else None

def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def bytes_exceeds_limit(b: bytes, limit_mb: int) -> bool:
    return len(b) > (limit_mb * 1024 * 1024)

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

def _track_name(gpx: gpxpy.gpx.GPX) -> Optional[str]:
    try:
        return gpx.tracks[0].name or None
    except Exception:
        return None

def _is_duplicate_hash(db: Session, file_hash: str) -> bool:
    stmt = select(exists().where(Track.hash == file_hash))
    return bool(db.execute(stmt).scalar())

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "time_utc": datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse("static/index.html")

@app.post("/upload")
async def upload_gpx(files: List[UploadFile] = File(...)):
    """
    Upload one or more GPX files, parse into MultiLineStrings,
    deduplicate by content hash, and store in PostGIS.
    """
    results: list[dict] = []

    with SessionLocal() as db:
        for f in files:
            filename = f.filename or "unknown.gpx"
            try:
                logger.info("📂 Upload received: %s", filename)
                content: bytes = await f.read()

                # Size check (after read; multipart size header is not reliable)
                if bytes_exceeds_limit(content, MAX_UPLOAD_MB):
                    msg = f"File exceeds {MAX_UPLOAD_MB} MB"
                    logger.warning("%s rejected: %s", filename, msg)
                    results.append({"filename": filename, "status": "failed", "reason": msg})
                    continue

                file_hash = compute_sha256(content)

                # Fast duplicate check
                if _is_duplicate_hash(db, file_hash):
                    logger.info("%s skipped (duplicate)", filename)
                    results.append({"filename": filename, "status": "skipped", "reason": "Duplicate track"})
                    continue

                # Parse GPX (decode robustly, ignore undecodable chars)
                try:
                    gpx = gpxpy.parse(io.StringIO(content.decode("utf-8", errors="ignore")))
                except Exception as parse_err:
                    logger.error("Failed to parse %s: %s", filename, parse_err)
                    results.append({"filename": filename, "status": "failed", "reason": "Invalid GPX"})
                    continue

                lines = _lines_from_gpx(gpx)
                if not lines:
                    logger.warning("%s has no valid track points", filename)
                    results.append({"filename": filename, "status": "skipped", "reason": "No valid track points"})
                    continue

                mls = _merge_to_multilinestring(lines)

                track = Track(
                    name=_track_name(gpx),
                    filename=filename,
                    tag=extract_tag(filename),
                    hash=file_hash,
                    geom=from_shape(mls, srid=4326),
                )
                db.add(track)
                results.append({"filename": filename, "status": "ok"})
                logger.info("%s saved successfully ✅", filename)

            except Exception as e:
                logger.exception("Unexpected failure while handling %s", filename)
                results.append({"filename": filename, "status": "failed", "reason": str(e)})

        # Single commit for batch (fewer round-trips)
        try:
            db.commit()
        except sqlalchemy.exc.IntegrityError as ie:
            # In case a hash race slipped through the exists() check
            db.rollback()
            logger.warning("Integrity error on commit (likely duplicate): %s", ie)
            # Mark undetected duplicates as skipped in results for transparency
            for r in results:
                if r["status"] == "ok":
                    r["status"] = "skipped"
                    r["reason"] = "Duplicate (constraint)"
        except Exception as commit_err:
            db.rollback()
            logger.exception("❌ DB commit failed")
            raise HTTPException(status_code=500, detail=f"DB commit failed: {commit_err}")

    return {"results": results}

@app.get("/tracks")
def get_tracks():
    """Return all tracks as a GeoJSON FeatureCollection."""
    features: list[dict] = []
    with SessionLocal() as db:
        for t in db.execute(select(Track)).scalars():
            shp = to_shape(t.geom)  # shapely geometry
            # Ensure MultiLineString in output
            if isinstance(shp, LineString):
                shp = MultiLineString([list(shp.coords)])
            elif isinstance(shp, MultiLineString):
                # normalize to basic lists for JSON safety
                shp = MultiLineString([list(geom.coords) for geom in shp.geoms])

            coordinates: list[list[tuple[float, float]]] = (
                [list(line.coords) for line in shp.geoms]  # type: ignore[attr-defined]
                if hasattr(shp, "geoms")
                else [list(shp.coords)]  # type: ignore[arg-type]
            )

            features.append({
                "type": "Feature",
                "properties": {
                    "id": t.id,
                    "name": t.name or t.filename or f"Track {t.id}",
                    "tag": t.tag,
                    "created_at": (t.created_at.isoformat() + "Z") if t.created_at else None,
                },
                "geometry": {"type": "MultiLineString", "coordinates": coordinates},
            })
    return JSONResponse({"type": "FeatureCollection", "features": features})

@app.delete("/delete_all")
def delete_all_tracks():
    with SessionLocal() as db:
        db.execute(delete(Track))
        db.commit()
    return {"status": "ok", "deleted": "all"}
