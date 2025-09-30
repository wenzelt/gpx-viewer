import os
import sys
from pathlib import Path

os.environ.setdefault("DB_DSN", "sqlite+pysqlite:///:memory:")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
