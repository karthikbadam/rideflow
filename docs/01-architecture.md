# 01 — Architecture & Data Flow

**Status:** locked
**Project:** RideFlow — a local Kafka + Flink learning lab

## 1. Purpose

A local, end-to-end streaming lab that simulates a ride-sharing city so you
can learn Kafka + Flink internals by building, breaking, and tuning a real
pipeline. The point is not to build a product. The point is to make every
abstraction physically observable on your laptop.

## 2. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Delivery semantics | At-least-once end-to-end | Feel duplicates firsthand before reaching for 2PC |
| CDC | Debezium (Postgres → Kafka) from day one | #1 real-world Kafka pattern |
| Orchestration | Docker Compose, no Kubernetes | You want to see containers, not abstractions |
| Observability | Kafka UI + Flink UI + CLI only | Minimal to start; add Prometheus later if wanted |
| State backend | RocksDB from day one | Disk-backed, realistic, enables large state |
| Schema format | Avro + Confluent Schema Registry | See doc 02 |
| Env topology | Single Compose file, all local | Simplicity wins until it doesn't |

## 3. Component map

```
┌──────────────┐   HTTP    ┌──────────────┐  produce   ┌─────────────┐
│  Simulator   │──────────▶│  Ingest API  │───────────▶│   Kafka     │
│  (python)    │   POST    │  (FastAPI)   │   Avro     │  3 brokers  │
└──────────────┘  events   └──────────────┘            │   KRaft     │
                                  │                    └──────┬──────┘
                                  │ validates via             │
                                  ▼                           │
                           ┌──────────────┐         consume   │
                           │Schema Reg.   │                   ▼
                           │(confluent)   │            ┌─────────────┐
                           └──────────────┘            │    Flink    │
                                                       │ JM + 2 TMs  │
                                                       │ RocksDB     │
                                                       └──┬───────┬──┘
                                                          │       │
                                                          │       │
                                              hot state   │       │  durable
                                                          ▼       ▼
                                                   ┌─────────┐ ┌──────────┐
                                                   │  Redis  │ │ Postgres │
                                                   └────┬────┘ └────┬─────┘
                                                        │           │
                                                        │  read     │
                                                        ▼           ▼
                                                   ┌──────────────────┐
                                                   │   Query API      │◀── you
                                                   │   (FastAPI)      │  (curl)
                                                   └──────────────────┘

                   ┌──────────────┐     CDC      ┌─────────────┐
                   │ Postgres     │─────────────▶│   Kafka     │
                   │ (rides,      │   Debezium   │ dbz.* topics│
                   │  drivers)    │              │             │
                   └──────────────┘              └─────────────┘
```

## 4. Why each component exists

| Component | Teaches you |
|---|---|
| Simulator | Realistic load, late/duplicate/malformed event generation |
| Ingest API | Schema validation at edge, producer tuning, backpressure |
| Schema Registry | Compatibility modes, contract evolution, Avro wire format |
| Kafka (3 brokers) | Partitions, replication, ISR, leader election, consumer groups |
| Debezium | Change Data Capture, transaction log tailing, snapshots |
| Flink (1 JM, 2 TMs) | State, watermarks, windows, checkpointing, key groups |
| Redis | Materialized views, hot path reads, TTL, eviction |
| Postgres | Durable sink, analytical queries, CDC source |
| Query API | CQRS, cache-first reads, fallback patterns |
| Kafka UI | Visualize partitions, lag, offsets, ISR |

## 5. Topics (locked)

| Topic | Key | Partitions | Retention | Purpose |
|---|---|---|---|---|
| `driver.location.v1` | `driver_id` | 12 | 1h | High-volume GPS pings |
| `ride.request.v1` | `rider_id` | 6 | 24h | User intent events |
| `ride.lifecycle.v1` | `ride_id` | 6 | 7d | Ride state transitions |
| `anomaly.detected.v1` | `entity_id` | 3 | 30d | Flink-produced alerts |
| `dbz.public.drivers` | pk | 3 | 7d | CDC stream of drivers table |
| `dbz.public.rides` | pk | 6 | 7d | CDC stream of rides table |
| `*.dlq` | — | 1 | 30d | Per-topic dead-letter queues |
| `_schemas` | — | 1 | compacted | Schema Registry internal |

## 6. State locations

| State | Lives in | Notes |
|---|---|---|
| Event log (source of truth) | Kafka | Durable, replayable |
| In-flight windows, keyed state | Flink (RocksDB on local disk) | Checkpointed to local volume |
| Current driver location | Redis hash with TTL | Overwritten on each ping |
| Current zone surge | Redis string | Overwritten per window emit |
| Driver state (idle/active/on_ride) | Redis string | From session window job |
| Completed rides | Postgres | Durable, queryable |
| Driver registry | Postgres | CDC source |

**State living in multiple places is the single biggest source of streaming
bugs.** We will deliberately break consistency as an experiment.

## 7. Write path (hot)

1. Simulator generates a `DriverLocationPinged` event (with envelope).
2. POSTs JSON to Ingest API `/events/driver-location`.
3. Ingest API:
   a. Validates against Avro schema (fetched from Schema Registry, cached)
   b. Assigns `event_id`, `ingested_at`, `trace_id` if missing
   c. Serializes to Avro binary with Confluent wire format (magic byte + schema id + payload)
   d. Produces to `driver.location.v1`, keyed by `driver_id`
4. Kafka replicates to 3 brokers; we require `min.insync.replicas=2`.
5. Flink consumes, updates RocksDB state, writes materialized view to Redis.

## 8. Read path (query)

1. Client hits Query API, e.g. `GET /zones/hot`.
2. Query API reads from Redis (sub-millisecond).
3. Falls back to Postgres for historical queries.
4. Kafka and Flink are never in the read path.

This is CQRS in a streaming context: writes go one way, reads go another.

## 9. CDC path (Debezium)

1. Postgres configured with `wal_level=logical`.
2. Debezium connector tails the WAL.
3. Each INSERT/UPDATE/DELETE on a watched table becomes a Kafka event on
   `dbz.<schema>.<table>`.
4. Flink can consume these streams to join operational DB state with live events.

Concrete use in our lab: enrich ride events with driver profile data by
joining `ride.request.v1` with `dbz.public.drivers`.

## 10. Failure modes we'll deliberately trigger

1. Kill a Kafka broker mid-flight → observe ISR shrink, producer retries
2. Kill a Flink TaskManager → observe checkpoint recovery, key group reassignment
3. Publish an event that violates schema → observe DLQ routing
4. Publish out-of-order events → observe watermark advancement
5. Make one partition idle → observe whole-pipeline watermark stall
6. Fill Redis to `maxmemory` → observe eviction, stale materialized view
7. Drop a required field in a new schema version → observe Registry rejection
8. Induce duplicates (network retry) → feel at-least-once pain first-hand

## 11. Out of scope (deliberately)

- Multi-region, Kubernetes, authentication, cross-cluster replication
- Custom UI — Kafka UI + Flink UI + curl is enough
- Exactly-once semantics — we'll build at-least-once and understand what
  duplicates cost us, which is the point

## 12. Ports (for reference)

| Service | Port |
|---|---|
| Ingest API | 8000 |
| Query API | 8001 |
| Kafka brokers (external) | 9092, 9093, 9094 |
| Schema Registry | 8081 |
| Kafka Connect (Debezium) | 8083 |
| Kafka UI | 8080 |
| Flink JobManager UI | 8082 |
| Redis | 6379 |
| Postgres | 5432 |
