# 05 — Claude Code Handoff Brief

**Purpose:** hand this file (and docs 01–04) to Claude Code. It contains
everything needed to scaffold the RideFlow repo interactively.

---

## Context for Claude Code

You are helping set up **RideFlow**, a local Kafka + Flink learning lab.
The user has deliberately chosen to build this up collaboratively rather
than have it delivered pre-made, because the goal is learning.

Read `docs/01-architecture.md`, `docs/02-schemas.md`, `docs/03-tuning.md`,
and `docs/04-checklist.md` before starting. All major decisions are locked;
do not re-litigate them.

## Your job

Scaffold the repo **incrementally** with the user. Work in the order of
Phase 0 and Phase 1 from `docs/04-checklist.md`. Do not try to build
everything at once.

After each step, stop and let the user run/inspect/ask questions.

## Directory layout to create

```
rideflow/
├── docker-compose.yml
├── .env.example
├── Makefile                    # convenience targets: up, down, reset, test
├── README.md                   # quickstart and pointers to docs
│
├── docs/                       # (user has already provided these)
│   ├── 01-architecture.md
│   ├── 02-schemas.md
│   ├── 03-tuning.md
│   ├── 04-checklist.md
│   └── 05-claude-code-brief.md
│
├── schemas/
│   ├── common/
│   │   ├── Envelope.avsc
│   │   └── GeoPoint.avsc
│   ├── driver_location_pinged.avsc
│   ├── ride_requested.avsc
│   ├── ride_lifecycle_changed.avsc
│   ├── anomaly_detected.avsc
│   ├── dlq_record.avsc
│   └── register.sh             # POST each schema to Schema Registry
│
├── ingest-api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── app/
│       ├── main.py             # FastAPI app
│       ├── config.py           # all tunable Kafka producer settings
│       ├── producers.py        # avro-serializing producer
│       ├── schemas.py          # pydantic models for request validation
│       └── h3util.py           # lat/lon -> h3 cell
│
├── query-api/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── redis_client.py
│       └── pg_client.py
│
├── simulator/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # event generator loop
│       ├── config.py           # eps, drivers count, % late, % dup, % malformed
│       ├── city.py             # bounded geo region, H3 cells, driver personas
│       └── events.py           # event construction
│
├── flink-jobs/
│   ├── Dockerfile              # PyFlink runtime with extra deps
│   ├── requirements.txt
│   └── jobs/
│       ├── 01_hot_zones.py
│       ├── 02_surge_pricing.py
│       ├── 03_idle_detector.py
│       ├── 04_ride_matching.py
│       └── 05_anomaly_detection.py
│
├── connect/
│   ├── Dockerfile              # Kafka Connect + Debezium plugin
│   └── connectors/
│       └── debezium-postgres.json
│
├── postgres/
│   └── init/
│       ├── 01-wal-logical.sql  # enable logical replication
│       ├── 02-schema.sql       # rides, drivers tables
│       └── 03-seed.sql         # a few rows so CDC has something to emit
│
├── experiments/
│   └── README.md               # experiment template + instructions
│
└── scripts/
    ├── reset.sh                # full teardown including volumes
    ├── smoke.sh                # end-to-end sanity check
    └── tools.sh                # kafka-console-producer wrappers, etc.
```

## Phase 0 build order (do these first, confirm each works)

### Step 0.1 — Scaffolding and placeholders
Create the directory structure above. Create empty/stub files with TODO
comments. Create `.env.example` with reasonable defaults. Create
`README.md` linking to each doc and with a "how to run" section.

### Step 0.2 — docker-compose.yml
Build it up in this order, verifying each layer starts cleanly:

1. Kafka x3 (KRaft mode, no Zookeeper)
2. Schema Registry
3. Kafka UI (provectus/kafka-ui)
4. Redis
5. Postgres with WAL logical replication
6. Kafka Connect + Debezium
7. Flink JobManager + 2 TaskManagers (RocksDB configured)
8. Ingest API, Query API, Simulator (after those services exist)

