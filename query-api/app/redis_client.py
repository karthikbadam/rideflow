"""Async Redis pool + hot-zones query helpers.

Hot zones are written by the Flink job as `zones:hot:<window_end_ms>` sorted
sets (score = distinct driver count). This module answers: "give me top-N
cells from the most recent closed window."
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

    async def latest_window_key(self) -> Optional[str]:
        """Return the highest window key, or None if no windows have closed yet.

        SCAN (not KEYS) to avoid blocking Redis on larger keyspaces.
        """
        prefix = self.settings.hot_zones_key_prefix
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
        """Top-N cells (descending) from the most recent window.

        Returns a dict ready to be JSON-serialized by FastAPI.
        """
        key = await self.latest_window_key()
        if key is None:
            return {"window_end_ms": None, "zones": []}

        # ZREVRANGE with scores → list of (member, score) pairs.
        raw = await self._conn().zrevrange(key, 0, max(0, limit - 1), withscores=True)
        window_ms = int(key[len(self.settings.hot_zones_key_prefix):])
        return {
            "window_end_ms": window_ms,
            "zones": [{"h3_cell": h3, "driver_count": int(score)} for h3, score in raw],
        }
