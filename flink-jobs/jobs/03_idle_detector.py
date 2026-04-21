"""Idle driver detector — session window on driver_id with inactivity gap.

Pipeline:
  Kafka[driver.location.v1] (Avro-Confluent)
    → watermark on envelope.occurred_at
    → SESSION window keyed by payload.driver_id, gap = IDLE_GAP_MIN
    → emit one row per closed session: driver_id, session_end, ping_count,
      last_h3_cell
    → Redis: ZADD drivers:idle <session_end_ms> <driver_id>

Sessions close when no ping arrives for gap minutes. The watermark is what
triggers closure, so a stream that falls idle globally will also stop firing
sessions — this is a known PyFlink limitation (no per-key idleness in the
Table API for SESSION TVF). For 2.2 we accept it; E-F3 will exercise it.
"""
from __future__ import annotations

import logging
import os
import sys

import redis
from pyflink.common import Types
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.functions import MapFunction
from pyflink.table import StreamTableEnvironment


KAFKA_BOOTSTRAP = os.getenv(
    "KAFKA_BOOTSTRAP_SERVERS",
    "kafka-0:29092,kafka-1:29092,kafka-2:29092",
)
SR_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry:8081")
TOPIC = os.getenv("LOCATION_TOPIC", "driver.location.v1")
CONSUMER_GROUP = os.getenv("IDLE_GROUP", "flink-idle-detector")
STARTUP_MODE = os.getenv("IDLE_STARTUP", "latest-offset")
BOUNDEDNESS_SECONDS = os.getenv("IDLE_BOUNDEDNESS_SEC", "10")
IDLE_GAP_MIN = os.getenv("IDLE_GAP_MIN", "10")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("IDLE_TTL_SECONDS", "3600"))
IDLE_KEY = os.getenv("IDLE_KEY", "drivers:idle")


class RedisIdleWriter(MapFunction):
    """Record idle drivers in a single sorted set.

    Score = session_end_ms so query-api can return the most-recently-idle
    drivers in descending order or trim old ones by score range.
    """

    def __init__(self, host: str, port: int, key: str, ttl_s: int) -> None:
        self._host = host
        self._port = port
        self._key = key
        self._ttl_s = ttl_s
        self._client = None

    def _conn(self):
        if self._client is None:
            self._client = redis.Redis(host=self._host, port=self._port, decode_responses=True)
        return self._client

    def map(self, value):
        # value = Row[driver_id, session_end, ping_count, last_h3_cell]
        driver_id = value[0]
        session_end = value[1]
        ping_count = int(value[2])
        last_h3 = value[3]
        session_end_ms = int(session_end.timestamp() * 1000)
        pipe = self._conn().pipeline()
        pipe.zadd(self._key, {driver_id: session_end_ms})
        pipe.expire(self._key, self._ttl_s)
        # Per-driver detail hash so query-api can surface last_h3 + count.
        detail_key = f"{self._key}:{driver_id}"
        pipe.hset(
            detail_key,
            mapping={
                "session_end_ms": session_end_ms,
                "ping_count": ping_count,
                "last_h3_cell": last_h3 or "",
            },
        )
        pipe.expire(detail_key, self._ttl_s)
        pipe.execute()
        return f"idle {driver_id} end={session_end_ms} pings={ping_count}"


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    t_env.execute_sql(f"""
CREATE TABLE driver_location (
    envelope ROW<
        event_id      STRING,
        event_type    STRING,
        event_version INT,
        occurred_at   TIMESTAMP(3),
        ingested_at   TIMESTAMP(3),
        producer      STRING,
        trace_id      STRING
    >,
    payload ROW<
        driver_id        STRING,
        lat              DOUBLE,
        lon              DOUBLE,
        h3_cell          STRING,
        heading_degrees  DOUBLE,
        speed_mps        DOUBLE,
        accuracy_meters  DOUBLE
    >,
    event_time AS envelope.occurred_at,
    WATERMARK FOR event_time AS event_time - INTERVAL '{BOUNDEDNESS_SECONDS}' SECOND
) WITH (
    'connector'                         = 'kafka',
    'topic'                             = '{TOPIC}',
    'properties.bootstrap.servers'      = '{KAFKA_BOOTSTRAP}',
    'properties.group.id'               = '{CONSUMER_GROUP}',
    'scan.startup.mode'                 = '{STARTUP_MODE}',
    'format'                            = 'avro-confluent',
    'avro-confluent.url'                = '{SR_URL}'
)
""")

    # Flink 1.18 doesn't ship SESSION as a window TVF (added in 1.19). Use the
    # legacy grouped-window syntax instead: SESSION(rowtime, gap) as a window
    # function in GROUP BY, with SESSION_END(...) projected out.
    # Session windows merge on the fly, so each aggregate must support merge.
    # LAST_VALUE doesn't, so we substitute MAX over h3_cell — a stable (if not
    # strictly latest) per-session representative cell.
    idle = t_env.sql_query(f"""
SELECT
    payload.driver_id AS driver_id,
    SESSION_END(event_time, INTERVAL '{IDLE_GAP_MIN}' MINUTE) AS session_end,
    CAST(COUNT(*) AS BIGINT) AS ping_count,
    MAX(payload.h3_cell) AS last_h3_cell
FROM driver_location
GROUP BY payload.driver_id, SESSION(event_time, INTERVAL '{IDLE_GAP_MIN}' MINUTE)
""")

    ds = t_env.to_data_stream(idle)
    ds \
        .map(RedisIdleWriter(REDIS_HOST, REDIS_PORT, IDLE_KEY, REDIS_TTL), Types.STRING()) \
        .name("redis-idle") \
        .print()

    env.execute("idle-detector")


if __name__ == "__main__":
    main()