Use healthchecks liberally. Pin image versions.

Use the port assignments from `docs/01-architecture.md` §12.

### Step 0.3 — Avro schemas + registration
Write all `.avsc` files per `docs/02-schemas.md`. Schemas must:
- Use logical types (uuid, timestamp-millis, decimal)
- Core topics use envelope + payload nested
- DLQ schema is flat
- Subject naming = TopicNameStrategy by default

Write `schemas/register.sh` to POST each to the registry with BACKWARD
compatibility. Include a `deregister.sh` companion for experiments.

### Step 0.4 — Ingest API (minimal version for Phase 1)
- FastAPI app on port 8000
- One endpoint: `POST /events/driver-location`
- Uses `confluent-kafka` library with Avro serializer pointed at Schema Registry
- Produces to `driver.location.v1` keyed by `driver_id`
- All producer settings in `config.py` with env var overrides (so tuning experiments are easy)
- Structured JSON logs with trace_id propagated from request

### Step 0.5 — Simulator (minimal version for Phase 1)
- Generates ~10 `DriverLocationPinged` events/sec by default (env var configurable)
- 50 synthetic drivers moving on a walk over a fixed SF-shaped bounding box
- POSTs to Ingest API
- Computes H3 cell client-side (or server-side — your call, document it)

### Step 0.6 — First Flink job (hot_zones)
- Consume `driver.location.v1`
- Tumbling 1-min event-time window keyed by `h3_cell`
- Count distinct drivers per cell
- Write result to Redis as sorted set: `ZADD zones:hot <count> <h3_cell>`
- TTL on the sorted set so stale cells age out

### Step 0.7 — Query API (minimal)
- FastAPI on port 8001
- `GET /zones/hot?limit=10` returns top-N from Redis
- That's it for Phase 1

### Step 0.8 — End-to-end smoke test
Write `scripts/smoke.sh` that:
1. Produces 100 events via curl to Ingest API
2. Waits 90 seconds (for windows to close)
3. Hits `GET /zones/hot` and prints result
4. Asserts non-empty response

## Rules for Claude Code

1. **Pin all versions.** No `:latest` tags. No unpinned pip deps.
2. **Expose all Kafka parameters via env vars** in `config.py` — this is how
   the user will run tuning experiments without rebuilding images.
3. **Structured logging everywhere** — JSON with `trace_id`, `event_id`,
   `duration_ms`. Use `structlog` in Python services.
4. **Do not add observability (Prometheus, Grafana, etc.)** — user explicitly
   chose minimal. Kafka UI + Flink UI + CLI only.
5. **Healthchecks in Compose** — every service, no exceptions.
6. **Keep commentary in code minimal but useful.** Every non-obvious config
   choice gets a one-line comment with "why", not "what".
7. **Stop frequently** — after each Phase 0 step, summarize what was done
   and what to run to verify. Do not chain 8 steps together.
8. **When the user hits an error, diagnose from logs first** — `docker compose
   logs <service>` and Kafka UI before changing code.

## Explicit non-goals for the scaffold

- No frontend/dashboard beyond Kafka UI and Flink UI
- No auth/auth
- No Kubernetes manifests
- No exactly-once plumbing (we chose at-least-once)
- No Prometheus/Grafana/Jaeger (user chose minimal)
- No multi-region, no cross-cluster replication

## Handoff summary to read out loud

"RideFlow is a local learning lab for Kafka + Flink built on Docker Compose.
The user has chosen: Avro + Schema Registry, BACKWARD compatibility, RocksDB
state backend, at-least-once delivery, CDC with Debezium from day one, hybrid
envelope pattern. We're building the write path first: simulator → ingest
API → Kafka (12-partition location topic) → one Flink job (hot zones
tumbling window) → Redis → query API. We stop and verify after each step."
