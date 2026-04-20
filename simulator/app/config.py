"""Simulator settings — every knob the Phase-3 experiments flip."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Target
    ingest_api_url: str = "http://ingest-api:8000"

    # Load shape
    sim_eps: float = 10.0            # events/sec across all drivers
    sim_drivers: int = 50
    sim_seed: int = 42

    # Fault-injection dials (all 0 in Phase 1; flipped in Phase 3)
    sim_pct_late: float = 0.0        # fraction stamped with past occurred_at
    sim_pct_dup: float = 0.0         # fraction re-sent once
    sim_pct_malformed: float = 0.0   # fraction with bad/missing fields
    sim_late_max_s: int = 120

    # HTTP
    http_timeout_s: float = 5.0
    http_concurrency: int = 20

    log_level: str = "INFO"
