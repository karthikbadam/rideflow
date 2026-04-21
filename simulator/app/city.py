"""SF bounding box + driver personas.

Each driver has a home point and walks a small random step per tick with
momentum (drift vector persists across ticks and slowly rotates). Clamped to
the bbox. A handful of seeded 'hotspot' centers bias home placement so
downstream Flink aggregations have visible clusters.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# SF bbox from plan
LAT_MIN, LAT_MAX = 37.70, 37.82
LON_MIN, LON_MAX = -122.52, -122.37

# Biased home centers (lat, lon, weight) — loosely SoMa, FiDi, Mission, Marina
HOTSPOTS = [
    (37.7749, -122.4194, 0.35),
    (37.7946, -122.3999, 0.25),
    (37.7599, -122.4148, 0.25),
    (37.8030, -122.4378, 0.15),
]

# Step size per tick, in degrees (~11m per 1e-4 deg lat; we want ~car speed).
STEP_DEG = 2.5e-4
MOMENTUM = 0.85          # fraction of prior heading kept each tick
JITTER = 0.35            # fraction of step that's random
HOME_PULL = 0.02         # restoring-force fraction toward home per tick
MAX_SPEED_MPS = 25.0
ACCURACY_METERS = 8.0


@dataclass
class Driver:
    driver_id: str
    lat: float
    lon: float
    heading_rad: float
    home_lat: float = 0.0
    home_lon: float = 0.0
    speed_mps: float = 10.0
    step_mag: float = STEP_DEG

    def tick(self, rng: random.Random) -> None:
        # Rotate heading by a small random angle (momentum keeps direction).
        self.heading_rad = self.heading_rad * MOMENTUM + rng.uniform(-math.pi, math.pi) * (1 - MOMENTUM)
        # Random jitter on top of the step.
        dlat = math.cos(self.heading_rad) * self.step_mag + rng.uniform(-JITTER, JITTER) * self.step_mag
        dlon = math.sin(self.heading_rad) * self.step_mag + rng.uniform(-JITTER, JITTER) * self.step_mag
        # Restoring pull toward home so drivers don't drift to bbox edges and
        # desynchronize from the pickup hotspots matched against in 04_ride_matching.
        dlat += (self.home_lat - self.lat) * HOME_PULL
        dlon += (self.home_lon - self.lon) * HOME_PULL
        self.lat = _clamp(self.lat + dlat, LAT_MIN, LAT_MAX)
        self.lon = _clamp(self.lon + dlon, LON_MIN, LON_MAX)
        self.speed_mps = max(0.0, min(MAX_SPEED_MPS, self.speed_mps + rng.uniform(-1.0, 1.0)))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _weighted_hotspot(rng: random.Random) -> tuple[float, float]:
    r = rng.random()
    cum = 0.0
    for lat, lon, w in HOTSPOTS:
        cum += w
        if r <= cum:
            return lat, lon
    return HOTSPOTS[-1][0], HOTSPOTS[-1][1]


def make_drivers(n: int, seed: int) -> list[Driver]:
    rng = random.Random(seed)
    drivers: list[Driver] = []
    for i in range(n):
        hub_lat, hub_lon = _weighted_hotspot(rng)
        drivers.append(
            Driver(
                driver_id=f"drv-{i:04d}",
                lat=hub_lat + rng.uniform(-0.01, 0.01),
                lon=hub_lon + rng.uniform(-0.01, 0.01),
                heading_rad=rng.uniform(-math.pi, math.pi),
                home_lat=hub_lat,
                home_lon=hub_lon,
                speed_mps=rng.uniform(5.0, 15.0),
            )
        )
    return drivers
