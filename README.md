# RideFlow

A local Kafka + Flink learning lab. Simulates a ride-hailing platform's event
pipeline end-to-end so you can see and tune every moving piece.

## What's in the box

A Docker Compose stack with:
- **Kafka** (3 brokers, KRaft) — event log, source of truth
- **Schema Registry** — Avro schemas with BACKWARD compatibility
- **Kafka Connect + Debezium** — CDC from Postgres
- **Flink** (1 JM, 2 TMs, RocksDB) — stream processing
- **Redis** — hot-path materialized views
- **Postgres** — durable sink + CDC source
- **Ingest / Query APIs** (FastAPI) — write and read paths
- **Simulator** (Python) — synthetic driver/ride load
- **Kafka UI** — visualize partitions, lag, ISR

Everything is tunable via env vars.

## Quickstart

```bash
cp .env.example .env
make up                  # start everything
make register-schemas    # register Avro schemas to SR (once brokers are up)
make submit-hot-zones    # submit the first Flink job
make smoke               # end-to-end sanity check
make ps                  # see service health
make logs SVC=ingest-api # tail one service
make reset               # full teardown including volumes
```

## Ports

| Service          | Port              | URL                            |
| ---------------- | ----------------- | ------------------------------ |
| Ingest API       | 8000              | http://localhost:8000          |
| Query API        | 8001              | http://localhost:8001          |
| Kafka UI         | 8080              | http://localhost:8080          |
| Schema Registry  | 8081              | http://localhost:8081          |
| Flink JobManager | 8082              | http://localhost:8082          |
| Kafka Connect    | 8083              | http://localhost:8083          |
| Kafka brokers    | 9092 / 9093 / 9094 | (external listeners)          |
| Postgres         | 5432              | `postgres://rideflow:rideflow@localhost:5432/rideflow` |
| Redis            | 6379              | `redis://localhost:6379/0`     |

## Status

Phase 0.1 complete — directory tree + stubs. See checklist for next step.
# rideflow
