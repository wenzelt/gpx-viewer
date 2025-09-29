from datetime import datetime
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime
from geoalchemy2 import Geometry

Base = declarative_base()

from sqlalchemy import Text

class Track(Base):
    __tablename__ = "tracks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    tag: Mapped[str | None] = mapped_column(Text, nullable=True)  # NEW: activity tag
    geom = mapped_column(Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True))
