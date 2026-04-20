"""Request model for POST /events/driver-location."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DriverLocationPingedIn(BaseModel):
    driver_id: str = Field(..., min_length=1)
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    h3_cell: str | None = None
    heading_degrees: float | None = None
    speed_mps: float | None = None
    accuracy_meters: float | None = None
    occurred_at_ms: int | None = None
