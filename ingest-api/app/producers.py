"""Avro-serializing Kafka producer for driver.location.v1.

We serialize by hand rather than via AvroSerializer so we can resolve schema
references (Envelope / GeoPoint) against Schema Registry at startup and cache
a fastavro parsed schema. Wire format is the standard Confluent one:
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
        self.topic = settings.location_topic
        self.sr = SchemaRegistryClient({"url": settings.schema_registry_url})
        self.producer = Producer(build_producer_config(settings))

        subject = f"{self.topic}-value"
        rs = self.sr.get_latest_version(subject)
        self.schema_id = rs.schema_id

        named: dict = {}
        self._load_refs(rs.schema.references, named)
        self.parsed_schema = fastavro.parse_schema(
            json.loads(rs.schema.schema_str), named_schemas=named
        )
        log.info(
            "producer_ready",
            topic=self.topic,
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

    def _serialize_value(self, value: dict) -> bytes:
        buf = io.BytesIO()
        buf.write(struct.pack(">bI", _MAGIC_BYTE, self.schema_id))
        fastavro.schemaless_writer(buf, self.parsed_schema, value)
        return buf.getvalue()

    def produce_location(self, key: str, value: dict) -> None:
        self.producer.produce(
            topic=self.topic,
            key=key.encode("utf-8"),
            value=self._serialize_value(value),
            on_delivery=self._on_delivery,
        )
        # poll(0) services delivery callbacks from prior produces without blocking.
        self.producer.poll(0)

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
