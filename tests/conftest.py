import os
import sys
from pathlib import Path

os.environ.setdefault("DB_DSN", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("APP_SECRET_PEPPER", "test-pepper-do-not-use-in-production")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
