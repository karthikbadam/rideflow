"""Settings — every Kafka producer dial exposed as env (docs/03)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # ---- Transport ----
    kafka_bootstrap_servers: str = "kafka-0:29092,kafka-1:29092,kafka-2:29092"
    schema_registry_url: str = "http://schema-registry:8081"

    # ---- Producer tuning (docs/03 §Producer) ----
    kafka_producer_acks: str = "all"
    kafka_producer_batch_size: int = 16384
    kafka_producer_linger_ms: int = 0
    kafka_producer_compression_type: str = "none"
    kafka_producer_max_in_flight_requests: int = 5
    kafka_producer_enable_idempotence: bool = True
    kafka_producer_buffer_memory: int = 33554432  # bytes; mapped to queue.buffering.max.kbytes

    # ---- App ----
    producer_name: str = "ingest-api"
    log_level: str = "INFO"
    location_topic: str = "driver.location.v1"


def build_producer_config(s: Settings) -> dict:
    # Keys are librdkafka-native. `buffer.memory` is a Java-API concept — we
    # expose it in bytes and translate to librdkafka's kbytes queue bound.
    return {
        "bootstrap.servers": s.kafka_bootstrap_servers,
        "client.id": s.producer_name,
        "acks": s.kafka_producer_acks,
        "batch.size": s.kafka_producer_batch_size,
        "linger.ms": s.kafka_producer_linger_ms,
        "compression.type": s.kafka_producer_compression_type,
        "max.in.flight.requests.per.connection": s.kafka_producer_max_in_flight_requests,
        "enable.idempotence": s.kafka_producer_enable_idempotence,
        "queue.buffering.max.kbytes": max(1, s.kafka_producer_buffer_memory // 1024),
    }
