from __future__ import annotations

import io
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
    init_db()

@app.get("/health")
def health():
    return {"status": "ok", "time_utc": datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
def index():
    # Serve the single-page app
    return FileResponse("static/index.html")

@app.post("/upload")
async def upload_gpx(files: List[UploadFile] = File(...)):
    results = []
    with SessionLocal() as db:
        for f in files:
            try:
                content = await f.read()
                if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
                    results.append({
                        "filename": f.filename,
                        "status": "failed",
                        "reason": f"File exceeds {MAX_UPLOAD_MB} MB"
                    })
                    continue

                gpx = gpxpy.parse(io.StringIO(content.decode("utf-8", errors="ignore")))

                lines: list[geom.LineString] = []
                for trk in gpx.tracks:
                    for seg in trk.segments:
                        coords = [(p.longitude, p.latitude) for p in seg.points if p.longitude and p.latitude]
                        if len(coords) >= 2:
                            lines.append(geom.LineString(coords))

                for rte in gpx.routes:
                    coords = [(p.longitude, p.latitude) for p in rte.points if p.longitude and p.latitude]
                    if len(coords) >= 2:
                        lines.append(geom.LineString(coords))

                if not lines:
                    results.append({
                        "filename": f.filename,
                        "status": "skipped",
                        "reason": "No valid track points"
                    })
                    continue

                merged = linemerge(geom.MultiLineString(lines))
                if isinstance(merged, geom.LineString):
                    mls = geom.MultiLineString([merged.coords])
                else:
                    mls = merged

                track = Track(
                    name=(gpx.tracks[0].name if gpx.tracks and gpx.tracks[0].name else None),
                    filename=f.filename,
                    geom=from_shape(mls, srid=4326),
                )
                db.add(track)

                results.append({
                    "filename": f.filename,
                    "status": "ok"
                })

            except Exception as e:
                results.append({
                    "filename": f.filename,
                    "status": "failed",
                    "reason": str(e)
                })

        db.commit()

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
                    "created_at": t.created_at.isoformat() + "Z",
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [list(line.coords) for line in shp.geoms] if hasattr(shp, "geoms") else [list(shp.coords)]
                },
            })
    return JSONResponse({"type": "FeatureCollection", "features": features})
