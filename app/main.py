from __future__ import annotations

import io
import re
import os
from datetime import datetime
from typing import List

import gpxpy
import shapely.geometry as geom
from shapely.ops import linemerge
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape, to_shape

from db import SessionLocal, init_db
from models import Track

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))

app = FastAPI(title="Local GPX Viewer", version="1.0")

# CORS (adjust origins if exposing on the internet)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to your domain if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# static UI
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
def _startup():
    init_db_with_retry()


@app.get("/health")
def health():
    return {"status": "ok", "time_utc": datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
def index():
    # Serve the single-page app
    return FileResponse("static/index.html")

import time
import sqlalchemy

def init_db_with_retry(max_retries=10, delay=3):
    for attempt in range(max_retries):
        try:
            init_db()
            return
        except sqlalchemy.exc.OperationalError as e:
            print(f"⚠️ DB not ready (attempt {attempt+1}/{max_retries}), retrying...")
            time.sleep(delay)
    raise RuntimeError("Database not ready after retries")


import logging

logger = logging.getLogger("uvicorn.error")

@app.post("/upload")
async def upload_gpx(files: List[UploadFile] = File(...)):
    """
    Upload one or more GPX files, parse them into LineStrings,
    deduplicate by file hash, and store in PostGIS.
    """
    def extract_tag(filename: str) -> str | None:
        # filename format: YYYY-MM-DD_HH.MM.SS-<tag>.gpx
        m = re.match(r".*-(\w+)\.gpx$", filename)
        return m.group(1).lower() if m else None

    results = []

    with SessionLocal() as db:
        for f in files:
            try:
                logger.info(f"📂 Upload received: {f.filename}")
                content = await f.read()

                # --- File size check ---
                if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
                    msg = f"File exceeds {MAX_UPLOAD_MB} MB"
                    logger.warning(f"{f.filename} rejected: {msg}")
                    results.append({"filename": f.filename, "status": "failed", "reason": msg})
                    continue

                # --- Compute SHA256 hash ---
                file_hash = hashlib.sha256(content).hexdigest()

                # --- Duplicate detection ---
                if db.query(Track).filter_by(hash=file_hash).first():
                    logger.info(f"{f.filename} skipped (duplicate)")
                    results.append({"filename": f.filename, "status": "skipped", "reason": "Duplicate track"})
                    continue

                # --- Parse GPX ---
                try:
                    gpx = gpxpy.parse(io.StringIO(content.decode("utf-8", errors="ignore")))
                except Exception as parse_err:
                    logger.error(f"Failed to parse {f.filename}: {parse_err}")
                    results.append({"filename": f.filename, "status": "failed", "reason": "Invalid GPX"})
                    continue

                lines: list[geom.LineString] = []

                # Tracks
                for trk in gpx.tracks:
                    for seg in trk.segments:
                        coords = [(p.longitude, p.latitude) for p in seg.points if p.longitude and p.latitude]
                        if len(coords) >= 2:
                            lines.append(geom.LineString(coords))

                # Routes
                for rte in gpx.routes:
                    coords = [(p.longitude, p.latitude) for p in rte.points if p.longitude and p.latitude]
                    if len(coords) >= 2:
                        lines.append(geom.LineString(coords))

                if not lines:
                    logger.warning(f"{f.filename} has no valid track points")
                    results.append({"filename": f.filename, "status": "skipped", "reason": "No valid track points"})
                    continue

                # --- Merge & normalize geometry ---
                merged = linemerge(geom.MultiLineString(lines))
                if isinstance(merged, geom.LineString):
                    mls = geom.MultiLineString([merged.coords])
                else:
                    mls = merged

                # --- Save track ---
                track = Track(
                    name=(gpx.tracks[0].name if gpx.tracks and gpx.tracks[0].name else None),
                    filename=f.filename,
                    tag=extract_tag(f.filename),
                    hash=file_hash,
                    geom=from_shape(mls, srid=4326),
                )
                db.add(track)

                results.append({"filename": f.filename, "status": "ok"})
                logger.info(f"{f.filename} saved successfully ✅")

            except Exception as e:
                logger.exception(f"Unexpected failure while handling {f.filename}")
                results.append({"filename": f.filename, "status": "failed", "reason": str(e)})

        try:
            db.commit()
        except Exception as commit_err:
            logger.exception("❌ DB commit failed")
            raise HTTPException(status_code=500, detail=f"DB commit failed: {commit_err}")

    return {"results": results}


@app.get("/tracks")
def get_tracks():
    """Return all tracks as a GeoJSON FeatureCollection."""
    features = []
    with SessionLocal() as db:
        for t in db.execute(select(Track)).scalars():
            shp = to_shape(t.geom)  # shapely MultiLineString
            # Build minimal GeoJSON feature
            features.append({
                "type": "Feature",
                "properties": {
                    "id": t.id,
                    "name": t.name or t.filename or f"Track {t.id}",
                    "tag": t.tag,
                    "created_at": t.created_at.isoformat() + "Z",
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [list(line.coords) for line in shp.geoms] if hasattr(shp, "geoms") else [list(shp.coords)]
                },
            })
    return JSONResponse({"type": "FeatureCollection", "features": features})

from sqlalchemy import delete

@app.delete("/delete_all")
def delete_all_tracks():
    with SessionLocal() as db:
        db.execute(delete(Track))   # delete all rows
        db.commit()
    return {"status": "ok", "deleted": "all"}
