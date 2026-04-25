"""Anomaly detection — impossible driver speed + ghost-driver timers.

Pipeline:
  Kafka[driver.location.v1] (Avro-Confluent, SR-resolved refs)
    → watermark on envelope.occurred_at
    → key_by(driver_id) → KeyedProcessFunction
      - ValueState[last_ping: (lat, lon, event_time_ms)]
      - ValueState[ghost_timer_ms]
      - On each ping:
          * Haversine(prev, curr) / dt → speed_mps
          * if speed > SPEED_MPS_THRESHOLD → emit 'speed_violation'
          * reset event-time timer at now + GHOST_GAP_MS
      - on_timer(): emit 'ghost_driver' if no new ping has reset state
    → sinks:
        * Kafka[anomaly.detected.v1] (Confluent Avro, manual fastavro wire)
        * Redis: ZADD anomalies:recent <detected_at_ms> <anomaly_id>
                 HSET anomalies:<anomaly_id> type severity evidence...

ProcessFunction + timers is the textbook Flink pattern for 'something should
have happened but didn't' — you register a watermark-driven timer on each
arrival, and on_timer fires only if the watermark advances past it *without*
a new ping resetting it.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import struct
import sys
import uuid

import fastavro
import redis
from confluent_kafka import Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from pyflink.common import Row, Types
from pyflink.common.typeinfo import RowTypeInfo
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.functions import KeyedProcessFunction, MapFunction
from pyflink.datastream.state import ValueStateDescriptor
from pyflink.table import StreamTableEnvironment


KAFKA_BOOTSTRAP = os.getenv(
    "KAFKA_BOOTSTRAP_SERVERS",
    "kafka-0:29092,kafka-1:29092,kafka-2:29092",
)
SR_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry:8081")
LOCATION_TOPIC = os.getenv("LOCATION_TOPIC", "driver.location.v1")
ANOMALY_TOPIC = os.getenv("ANOMALY_TOPIC", "anomaly.detected.v1")
CONSUMER_GROUP = os.getenv("ANOMALY_GROUP", "flink-anomaly-detection")
STARTUP_MODE = os.getenv("ANOMALY_STARTUP", "latest-offset")
BOUNDEDNESS_SECONDS = os.getenv("ANOMALY_BOUNDEDNESS_SEC", "10")
SPEED_MPS_THRESHOLD = float(os.getenv("ANOMALY_SPEED_MPS", "50"))   # ~180 km/h
GHOST_GAP_MIN = int(os.getenv("ANOMALY_GHOST_GAP_MIN", "5"))
PRODUCER_NAME = os.getenv("ANOMALY_PRODUCER", "flink-anomaly")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TTL = int(os.getenv("ANOMALY_TTL_SECONDS", "3600"))
RECENT_KEY = os.getenv("ANOMALY_RECENT_KEY", "anomalies:recent")

_EARTH_R_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


# Emitted row type from the detector — kept flat to avoid PyFlink row-type
# inference pitfalls around nested rows + None.
ANOMALY_ROW = RowTypeInfo(
    [
        Types.STRING(),   # anomaly_id
        Types.STRING(),   # anomaly_type
        Types.STRING(),   # driver_id (entity_id)
        Types.STRING(),   # severity
        Types.LONG(),     # detected_at_ms
        Types.DOUBLE(),   # observed_speed_mps (0 for ghost_driver)
        Types.DOUBLE(),   # prev_lat
        Types.DOUBLE(),   # prev_lon
        Types.DOUBLE(),   # curr_lat
        Types.DOUBLE(),   # curr_lon
        Types.LONG(),     # gap_ms (dt between pings, or silence duration for ghost)
    ],
    [
        "anomaly_id",
        "anomaly_type",
        "driver_id",
        "severity",
        "detected_at_ms",
        "observed_speed_mps",
        "prev_lat",
        "prev_lon",
        "curr_lat",
        "curr_lon",
        "gap_ms",
    ],
)


class AnomalyDetector(KeyedProcessFunction):
    """Per-driver teleport + ghost-driver detector.

    State layout per driver_id:
      last_ping  (ValueState): prev lat/lon/event_time_ms — powers Haversine
                               speed calc on the next ping
      ghost_tm   (ValueState): registered timer ts; delete-then-reregister on
                               each ping. Timer fires only if the watermark
                               advances past it *without* a reset.
    """

    def __init__(self, speed_threshold_mps: float, ghost_gap_ms: int) -> None:
        self._speed_threshold = speed_threshold_mps
        self._ghost_gap_ms = ghost_gap_ms
        self._last_ping = None
        self._ghost_tm = None

    def open(self, runtime_context) -> None:
        last_type = Types.ROW_NAMED(
            ["lat", "lon", "ts_ms"],
            [Types.DOUBLE(), Types.DOUBLE(), Types.LONG()],
        )
        self._last_ping = runtime_context.get_state(
            ValueStateDescriptor("last_ping", last_type)
        )
        self._ghost_tm = runtime_context.get_state(
            ValueStateDescriptor("ghost_tm", Types.LONG())
        )

    def process_element(self, value, ctx):
        # value = Row(driver_id, lat, lon, event_time_ts)
        driver_id = value[0]
        lat = float(value[1])
        lon = float(value[2])
        ts_ms = int(value[3].timestamp() * 1000)

        prev = self._last_ping.value()
        if prev is not None:
            prev_lat = float(prev[0])
            prev_lon = float(prev[1])
            prev_ts = int(prev[2])
            dt_ms = ts_ms - prev_ts
            if dt_ms > 0:
                dist_m = _haversine_m(prev_lat, prev_lon, lat, lon)
                speed = dist_m / (dt_ms / 1000.0)
                if speed > self._speed_threshold:
                    severity = "critical" if speed > self._speed_threshold * 2 else "high"
                    yield Row(
                        str(uuid.uuid4()),
                        "speed_violation",
                        driver_id,
                        severity,
                        ts_ms,
                        speed,
                        prev_lat,
                        prev_lon,
                        lat,
                        lon,
                        int(dt_ms),
                    )

        self._last_ping.update(Row(lat=lat, lon=lon, ts_ms=ts_ms))

        # Reset ghost timer — delete prior (if any) and register new.
        prev_timer = self._ghost_tm.value()
        if prev_timer is not None:
            ctx.timer_service().delete_event_time_timer(int(prev_timer))
        new_timer = ts_ms + self._ghost_gap_ms
        ctx.timer_service().register_event_time_timer(new_timer)
        self._ghost_tm.update(new_timer)

    def on_timer(self, timestamp, ctx):
        # Reached silence threshold without a reset → ghost.
        last = self._last_ping.value()
        if last is None:
            return
        driver_id = ctx.get_current_key()
        prev_lat = float(last[0])
        prev_lon = float(last[1])
        prev_ts = int(last[2])
        yield Row(
            str(uuid.uuid4()),
            "ghost_driver",
            driver_id,
            "medium",
            int(timestamp),
            0.0,
            prev_lat,
            prev_lon,
            prev_lat,
            prev_lon,
            int(timestamp - prev_ts),
        )
        # Don't re-arm; a new ping will recreate state + timer.
        self._ghost_tm.clear()


class AnomalySink(MapFunction):
    """Emit to anomaly.detected.v1 (Avro-Confluent) + Redis.

    fastavro handles the Avro enum fields natively when we pass enum symbols
    as Python strings; this sidesteps the avro-confluent read-side enum↔string
    coercion gap we hit in surge/matching.
    """

    _MAGIC = 0x00

    def __init__(
        self,
        bootstrap: str,
        sr_url: str,
        topic: str,
        redis_host: str,
        redis_port: int,
        recent_key: str,
        ttl_s: int,
        producer_name: str,
    ) -> None:
        self._bootstrap = bootstrap
        self._sr_url = sr_url
        self._topic = topic
        self._redis_host = redis_host
        self._redis_port = redis_port
        self._recent_key = recent_key
        self._ttl_s = ttl_s
        self._producer_name = producer_name
        self._schema_id = None
        self._parsed = None
        self._kafka = None
        self._redis = None

    def _init(self) -> None:
        sr = SchemaRegistryClient({"url": self._sr_url})
        rs = sr.get_latest_version(f"{self._topic}-value")
        named: dict = {}
        self._load_refs(sr, rs.schema.references, named)
        self._parsed = fastavro.parse_schema(
            json.loads(rs.schema.schema_str), named_schemas=named
        )
        self._schema_id = rs.schema_id
        self._kafka = Producer(
            {
                "bootstrap.servers": self._bootstrap,
                "enable.idempotence": True,
                "acks": "all",
                "client.id": self._producer_name,
            }
        )
        self._redis = redis.Redis(
            host=self._redis_host, port=self._redis_port, decode_responses=True
        )

    def _load_refs(self, sr, refs, named: dict) -> None:
        for ref in refs or []:
            ref_rs = sr.get_version(ref.subject, ref.version)
            self._load_refs(sr, ref_rs.schema.references, named)
            fastavro.parse_schema(
                json.loads(ref_rs.schema.schema_str), named_schemas=named
            )

    def _serialize(self, value: dict) -> bytes:
        buf = io.BytesIO()
        buf.write(struct.pack(">bI", self._MAGIC, self._schema_id))
        fastavro.schemaless_writer(buf, self._parsed, value)
        return buf.getvalue()

    def map(self, value):
        if self._kafka is None:
            self._init()

        (
            anomaly_id,
            anomaly_type,
            driver_id,
            severity,
            detected_at_ms,
            observed_speed,
            prev_lat,
            prev_lon,
            curr_lat,
            curr_lon,
            gap_ms,
        ) = (value[i] for i in range(11))

        evidence = {
            "observed_speed_mps": f"{observed_speed:.3f}",
            "prev_lat": f"{prev_lat:.6f}",
            "prev_lon": f"{prev_lon:.6f}",
            "curr_lat": f"{curr_lat:.6f}",
            "curr_lon": f"{curr_lon:.6f}",
            "gap_ms": str(int(gap_ms)),
        }

        envelope = {
            "event_id": anomaly_id,
            "event_type": "AnomalyDetected",
            "event_version": 1,
            "occurred_at": int(detected_at_ms),
            "ingested_at": int(detected_at_ms),
            "producer": self._producer_name,
            "trace_id": anomaly_id,
        }
        payload = {
            "anomaly_id": anomaly_id,
            "anomaly_type": anomaly_type,
            "entity_type": "driver",
            "entity_id": driver_id,
            "severity": severity,
            "evidence": evidence,
            "detected_at": int(detected_at_ms),
        }
        record = {"envelope": envelope, "payload": payload}
        self._kafka.produce(
            topic=self._topic,
            key=driver_id.encode("utf-8"),
            value=self._serialize(record),
        )
        self._kafka.poll(0)

        pipe = self._redis.pipeline()
        pipe.zadd(self._recent_key, {anomaly_id: int(detected_at_ms)})
        pipe.expire(self._recent_key, self._ttl_s)
        detail_key = f"anomalies:{anomaly_id}"
        pipe.hset(
            detail_key,
            mapping={
                "anomaly_type": anomaly_type,
                "driver_id": driver_id,
                "severity": severity,
                "detected_at_ms": int(detected_at_ms),
                **evidence,
            },
        )
        pipe.expire(detail_key, self._ttl_s)
        pipe.execute()
        return f"anomaly {anomaly_type} driver={driver_id} speed={observed_speed:.1f} gap_ms={gap_ms}"


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
    > NOT NULL,
    payload ROW<
        driver_id        STRING,
        lat              DOUBLE,
        lon              DOUBLE,
        h3_cell          STRING,
        heading_degrees  DOUBLE,
        speed_mps        DOUBLE,
        accuracy_meters  DOUBLE
    > NOT NULL,
    event_time AS envelope.occurred_at,
    WATERMARK FOR event_time AS event_time - INTERVAL '{BOUNDEDNESS_SECONDS}' SECOND
) WITH (
    'connector'                         = 'kafka',
    'topic'                             = '{LOCATION_TOPIC}',
    'properties.bootstrap.servers'      = '{KAFKA_BOOTSTRAP}',
    'properties.group.id'               = '{CONSUMER_GROUP}',
    'scan.startup.mode'                 = '{STARTUP_MODE}',
    'format'                            = 'avro-confluent',
    'avro-confluent.url'                = '{SR_URL}'
)
""")

    # Project to flat row before shipping to DataStream — the detector doesn't
    # need the envelope, and a flat Row matches Python row-type inference
    # better than nested ROW<>.
    pings = t_env.sql_query("""
SELECT
    payload.driver_id AS driver_id,
    payload.lat       AS lat,
    payload.lon       AS lon,
    event_time
FROM driver_location
WHERE payload.driver_id IS NOT NULL
""")

    ping_type = Types.ROW_NAMED(
        ["driver_id", "lat", "lon", "event_time"],
        [Types.STRING(), Types.DOUBLE(), Types.DOUBLE(), Types.SQL_TIMESTAMP()],
    )
    ds = t_env.to_data_stream(pings)

    ghost_gap_ms = GHOST_GAP_MIN * 60 * 1000
    anomalies = (
        ds.key_by(lambda r: r[0], key_type=Types.STRING())
        .process(AnomalyDetector(SPEED_MPS_THRESHOLD, ghost_gap_ms), output_type=ANOMALY_ROW)
        .name("anomaly-detector")
    )

    anomalies.map(
        AnomalySink(
            KAFKA_BOOTSTRAP,
            SR_URL,
            ANOMALY_TOPIC,
            REDIS_HOST,
            REDIS_PORT,
            RECENT_KEY,
            REDIS_TTL,
            PRODUCER_NAME,
        ),
        Types.STRING(),
    ).name("anomaly-sink").print()

    _ = ping_type  # reserved for future type-info assertions

    env.execute("anomaly-detection")


if __name__ == "__main__":
    main()
