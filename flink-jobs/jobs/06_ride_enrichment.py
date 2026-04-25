"""CDC enrichment — ride.request.v1 × dbz.public.drivers.

Pipeline:
  ride_request  : Kafka[ride.request.v1] (Avro-Confluent)
  drivers_cdc   : Kafka[dbz.public.drivers] (Avro, Debezium ExtractNewRecordState)
                  treated as an *upsert changelog* keyed by driver_id —
                  Flink's Kafka 'upsert-kafka' connector keeps only the latest
                  row per key in state, which is exactly a materialized
                  dimension table for lookup joins.

  ride_request LEFT JOIN drivers FOR SYSTEM_TIME AS OF ... IS NOT SUPPORTED
  for upsert-kafka in 1.18; instead we use a regular LEFT JOIN with the
  upsert-kafka-sourced table. Flink internally does an incremental join with
  per-key retraction — memory bounded by the driver dimension cardinality.

Sink:
  rides:enriched:<ride_id>   HASH   ride fields + driver name/vehicle/rating

The LEFT JOIN means rides whose driver isn't (yet) in the CDC stream still
get a record written, with NULL driver fields. The right side materializes
snapshot rows on startup; subsequent INSERTs/UPDATEs/DELETEs (via Debezium)
show up as changelog events and retract/replace the keyed state.
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
DRIVERS_CDC_TOPIC = os.getenv("DRIVERS_CDC_TOPIC", "dbz.public.drivers")
CONSUMER_GROUP = os.getenv("ENRICH_GROUP", "flink-ride-enrichment")
STARTUP_MODE = os.getenv("ENRICH_STARTUP", "earliest-offset")  # want snapshot
BOUNDEDNESS_SECONDS = os.getenv("ENRICH_BOUNDEDNESS_SEC", "10")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("ENRICH_TTL_SECONDS", "600"))
KEY_PREFIX = os.getenv("ENRICH_KEY_PREFIX", "rides:enriched:")


class RedisEnrichmentWriter(MapFunction):
    """HSET rides:enriched:<ride_id> with ride + driver columns."""

    def __init__(self, host: str, port: int, prefix: str, ttl_s: int) -> None:
        self._host = host
        self._port = port
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._client = None

    def _conn(self):
        if self._client is None:
            self._client = redis.Redis(host=self._host, port=self._port, decode_responses=True)
        return self._client

    def map(self, value):
        # value = Row[ride_id, rider_id, pickup_h3, request_time,
        #             driver_id, name, vehicle_type, rating, active]
        ride_id = value[0]
        rider_id = value[1]
        pickup_h3 = value[2]
        request_time = value[3]
        driver_id = value[4]
        name = value[5]
        vehicle_type = value[6]
        rating = value[7]
        active = value[8]

        request_ms = int(request_time.timestamp() * 1000) if request_time else None
        key = f"{self._prefix}{ride_id}"
        mapping = {
            "rider_id": rider_id or "",
            "pickup_h3": pickup_h3 or "",
            "request_ms": request_ms if request_ms is not None else "",
            "driver_id": driver_id or "",
            "driver_name": name or "",
            "vehicle_type": vehicle_type or "",
            # decimal/boolean → redis strings; query-api parses as needed.
            "rating": str(rating) if rating is not None else "",
            "active": "" if active is None else ("true" if active else "false"),
            "enriched": "true" if driver_id else "false",
        }
        pipe = self._conn().pipeline()
        pipe.hset(key, mapping={k: v for k, v in mapping.items() if v != ""})
        pipe.expire(key, self._ttl_s)
        pipe.execute()
        return f"enriched ride={ride_id} driver={driver_id or '-'} veh={vehicle_type or '-'}"


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    # Ride requests — same schema shape as 02/04 (enum fields omitted).
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
    'scan.startup.mode'                 = 'latest-offset',
    'format'                            = 'avro-confluent',
    'avro-confluent.url'                = '{SR_URL}'
)
""")

    # Drivers CDC — upsert-kafka keeps one materialized row per driver_id,
    # fed by Debezium's snapshot + stream. Value schema is flat because the
    # connector config sets ExtractNewRecordState (topic value is just the
    # row plus op/ts_ms metadata columns, not a Debezium envelope).
    #
    # We declare rating as STRING because Debezium encodes NUMERIC(3,2) as
    # bytes+connect.decimal logical type, which Flink's avro-confluent format
    # can't decode to DECIMAL in 1.18. query-api can parse the string.
    t_env.execute_sql(f"""
CREATE TABLE drivers (
    driver_id     STRING,
    name          STRING,
    phone         STRING,
    vehicle_type  STRING,
    rating        STRING,
    active        BOOLEAN,
    PRIMARY KEY (driver_id) NOT ENFORCED
) WITH (
    'connector'                     = 'upsert-kafka',
    'topic'                         = '{DRIVERS_CDC_TOPIC}',
    'properties.bootstrap.servers'  = '{KAFKA_BOOTSTRAP}',
    'properties.group.id'           = '{CONSUMER_GROUP}-drv',
    'key.format'                    = 'raw',
    'value.format'                  = 'avro-confluent',
    'value.avro-confluent.url'      = '{SR_URL}',
    'value.fields-include'          = 'EXCEPT_KEY'
)
""")

    # Join: ride.request.v1 × drivers on rider's *chosen* driver. Phase-1
    # ride.request.v1 doesn't carry driver_id — the matcher (Phase 2.3)
    # assigns one downstream. So here we enrich by rider_id-mod-drivers just
    # to exercise the join; a real system would wait for a matched signal.
    # Driver id is derived as 'drv-<first-4-digits-of-rider-id>' so we hit
    # the seed rows (drv-0000..drv-0023).
    enriched = t_env.sql_query("""
SELECT
    r.payload.ride_id                 AS ride_id,
    r.payload.rider_id                AS rider_id,
    r.payload.pickup.h3_cell          AS pickup_h3,
    r.event_time                      AS request_time,
    CONCAT('drv-', SUBSTRING(r.payload.rider_id, 5, 4))  AS driver_id,
    d.name                            AS name,
    d.vehicle_type                    AS vehicle_type,
    d.rating                          AS rating,
    d.active                          AS active
FROM ride_request AS r
LEFT JOIN drivers AS d
  ON CONCAT('drv-', SUBSTRING(r.payload.rider_id, 5, 4)) = d.driver_id
""")

    ds = t_env.to_data_stream(enriched)
    ds.map(
        RedisEnrichmentWriter(REDIS_HOST, REDIS_PORT, KEY_PREFIX, REDIS_TTL),
        Types.STRING(),
    ).name("redis-enrichment").print()

    env.execute("ride-enrichment")


if __name__ == "__main__":
    main()
