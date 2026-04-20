import os
import sys
from pathlib import Path

# Run tests against the production code path so auth and rate-limit logic is exercised.
# Individual tests that need local mode can monkeypatch main.LOCAL_MODE directly.
os.environ.setdefault("APP_ENV", "production")
os.environ.setdefault("DB_DSN", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("APP_SECRET_PEPPER", "test-pepper-do-not-use-in-production")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
