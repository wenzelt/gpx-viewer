from datetime import datetime
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime
from geoalchemy2 import Geometry

Base = declarative_base()

class Track(Base):
    __tablename__ = "tracks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # Store MultiLineString in EPSG:4326 (lon/lat)
    geom = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=True)
    )

    # Optional: original filename for reference
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
