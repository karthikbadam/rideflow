"""Ride matching — interval join ride.request.v1 × driver.location.v1.

Pipeline:
  ride_request:   Kafka[ride.request.v1] (pickup H3)
  driver_location: Kafka[driver.location.v1] (driver H3)

  INNER JOIN on pickup.h3_cell = driver.h3_cell
  WHERE driver.event_time BETWEEN request.event_time - PRE_SEC AND request.event_time + POST_SEC

  → Row[ride_id, driver_id, match_time_ms, h3_cell]
  → Redis:
      ZADD rides:matches:<ride_id> <match_time_ms> <driver_id>  (candidates per ride)
      ZADD rides:matches:recent <match_time_ms> <ride_id:driver_id>  (global stream)

Interval join is the textbook Flink pattern for request/supply matching: it
keeps both sides' state only for the lookback+lookahead window, so memory
stays bounded as throughput grows.
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
REQUEST_TOPIC = os.getenv("RIDE_REQUEST_TOPIC", "ride.request.v1")
LOCATION_TOPIC = os.getenv("LOCATION_TOPIC", "driver.location.v1")
CONSUMER_GROUP = os.getenv("MATCH_GROUP", "flink-ride-matching")
STARTUP_MODE = os.getenv("MATCH_STARTUP", "latest-offset")
BOUNDEDNESS_SECONDS = os.getenv("MATCH_BOUNDEDNESS_SEC", "10")
PRE_SECONDS = os.getenv("MATCH_PRE_SEC", "60")
POST_SECONDS = os.getenv("MATCH_POST_SEC", "120")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("MATCH_TTL_SECONDS", "600"))
RECENT_KEY = os.getenv("MATCH_RECENT_KEY", "rides:matches:recent")


class RedisMatchWriter(MapFunction):
    def __init__(self, host: str, port: int, recent_key: str, ttl_s: int) -> None:
        self._host = host
        self._port = port
        self._recent_key = recent_key
        self._ttl_s = ttl_s
        self._client = None

    def _conn(self):
        if self._client is None:
            self._client = redis.Redis(host=self._host, port=self._port, decode_responses=True)
        return self._client

    def map(self, value):
        # value = Row[ride_id, driver_id, match_time, h3_cell]
        ride_id = value[0]
        driver_id = value[1]
        match_time = value[2]
        h3_cell = value[3]
        match_time_ms = int(match_time.timestamp() * 1000)

        per_ride_key = f"rides:matches:{ride_id}"
        recent_member = f"{ride_id}:{driver_id}"

        pipe = self._conn().pipeline()
        pipe.zadd(per_ride_key, {driver_id: match_time_ms})
        pipe.expire(per_ride_key, self._ttl_s)
        pipe.zadd(self._recent_key, {recent_member: match_time_ms})
        pipe.expire(self._recent_key, self._ttl_s)
        pipe.hset(
            f"rides:matches:{ride_id}:meta",
            mapping={"h3_cell": h3_cell or "", "last_match_ms": match_time_ms},
        )
        pipe.expire(f"rides:matches:{ride_id}:meta", self._ttl_s)
        pipe.execute()
        return f"match ride={ride_id} driver={driver_id} h3={h3_cell} t={match_time_ms}"


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

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
    'topic'                             = '{REQUEST_TOPIC}',
    'properties.bootstrap.servers'      = '{KAFKA_BOOTSTRAP}',
    'properties.group.id'               = '{CONSUMER_GROUP}-req',
    'scan.startup.mode'                 = '{STARTUP_MODE}',
    'format'                            = 'avro-confluent',
    'avro-confluent.url'                = '{SR_URL}'
)
""")

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
    'topic'                             = '{LOCATION_TOPIC}',
    'properties.bootstrap.servers'      = '{KAFKA_BOOTSTRAP}',
    'properties.group.id'               = '{CONSUMER_GROUP}-loc',
    'scan.startup.mode'                 = '{STARTUP_MODE}',
    'format'                            = 'avro-confluent',
    'avro-confluent.url'                = '{SR_URL}'
)
""")

    # Event-time interval join: driver pings that land inside
    # [request - PRE, request + POST] and share the pickup cell are match
    # candidates. Flink keeps state only across the interval bounds, so memory
    # grows with join throughput, not total history.
    matches = t_env.sql_query(f"""
SELECT
    r.payload.ride_id     AS ride_id,
    d.payload.driver_id   AS driver_id,
    d.event_time          AS match_time,
    r.payload.pickup.h3_cell AS h3_cell
FROM ride_request AS r, driver_location AS d
WHERE r.payload.pickup.h3_cell = d.payload.h3_cell
  AND d.event_time BETWEEN r.event_time - INTERVAL '{PRE_SECONDS}' SECOND(3)
                       AND r.event_time + INTERVAL '{POST_SECONDS}' SECOND(3)
""")

    ds = t_env.to_data_stream(matches)
    ds \
        .map(RedisMatchWriter(REDIS_HOST, REDIS_PORT, RECENT_KEY, REDIS_TTL), Types.STRING()) \
        .name("redis-matches") \
        .print()

    env.execute("ride-matching")


if __name__ == "__main__":
    main()
