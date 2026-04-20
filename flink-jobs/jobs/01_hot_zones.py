"""Hot zones — count distinct drivers per H3 cell over tumbling windows.

Pipeline:
  Kafka[driver.location.v1] (Avro-Confluent, SR-resolved refs)
    → watermark on envelope.occurred_at with bounded out-of-orderness
    → GROUP BY h3_cell TUMBLE(event_time, 1 min)
    → COUNT DISTINCT driver_id
    → Redis: ZADD zones:hot:<window_end_ms> <count> <h3_cell>, EXPIRE TTL
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
CONSUMER_GROUP = os.getenv("HOT_ZONES_GROUP", "flink-hot-zones")
STARTUP_MODE = os.getenv("HOT_ZONES_STARTUP", "latest-offset")
BOUNDEDNESS_SECONDS = os.getenv("HOT_ZONES_BOUNDEDNESS_SEC", "10")
WINDOW_MINUTES = os.getenv("HOT_ZONES_WINDOW_MIN", "1")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("HOT_ZONES_TTL_SECONDS", "300"))


class RedisHotZoneWriter(MapFunction):
    """ZADD zones:hot:<window_end_ms> <count> <h3_cell>, then EXPIRE.

    Implemented as a MapFunction (not SinkFunction) because PyFlink's
    SinkFunction only wraps Java sinks — user Python sinks must take the form
    of a map/process + terminal print or a map whose side effect is the write.
    Returns a debug string for the terminal print sink.
    """

    def __init__(self, host: str, port: int, ttl_s: int) -> None:
        self._host = host
        self._port = port
        self._ttl_s = ttl_s
        self._client = None  # lazy: created in worker process

    def _conn(self):
        if self._client is None:
            self._client = redis.Redis(host=self._host, port=self._port, decode_responses=True)
        return self._client

    def map(self, value):
        # value = Row[h3_cell, window_end, driver_count]
        h3_cell = value[0]
        window_end = value[1]
        driver_count = int(value[2])
        window_end_ms = int(window_end.timestamp() * 1000)
        key = f"zones:hot:{window_end_ms}"
        pipe = self._conn().pipeline()
        pipe.zadd(key, {h3_cell: driver_count})
        pipe.expire(key, self._ttl_s)
        pipe.execute()
        return f"{key} {h3_cell}={driver_count}"


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    # Table schema must match (project from) the Avro schema for
    # driver.location.v1. occurred_at is logical timestamp-millis, which the
    # avro-confluent format decodes as TIMESTAMP_LTZ(3) — usable directly as
    # the event-time column.
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

    hot_zones = t_env.sql_query(f"""
SELECT
    payload.h3_cell AS h3_cell,
    window_end,
    COUNT(DISTINCT payload.driver_id) AS driver_count
FROM TABLE(
    TUMBLE(TABLE driver_location, DESCRIPTOR(event_time), INTERVAL '{WINDOW_MINUTES}' MINUTE)
)
GROUP BY window_start, window_end, payload.h3_cell
""")

    ds = t_env.to_data_stream(hot_zones)
    ds \
        .map(RedisHotZoneWriter(REDIS_HOST, REDIS_PORT, REDIS_TTL), Types.STRING()) \
        .name("redis-hot-zones") \
        .print()

    env.execute("hot-zones")


if __name__ == "__main__":
    main()
