"""Avro-serializing Kafka producer for core ingest topics.

We serialize by hand rather than via AvroSerializer so we can resolve schema
references (Envelope / GeoPoint) against Schema Registry at startup and cache
fastavro parsed schemas per topic. Wire format is the standard Confluent one:
  [magic byte 0x00][schema_id big-endian u32][fastavro-binary payload]
"""
from __future__ import annotations

import io
import json
import struct

import fastavro
import structlog
from confluent_kafka import Producer
from confluent_kafka.schema_registry import SchemaRegistryClient

from .config import Settings, build_producer_config

log = structlog.get_logger(__name__)

_MAGIC_BYTE = 0x00


class EventProducer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sr = SchemaRegistryClient({"url": settings.schema_registry_url})
        self.producer = Producer(build_producer_config(settings))
        self._topics: dict[str, tuple[int, dict]] = {}
        for topic in (settings.location_topic, settings.ride_request_topic):
            self._register(topic)

    def _register(self, topic: str) -> None:
        subject = f"{topic}-value"
        rs = self.sr.get_latest_version(subject)
        named: dict = {}
        self._load_refs(rs.schema.references, named)
        parsed = fastavro.parse_schema(
            json.loads(rs.schema.schema_str), named_schemas=named
        )
        self._topics[topic] = (rs.schema_id, parsed)
        log.info(
            "producer_ready",
            topic=topic,
            subject=subject,
            schema_id=rs.schema_id,
            schema_version=rs.version,
            refs=[r.name for r in (rs.schema.references or [])],
        )

    def _load_refs(self, refs, named: dict) -> None:
        for ref in refs or []:
            ref_rs = self.sr.get_version(ref.subject, ref.version)
            self._load_refs(ref_rs.schema.references, named)
            fastavro.parse_schema(
                json.loads(ref_rs.schema.schema_str), named_schemas=named
            )

    def _serialize_value(self, topic: str, value: dict) -> bytes:
        schema_id, schema = self._topics[topic]
        buf = io.BytesIO()
        buf.write(struct.pack(">bI", _MAGIC_BYTE, schema_id))
        fastavro.schemaless_writer(buf, schema, value)
        return buf.getvalue()

    def produce(self, topic: str, key: str, value: dict) -> None:
        self.producer.produce(
            topic=topic,
            key=key.encode("utf-8"),
            value=self._serialize_value(topic, value),
            on_delivery=self._on_delivery,
        )
        # poll(0) services delivery callbacks from prior produces without blocking.
        self.producer.poll(0)

    def produce_location(self, key: str, value: dict) -> None:
        self.produce(self.settings.location_topic, key, value)

    def produce_ride_request(self, key: str, value: dict) -> None:
        self.produce(self.settings.ride_request_topic, key, value)

    @staticmethod
    def _on_delivery(err, msg) -> None:
        if err is not None:
            log.error("delivery_failed", error=str(err))
        else:
            log.debug(
                "delivered",
                topic=msg.topic(),
                partition=msg.partition(),
                offset=msg.offset(),
            )

    def flush(self, timeout: float = 5.0) -> int:
        return self.producer.flush(timeout)
