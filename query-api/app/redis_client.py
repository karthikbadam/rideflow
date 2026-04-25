"""Async Redis pool + hot-zones, surge, idle, and match query helpers.

Stream-shaped Redis keys written by Flink jobs:
  zones:hot:<window_end_ms>      ZSET   h3_cell -> distinct driver count   (01_hot_zones)
  zones:surge:<window_end_ms>    ZSET   h3_cell -> ride request count      (02_surge_pricing)
  drivers:idle                   ZSET   driver_id -> session_end_ms        (03_idle_detector)
  drivers:idle:<driver_id>       HASH   session_end_ms, ping_count, last_h3_cell
  rides:matches:recent           ZSET   "ride_id:driver_id" -> match_ms    (04_ride_matching)
  rides:matches:<ride_id>        ZSET   driver_id -> match_ms
  rides:matches:<ride_id>:meta   HASH   h3_cell, last_match_ms
"""
from __future__ import annotations

from typing import Optional

import redis.asyncio as aioredis

from .config import Settings


class RedisClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = aioredis.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            max_connections=settings.redis_pool_size,
            decode_responses=True,
        )

    def _conn(self) -> aioredis.Redis:
        return aioredis.Redis(connection_pool=self.pool)

    async def ping(self) -> bool:
        return await self._conn().ping()

    async def close(self) -> None:
        await self.pool.disconnect()

    async def _latest_window_key(self, prefix: str) -> Optional[str]:
        """Scan (not KEYS) for the highest window_end_ms under `prefix`."""
        latest_ms = -1
        latest_key: Optional[str] = None
        async for key in self._conn().scan_iter(match=f"{prefix}*", count=200):
            try:
                window_ms = int(key[len(prefix):])
            except ValueError:
                continue
            if window_ms > latest_ms:
                latest_ms = window_ms
                latest_key = key
        return latest_key

    async def top_hot_zones(self, limit: int) -> dict:
        prefix = self.settings.hot_zones_key_prefix
        key = await self._latest_window_key(prefix)
        if key is None:
            return {"window_end_ms": None, "zones": []}
        raw = await self._conn().zrevrange(key, 0, max(0, limit - 1), withscores=True)
        window_ms = int(key[len(prefix):])
        return {
            "window_end_ms": window_ms,
            "zones": [{"h3_cell": h3, "driver_count": int(score)} for h3, score in raw],
        }

    async def top_surge_zones(self, limit: int) -> dict:
        prefix = self.settings.surge_key_prefix
        key = await self._latest_window_key(prefix)
        if key is None:
            return {"window_end_ms": None, "zones": []}
        raw = await self._conn().zrevrange(key, 0, max(0, limit - 1), withscores=True)
        window_ms = int(key[len(prefix):])
        return {
            "window_end_ms": window_ms,
            "zones": [{"h3_cell": h3, "request_count": int(score)} for h3, score in raw],
        }

    async def idle_drivers(self, limit: int) -> dict:
        """Most-recently-idle drivers, descending by session_end_ms."""
        conn = self._conn()
        raw = await conn.zrevrange(self.settings.idle_key, 0, max(0, limit - 1), withscores=True)
        drivers = []
        for driver_id, score in raw:
            detail = await conn.hgetall(f"{self.settings.idle_key}:{driver_id}")
            drivers.append({
                "driver_id": driver_id,
                "session_end_ms": int(score),
                "ping_count": int(detail.get("ping_count", 0)) if detail else 0,
                "last_h3_cell": detail.get("last_h3_cell") if detail else None,
            })
        return {"drivers": drivers}

    async def recent_matches(self, limit: int) -> dict:
        raw = await self._conn().zrevrange(
            self.settings.matches_recent_key, 0, max(0, limit - 1), withscores=True
        )
        matches = []
        for member, score in raw:
            ride_id, _, driver_id = member.partition(":")
            matches.append({
                "ride_id": ride_id,
                "driver_id": driver_id,
                "match_time_ms": int(score),
            })
        return {"matches": matches}

    async def matches_for_ride(self, ride_id: str, limit: int) -> dict:
        conn = self._conn()
        key = f"{self.settings.matches_per_ride_prefix}{ride_id}"
        raw = await conn.zrevrange(key, 0, max(0, limit - 1), withscores=True)
        meta = await conn.hgetall(f"{key}:meta")
        return {
            "ride_id": ride_id,
            "h3_cell": meta.get("h3_cell") if meta else None,
            "candidates": [
                {"driver_id": d, "match_time_ms": int(s)} for d, s in raw
            ],
        }

    async def recent_anomalies(self, limit: int) -> dict:
        conn = self._conn()
        raw = await conn.zrevrange(
            self.settings.anomalies_recent_key, 0, max(0, limit - 1), withscores=True
        )
        anomalies = []
        for anomaly_id, score in raw:
            detail = await conn.hgetall(f"{self.settings.anomalies_detail_prefix}{anomaly_id}")
            if not detail:
                continue
            anomalies.append({
                "anomaly_id": anomaly_id,
                "detected_at_ms": int(score),
                "anomaly_type": detail.get("anomaly_type"),
                "driver_id": detail.get("driver_id"),
                "severity": detail.get("severity"),
                "evidence": {
                    k: detail[k]
                    for k in ("observed_speed_mps", "prev_lat", "prev_lon",
                              "curr_lat", "curr_lon", "gap_ms")
                    if k in detail
                },
            })
        return {"anomalies": anomalies}

    async def enriched_ride(self, ride_id: str) -> Optional[dict]:
        key = f"{self.settings.enriched_ride_prefix}{ride_id}"
        raw = await self._conn().hgetall(key)
        return raw or None
