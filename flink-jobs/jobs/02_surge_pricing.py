"""Surge pricing — sliding-window ride-request demand per H3 cell.

Pipeline:
  Kafka[ride.request.v1] (Avro-Confluent, SR-resolved refs)
    → watermark on envelope.occurred_at with bounded out-of-orderness
    → HOP(5 min size, 1 min slide) partitioned by payload.pickup.h3_cell
    → COUNT(*) as request_count
    → Redis: ZADD zones:surge:<window_end_ms> <count> <h3_cell>, EXPIRE TTL

The slide interval is what makes this a *sliding* window — every SLIDE
minutes we emit a fresh snapshot of the last WINDOW minutes of demand.
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
TOPIC = os.getenv("RIDE_REQUEST_TOPIC", "ride.request.v1")
CONSUMER_GROUP = os.getenv("SURGE_GROUP", "flink-surge-pricing")
STARTUP_MODE = os.getenv("SURGE_STARTUP", "latest-offset")
BOUNDEDNESS_SECONDS = os.getenv("SURGE_BOUNDEDNESS_SEC", "10")
WINDOW_MINUTES = os.getenv("SURGE_WINDOW_MIN", "5")
SLIDE_MINUTES = os.getenv("SURGE_SLIDE_MIN", "1")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("SURGE_TTL_SECONDS", "600"))


class RedisSurgeWriter(MapFunction):
    """ZADD zones:surge:<window_end_ms> <count> <h3_cell>, then EXPIRE.

    Same MapFunction-as-sink pattern as the hot-zones job — the terminal
    print() on the returned string keeps the datastream alive.
    """

    def __init__(self, host: str, port: int, ttl_s: int) -> None:
        self._host = host
        self._port = port
        self._ttl_s = ttl_s
        self._client = None

    def _conn(self):
        if self._client is None:
            self._client = redis.Redis(host=self._host, port=self._port, decode_responses=True)
        return self._client

    def map(self, value):
        # value = Row[h3_cell, window_end, request_count]
        h3_cell = value[0]
        window_end = value[1]
        request_count = int(value[2])
        window_end_ms = int(window_end.timestamp() * 1000)
        key = f"zones:surge:{window_end_ms}"
        pipe = self._conn().pipeline()
        pipe.zadd(key, {h3_cell: request_count})
        pipe.expire(key, self._ttl_s)
        pipe.execute()
        return f"{key} {h3_cell}={request_count}"


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    # Schema matches ride.request.v1 (Envelope + RideRequestedPayload with
    # nested GeoPoint pickup/dropoff). occurred_at is Avro timestamp-millis,
    # decoded by avro-confluent as TIMESTAMP(3) — suitable for event-time.
    t_env.execute_sql(f"""
CREATE TABLE ride_request (
    envelope ROW<
        event_id      STRING,
        event_type    STRING,
        event_version INT,
        occurred_at   TIMESTAMP(3),
        ingested_at   TIMESTAMP(3),
        producer      STRING,
        trace_id      STRING
    > NOT NULL,
    -- product_tier/requested_at/dropoff omitted: Flink 1.18 avro-confluent
    -- can't coerce Avro enum→string. Avro resolution skips writer-only fields.
    payload ROW<
        ride_id       STRING,
        rider_id      STRING,
        pickup  ROW<lat DOUBLE, lon DOUBLE, h3_cell STRING> NOT NULL
    > NOT NULL,
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

    surge = t_env.sql_query(f"""
SELECT
    payload.pickup.h3_cell AS h3_cell,
    window_end,
    COUNT(*) AS request_count
FROM TABLE(
    HOP(
        TABLE ride_request,
        DESCRIPTOR(event_time),
        INTERVAL '{SLIDE_MINUTES}' MINUTE,
        INTERVAL '{WINDOW_MINUTES}' MINUTE
    )
)
GROUP BY window_start, window_end, payload.pickup.h3_cell
""")

    ds = t_env.to_data_stream(surge)
    ds \
        .map(RedisSurgeWriter(REDIS_HOST, REDIS_PORT, REDIS_TTL), Types.STRING()) \
        .name("redis-surge") \
        .print()

    env.execute("surge-pricing")


if __name__ == "__main__":
    main()
