"""Request models for POST /events/* endpoints."""
from __future__ import annotations

from typing import Literal

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


class GeoPointIn(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    h3_cell: str | None = None


class RideRequestedIn(BaseModel):
    # ride_id is client-generated so retries hash to the same partition and
    # downstream can dedupe — docs/02 §4.2.
    ride_id: str = Field(..., min_length=1)
    rider_id: str = Field(..., min_length=1)
    pickup: GeoPointIn
    dropoff: GeoPointIn
    product_tier: Literal["economy", "comfort", "premium"] = "economy"
    requested_at_ms: int | None = None
    occurred_at_ms: int | None = None
