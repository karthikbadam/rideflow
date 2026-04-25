"""Query API settings — Redis + Postgres (Phase-2 stub)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Redis — materialized hot zones
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_pool_size: int = 20

    # Hot zones
    hot_zones_key_prefix: str = "zones:hot:"
    hot_zones_default_limit: int = 10
    hot_zones_max_limit: int = 100

    # Surge (Phase 2.1)
    surge_key_prefix: str = "zones:surge:"

    # Idle drivers (Phase 2.2)
    idle_key: str = "drivers:idle"

    # Ride matches (Phase 2.3)
    matches_recent_key: str = "rides:matches:recent"
    matches_per_ride_prefix: str = "rides:matches:"

    # Anomalies (Phase 2.4)
    anomalies_recent_key: str = "anomalies:recent"
    anomalies_detail_prefix: str = "anomalies:"

    # Enriched rides (Phase 2.5)
    enriched_ride_prefix: str = "rides:enriched:"

    # Postgres — unused in Phase 1, read from env so Phase 2 just wires in.
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "rideflow"
    postgres_user: str = "rideflow"
    postgres_password: str = "rideflow"
    postgres_pool_min: int = 1
    postgres_pool_max: int = 10

    log_level: str = "INFO"
