"""Async Postgres pool — Phase-2 stub for CDC-enriched reads.

Not used in Phase 1 (hot zones are Redis-only). Wired here so Phase 2 wiring
is an import-and-use change, not a new module.
"""
from __future__ import annotations

import asyncpg

from .config import Settings


class PgClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            host=self.settings.postgres_host,
            port=self.settings.postgres_port,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password,
            database=self.settings.postgres_db,
            min_size=self.settings.postgres_pool_min,
            max_size=self.settings.postgres_pool_max,
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
