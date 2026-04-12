from __future__ import annotations

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Float, Integer, String, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, declarative_base

Base = declarative_base()

class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("hash", name="uq_track_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    tag: Mapped[str | None] = mapped_column(String, nullable=True)
    hash: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    total_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_elevation_gain_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # GeoAlchemy Geometry column; typing as Any to keep mypy happy
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True)
    )

    def __repr__(self) -> str:
        return f"Track(id={self.id!r}, filename={self.filename!r}, tag={self.tag!r})"
