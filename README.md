# GPX Viewer

A lightweight GPX track visualizer running in Docker.  
Upload `.gpx` files via a simple web interface, and view them on an interactive OpenStreetMap world map.

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) (backend API & upload service)
- [PostGIS](https://postgis.net/) (store and query tracks)
- [Leaflet.js](https://leafletjs.com/) + [OpenStreetMap](https://www.openstreetmap.org/) (map visualization)
- Docker Compose (for easy setup)

---

## Features

- 📂 Drag & drop multiple `.gpx` files to upload at once
- 📊 Per-file upload status (success, skipped, failed)
- 🌍 All tracks shown on a scrollable/zoomable world map
- 💾 Tracks stored in PostGIS with spatial indexing
- 🔄 Persists data across container restarts

---

## Prerequisites

- macOS / Linux / Windows with [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed
- `docker compose` command available (`docker compose version`)

---

## Getting Started

Clone the project:

```bash
git clone https://github.com/yourname/gpx-viewer.git
cd gpx-viewer
```

Start the stack:

```bash
docker compose up --build
```

Open the app:

👉 [http://localhost:8000](http://localhost:8000)

---

## Usage

1. Drag & drop `.gpx` files onto the upload box (or click to select).
2. Uploaded tracks will be stored in PostGIS and immediately shown on the map.
3. Upload multiple files at once — each will report **ok / failed / skipped**.
4. Tracks persist across restarts (stored in a Docker volume).

Example API calls:

- Upload with `curl`:

  ```bash
  curl -F "files=@/path/to/track1.gpx"        -F "files=@/path/to/track2.gpx"        http://localhost:8000/upload
  ```

- List all tracks as GeoJSON:

  ```bash
  curl http://localhost:8000/tracks
  ```

- Health check:

  ```bash
  curl http://localhost:8000/health
  ```

---

## Project Structure

```
gpx-viewer/
├─ docker-compose.yml        # Services: PostGIS + FastAPI app
├─ .env                      # Database credentials
├─ app/
│  ├─ Dockerfile             # FastAPI app container
│  ├─ requirements.txt       # Python dependencies
│  ├─ main.py                # FastAPI endpoints
│  ├─ models.py              # SQLAlchemy ORM models
│  ├─ db.py                  # Database setup & session
│  └─ static/
│     └─ index.html          # Frontend (Leaflet + upload UI)
```

---

## Data Persistence

- PostGIS data is stored in the Docker volume `gpx-viewer_pgdata`
- Uploaded files are stored in `gpx-viewer_uploads` (not strictly needed, but mounted)

To reset everything:

```bash
docker compose down -v
```

---

## Notes for Apple Silicon (M1/M2)

If you see a platform mismatch warning for PostGIS, add this under the `db:` service in `docker-compose.yml`:

```yaml
platform: linux/arm64/v8
```

---

## Roadmap / Ideas

- Delete/reset tracks from the UI
- Store GPX metadata (activity type, distance, elevation)
- Add authentication if exposing publicly
- Geometry simplification for large datasets

---

## License

MIT – feel free to fork and adapt.
