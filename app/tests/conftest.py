"""pytest configuration — set DB_DSN before any module imports it."""
import os

os.environ.setdefault("DB_DSN", "sqlite:///:memory:")
