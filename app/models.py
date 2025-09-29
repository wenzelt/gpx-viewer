from datetime import datetime
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime
from geoalchemy2 import Geometry

Base = declarative_base()

from sqlalchemy import UniqueConstraint

class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("hash", name="uq_track_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    tag: Mapped[str | None] = mapped_column(String, nullable=True)
    hash: Mapped[str] = mapped_column(String, nullable=False)  # NEW
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    geom = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True)
    )
