from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Float, Integer, String, DateTime, UniqueConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, declarative_base, relationship

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Hashed seed phrase
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    tracks: Mapped[list[Track]] = relationship("Track", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, created_at={self.created_at!r})"


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("user_id", "hash", name="uq_user_track_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    tag: Mapped[str | None] = mapped_column(String, nullable=True)
    hash: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    total_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_elevation_gain_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # GeoAlchemy Geometry column; typing as Any to keep mypy happy
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True)
    )

    user: Mapped[User] = relationship("User", back_populates="tracks")

    def __repr__(self) -> str:
        return f"Track(id={self.id!r}, filename={self.filename!r}, tag={self.tag!r}, user_id={self.user_id!r})"
