"""H3 fallback — simulator normally precomputes h3_cell client-side (res 9)."""
from __future__ import annotations

import h3

RESOLUTION = 9


def cell_for(lat: float, lon: float, res: int = RESOLUTION) -> str:
    return h3.geo_to_h3(lat, lon, res)
