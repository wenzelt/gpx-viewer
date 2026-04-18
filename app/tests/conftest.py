"""pytest configuration — set DB_DSN before any module imports it."""
import os

os.environ.setdefault("DB_DSN", "sqlite:///:memory:")
os.environ.setdefault("APP_SECRET_PEPPER", "test-pepper-do-not-use-in-production")
