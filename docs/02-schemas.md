# 02 — Schema Design

**Status:** locked

## 1. Locked decisions

| Decision | Choice |
|---|---|
| Format | Avro |
| Registry | Confluent Schema Registry |
| Compatibility mode (default) | `BACKWARD` |
| Subject naming | `TopicNameStrategy` by default; one `RecordNameStrategy` topic added later for comparison |
| Logical types | Use them: `uuid`, `timestamp-millis`, `decimal` |
| Envelope style | Hybrid — strict envelope for 4 core topics, flat for DLQ and internal |
| Wire format | Confluent (magic byte 0x00 + 4-byte schema ID + Avro payload) |

## 2. Why Avro + Registry

- Confluent's first-class format; best Kafka ecosystem support (Debezium,
  Kafka Connect, Flink Table API all speak Avro natively).
- Evolution rules are built into the spec — reader/writer schema resolution.
- `.avsc` schema definitions are JSON (readable) even though the wire data is binary.
- Schema Registry stores versions and enforces compatibility centrally.

## 3. The envelope (for core topics only)

The envelope is a reusable Avro record attached to every event on the four
core topics: `driver.location.v1`, `ride.request.v1`, `ride.lifecycle.v1`,
`anomaly.detected.v1`.

```json
{
  "type": "record",
  "name": "Envelope",
  "namespace": "rideflow.common",
  "fields": [
    {"name": "event_id",      "type": {"type": "string", "logicalType": "uuid"}},
    {"name": "event_type",    "type": "string"},
    {"name": "event_version", "type": "int",  "default": 1},
    {"name": "occurred_at",   "type": {"type": "long",   "logicalType": "timestamp-millis"}},
    {"name": "ingested_at",   "type": {"type": "long",   "logicalType": "timestamp-millis"}},
    {"name": "producer",      "type": "string"},
    {"name": "trace_id",      "type": {"type": "string", "logicalType": "uuid"}}
  ]
}
```

DLQ topics and internal/ops topics use flat schemas — metadata fields are
fewer and needs are simpler there.

## 4. Core schemas

Each core topic has a schema of the shape:

```
<EventName> record
├── envelope: rideflow.common.Envelope
└── payload: <EventName>Payload (record)
```

### 4.1 `DriverLocationPinged` — `driver.location.v1`

Payload fields:
- `driver_id: string`
- `lat: double`
- `lon: double`
- `h3_cell: string` (precomputed at Ingest API, resolution 9)
- `heading_degrees: ["null", "float"]` default null
- `speed_mps:       ["null", "float"]` default null
- `accuracy_meters: ["null", "float"]` default null

Design notes:
- Key is `driver_id` → all events for a driver go to the same partition.
- `h3_cell` is computed once at the edge; every downstream job reuses it.
- Optional fields use nullable unions so old producers without those fields
  remain compatible (BACKWARD-safe).

### 4.2 `RideRequested` — `ride.request.v1`

Payload fields:
- `ride_id:    {type: string, logicalType: uuid}` — client-generated for idempotency
- `rider_id:   string`
- `pickup:     rideflow.common.GeoPoint`
- `dropoff:    rideflow.common.GeoPoint`
- `product_tier: enum {economy, comfort, premium}`
- `requested_at: timestamp-millis`

Design note: client-generated `ride_id` makes the POST idempotent. A retried
request hashes to the same key, and Flink can deduplicate.

### 4.3 `RideLifecycleChanged` — `ride.lifecycle.v1`

Payload fields:
- `ride_id:    uuid`
- `from_state: enum RideState`
- `to_state:   enum RideState`  values: `requested, matched, started, completed, cancelled`
- `driver_id:  ["null", string]`
- `reason:     ["null", string]`
- `changed_at: timestamp-millis`

Design note: this is a **transition event**, not a snapshot. Storing
transitions lets you rebuild any state; snapshots lose information.

### 4.4 `AnomalyDetected` — `anomaly.detected.v1`

Payload fields:
- `anomaly_id:   uuid`
- `anomaly_type: enum {teleport, speed_violation, ghost_driver, dup_event, clock_skew}`
- `entity_type:  enum {driver, rider, ride}`
- `entity_id:    string`
- `severity:     enum {low, medium, high, critical}`
- `evidence:     map<string>`
- `detected_at:  timestamp-millis`

### 4.5 Shared types

```
GeoPoint record
├── lat: double
├── lon: double
└── h3_cell: ["null", string] default null
```

## 5. Compatibility rules (with `BACKWARD` default)

| Change | Allowed? |
|---|---|
| Add optional field with default | Yes |
| Remove field that had a default | Yes |
| Add required field | No |
| Change field type | No |
| Rename field (use `aliases`) | Yes |

Planned evolution experiments (see doc 03 for details): safe add, safe rename,
unsafe change, required-field trap, v2 breaking change with dual-write.

## 6. Key design — the hidden performance lever

Kafka partitioning is `hash(key) % partition_count`. Key choice determines:
- Throughput ceiling (one partition = one consumer thread max)
- Ordering (only within a partition)
- Skew (one hot key = one overloaded partition)

| Topic | Key | Reasoning |
|---|---|---|
| `driver.location.v1` | `driver_id` | 500 drivers → good spread over 12 partitions |
| `ride.request.v1` | `rider_id` | Many riders → great spread |
| `ride.lifecycle.v1` | `ride_id` | Per-ride ordering essential |
| `anomaly.detected.v1` | `entity_id` | Co-locates anomalies per entity |

Experiment: change the location key to `h3_cell` and watch what happens
when many drivers cluster downtown. Hot partition. Consumer lag spike.

## 7. DLQ strategy

For every core topic, a corresponding `<topic>.dlq`:
- Single partition, 30-day retention
- Flat schema (no envelope) because DLQ consumers should not assume structure:
  - `original_topic: string`
  - `original_key_bytes: bytes`
  - `original_value_bytes: bytes`
  - `failure_reason: string`
  - `failure_stage: enum {deserialize, validate, business, sink}`
  - `failed_at: timestamp-millis`
  - `original_headers: map<string>`

A generic DLQ reader consumer is a good learning exercise — it must not crash
on anything, including its own failures.

## 8. Subject naming strategy

- Default: `TopicNameStrategy` → subject = `<topic>-value` and `<topic>-key`
- Later: one topic `ride.events.v1` uses `RecordNameStrategy` with multiple
  event types on the same topic (e.g., both `RideRequested` and
  `RideLifecycleChanged`). This preserves cross-event ordering per `ride_id`
  at the cost of consumer complexity. Pure learning experiment.

## 9. Logical types in practice

- UUIDs stored as strings on the wire but deserialized as UUID in the app
- Timestamps as `long` millis, deserialized as native datetime
- `decimal(precision, scale)` for any money field (fare, surge multiplier)

This forces you to write correct serializer/deserializer plumbing from
day one — a lesson you'd otherwise skip.
