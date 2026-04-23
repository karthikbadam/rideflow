# Satflow — Satellite Tracking Platform (greenfield)

> A new repository, provisional name **`satflow`** (rename freely: `skytrack`, `orbit-stream`, etc.). This plan is self-contained and assumes an empty repo; it does **not** modify or depend on RideFlow. Architectural patterns are borrowed from RideFlow's reference stack (Avro Envelope wrapper, BACKWARD Schema Registry compat, per-event-type topic naming with version suffix, FastAPI split between `ingest-api` and `query-api`, Flink SQL for aggregates, DLQ-per-topic, Redis hot state, Postgres cold state, structured logging + trace headers) but re-applied to a satellite-tracking domain from scratch.

## Context

Build a **real-time satellite tracking platform** that ingests **real TLE data** (Celestrak by default; N2YO / Space-Track via adapter seams), propagates orbits, publishes positions + derived events to Kafka, and serves both REST + WebSocket consumers to a Vite + React + TypeScript + Chakra UI frontend rendering Deck.gl + MapLibre. Backend includes API-key auth + rate limiting, durable Postgres persistence + replay, observability (Prometheus + OTel + Grafana + Jaeger), and a DLQ consumer — all from day 1 (no Phase 1/2 migration overhead, since nothing legacy exists).

Design priorities:

- **Event-driven by default.** Kafka is the system of record; everything else is a materialization.
- **Schema-first.** Every topic has an Avro subject registered in Schema Registry under `BACKWARD` compat. All events share a `satflow.common.Envelope` wrapper (event_id, event_version, occurred_at, ingested_at, trace_id, source, payload).
- **Adapter seams** for TLE source, auth backend, map basemap, and metrics exporters.
- **Local first, cloud-ready.** `docker compose up` brings up the whole stack on a laptop. No managed dependencies required.
- **Observable from day 1.** Metrics, traces, and structured logs are wired into every service before features land.

Target demo: open `http://localhost:5173`, see the ISS (NORAD 25544) and the active Celestrak catalog moving on a dark MapLibre globe, click a satellite, see its ground track and upcoming passes for the browser's geolocation, and watch a live alert feed for conjunctions / orbit-decay / stale-TLE anomalies.

---

## A. Avro schemas and Kafka topics

### A.1 Kafka topics

Topic naming: `<entity>.<action>.v<N>`. Partitions sized for local dev; tune up in cloud.

| Topic | Partitions | Retention | Key |
|---|---|---|---|
| `satellite.position.v1` | 12 | 24h | `norad_id` |
| `satellite.tle.v1` | 1 | compacted (30d tombstone) | `norad_id` |
| `satellite.pass.v1` | 6 | 7d | `norad_id\|observer_id` |
| `satellite.anomaly.v1` | 3 | 30d | `norad_id` |
| `satellite.position.v1.dlq` | 1 | 30d | — |
| `satellite.pass.v1.dlq` | 1 | 30d | — |
| `satellite.anomaly.v1.dlq` | 1 | 30d | — |

Topic creation is declarative: `docker-compose.yml` includes a `topic-init` one-shot container that runs `kafka-topics --create --if-not-exists` for each entry above.

### A.2 Avro schemas (`schemas/`, namespace `satflow`)

Common definitions (`satflow.common`):

- `envelope.avsc` — `event_id` (uuid string), `event_version` (int, starts at 1), `occurred_at` (ts-ms), `ingested_at` (ts-ms), `source` (string), `trace_id` (nullable string), `payload` (union of all payload records via named references).
- `geo_point.avsc` — `lat` (double), `lon` (double), `elevation_m` (nullable double).

Domain payloads (`satflow.satellite`), each wrapped by the Envelope at the topic level:

- `satellite_position_sampled.avsc` — `norad_id` (string), `name` (string), `lat`, `lon`, `alt_km` (double), `velocity_kms` (double), `healpix_cell` (long, nside=64, nested ordering), `tle_epoch` (ts-ms), `sampled_at` (ts-ms), `source` enum `{celestrak, n2yo, spacetrack}`.
- `tle_record.avsc` — `norad_id`, `name`, `line1`, `line2`, `classification`, `epoch` (ts-ms), `fetched_at` (ts-ms). Backs log-compacted `satellite.tle.v1`.
- `satellite_pass_predicted.avsc` — `norad_id`, `observer_id` (hash of lat|lon|elev), `observer` (`GeoPoint`), `rise_at`, `culminate_at`, `set_at`, `max_elevation_deg`, `rise_azimuth_deg`, `set_azimuth_deg`, `visible` (bool).
- `satellite_anomaly_detected.avsc` — `norad_id`, `anomaly_type` enum `{orbit_decay, conjunction_alert, unexpected_maneuver, tle_stale, reentry_candidate}`, `severity` enum `{info, warn, critical}`, `detected_at` (ts-ms), `evidence` (`map<string,string>`).
- `dlq_record.avsc` — `original_topic`, `failure_reason`, `failure_stage`, `failed_at` (ts-ms), `original_key` (nullable bytes), `original_value` (bytes), `headers` (`map<string,string>`). Shared across all DLQ topics.

### A.3 Schema Registry wiring

- Registry runs at `http://schema-registry:8081` with global `BACKWARD` compatibility.
- `schemas/register.sh` walks `*.avsc`, resolves `$ref`-style imports recursively, and POSTs to `/subjects/<topic>-value/versions` with `references=[{name, subject, version}]` for `envelope` / `geo_point`.
- `schemas/deregister.sh` does the inverse for teardown.
- Evolution rule: only additive changes to existing subjects (`["null", T]` with `default null`). New event types → new subject + new topic.

---

## B. TLE producer (`tle-producer/`)

Python + `uv` + async httpx service. Only producer of raw + propagated satellite state.

```
tle-producer/
  Dockerfile
  pyproject.toml        # httpx, sgp4, skyfield, astropy-healpix, structlog, pydantic-settings, opentelemetry-*
  app/
    main.py             # two asyncio loops: fetch + propagate
    config.py           # pydantic-settings
    sources/
      base.py           # TleSource Protocol + TleRecord dataclass
      celestrak.py      # default; GROUP=active at celestrak.org
      n2yo.py           # reads N2YO_API_KEY
      spacetrack.py     # reads SPACETRACK_USER/PASS
    propagate.py        # Skyfield primary; SGP4 fallback for determinism in tests
    publish.py          # bounded-concurrency httpx.AsyncClient → ingest-api
    healpix.py          # cell_for_sky(lat, lon, nside=64) via astropy_healpix
```

**Behavior**
- **TLE fetch loop**: every `TLE_REFRESH_MINUTES` (default 180 — Celestrak guidance). In-memory `dict[norad_id, Satrec]`. Also publishes raw TLEs to `satellite.tle.v1` (log-compacted → tombstones possible).
- **Propagation loop**: every `PROPAGATE_INTERVAL_S` (default 5s).
  - Watchlist (env `WATCHLIST_NORAD_IDS`, default `25544,20580,48274` = ISS, Hubble, Tiangong): emit every tick.
  - Rest of `active.txt`: emit every `BULK_CADENCE_S` (default 30s), round-robin-sliced across ticks to smooth ingest load.
- **Publish**: `POST http://ingest-api:8000/events/satellite-position` with `X-API-Key` + W3C `traceparent` + legacy `x-trace-id`. `httpx.AsyncClient` with `limits=Limits(max_keepalive_connections=50, max_connections=100)`.

Source adapter selected at startup via `TLE_SOURCE=celestrak|n2yo|spacetrack`. Rest of the code only sees `TleSource` Protocol. Secrets (`N2YO_API_KEY`, `SPACETRACK_USER/PASS`) via compose env + `.env.example`.

**Tests**: fixture TLE block → parse → propagate at a known epoch → assert (lat, lon, alt) within tolerance of a hand-computed reference.

---

## C. Ingest API (`ingest-api/`)

FastAPI service, sync-producer side of Kafka (Confluent `confluent-kafka`). Validates input, wraps in Envelope, emits to the appropriate topic.

```
ingest-api/
  Dockerfile
  pyproject.toml        # fastapi, uvicorn, confluent-kafka[avro], fastavro, httpx, asyncpg, structlog, pydantic, astropy-healpix, prometheus-fastapi-instrumentator, opentelemetry-*
  app/
    main.py             # app factory, middleware wiring, routes
    config.py
    schemas.py          # pydantic request models
    producers.py        # dict[subject → (schema_id, parsed_schema)]; Confluent wire-format serialize
    auth.py             # APIKeyMiddleware (shared copy with query-api; OK to duplicate until extracted)
    ratelimit.py        # Redis token bucket
    tracing.py          # trace_middleware + OTel bootstrap
    healpix_util.py
```

**Endpoints**
- `POST /events/satellite-position` (202) — body: `SatellitePositionIn` (`norad_id`, `name`, lat/lon bounds-checked, `alt_km >= 0`, `velocity_kms`, `healpix_cell: int | None` (server fills if absent via `healpix_util`), `tle_epoch_ms`, `sampled_at_ms` (defaults to now), `source: Literal[...]`). On success, produce to `satellite.position.v1`.
- `POST /events/tle` (202) — raw TLE upsert; produces to `satellite.tle.v1`.
- `GET /health` — liveness; excluded from auth and metrics.
- `GET /metrics` — Prometheus text.

**Producer path**
- On startup, fetch and cache schema IDs + parsed schemas for every `<topic>-value` subject the service needs (not just one), mirroring a pattern where `_load_refs` walks references recursively so Envelope + GeoPoint resolve.
- Serialize with Confluent wire format: `\x00` magic byte + 4-byte big-endian schema id + Avro binary payload.
- Producer uses `acks=all`, `enable.idempotence=true`, `linger.ms=10`, `compression.type=zstd`. `stats_cb` JSON → Prometheus gauges (msgs in flight, rtt, batch size).

**Middleware order** (request flow): `CORSMiddleware` → `trace_middleware` (W3C + legacy `x-trace-id`) → OTel ASGI → `APIKeyMiddleware` → `RateLimitMiddleware` → route. `/health` and `/metrics` skip auth and rate-limit.

---

## D. Query API (`query-api/`)

FastAPI async-consumer side. REST + WebSocket. Reads Redis for hot state, Postgres for cold replay, and runs a single long-lived `aiokafka` consumer for the WS fanout.

```
query-api/
  Dockerfile
  pyproject.toml        # fastapi, uvicorn, aiokafka, fastavro, asyncpg, redis[async], websockets, skyfield, astropy-healpix, bcrypt, prometheus-fastapi-instrumentator, opentelemetry-*
  app/
    main.py             # app factory, lifespan starts aiokafka + fanout
    config.py
    avro_codec.py       # reverse of ingest-api wire format; cache parsed schemas by id
    auth.py             # APIKeyMiddleware (shared copy)
    ratelimit.py        # Redis token bucket
    deps.py             # require_scope("read:satellites")
    redis_client.py     # typed helpers: track_positions, active_satellites, top_hot_sky_cells, recent_alerts
    routers/
      satellites.py     # /satellites/*
      passes.py         # /passes
      alerts.py         # /alerts
      zones.py          # /zones/hot  (GeoJSON of HEALPix cells)
      ws.py             # /ws/stream
    pubsub.py           # asyncio PubSub fanout: one consumer → many WS queues
    pass_compute.py     # on-demand skyfield passes for /passes lat/lon queries
```

### D.1 REST endpoints

| Method | Path | Backing store |
|---|---|---|
| `GET /satellites/active?limit=` | Redis ZSET `sats:active` (score = last-seen ms) |
| `GET /satellites/{norad_id}` | Postgres `satellites` |
| `GET /satellites/{norad_id}/track?window=5m` | Redis list `sats:track:{norad_id}`; fallback Postgres |
| `GET /zones/hot?limit=` | Redis `sky:hot:<window_end_ms>` ZSET → GeoJSON FeatureCollection of HEALPix cell polygons (polygons computed server-side once per window and cached in Redis) |
| `GET /passes?lat=&lon=&hours=24` | On-demand skyfield compute; else Redis `passes:{observer_id}` |
| `GET /alerts?since=` | Postgres `anomalies` or Redis `alerts:recent` |
| `WS /ws/stream` | Multiplexed live stream (see D.2) |

### D.2 WebSocket protocol

Client subscribes on connect:
```json
{"subscribe": ["satellite.position", "alerts"], "filter": {"norad_ids": ["25544"]}}
```
Server streams `{"topic": ..., "ts": ..., "data": {...}}` JSON frames. Heartbeat: server `ping` every 20s; client `pong` required within 60s or server closes with 1011.

Backing: single `AIOKafkaConsumer` subscribed to `satellite.position.v1` + `satellite.anomaly.v1` + `satellite.pass.v1`. Decoded via `avro_codec.py`. asyncio PubSub fans out to per-WS filtered queues. One `query-api` replica handles O(10³) concurrent WS connections comfortably.

### D.3 Auth + rate limiting

- `auth.py` — `APIKeyMiddleware` reads `X-API-Key` header, or for WS the `?api_key=...` query param or `Sec-WebSocket-Protocol: apikey.<key>`. Lookup `api_keys` by `sha256(key)` with 60s in-process TTL cache.
- `ratelimit.py` — Redis token bucket keyed by `key_id` using a Lua script on `rl:{key_id}:{window_minute}`. Reject with 429 + `Retry-After`. Per-key limit stored in `api_keys.rate_per_minute` (default 120).
- `deps.py` — `require_scope(s)` dependency factory.
- Same middleware copy applied to `ingest-api` so `tle-producer` must authenticate too. Extract into a shared `satflow_auth` package later; duplicate until the second consumer shows up.

### D.4 CORS

`CORSMiddleware` on both APIs. Origins from env `CORS_ORIGINS` (csv; default `http://localhost:5173,http://localhost:4173`). Methods `GET,POST,OPTIONS`. Headers `X-API-Key,X-Trace-Id,Content-Type`.

---

## E. Stream processing (`flink-jobs/` and `pass-worker/`)

### E.1 Flink image and deps

`flink-jobs/Dockerfile`: base `flink:1.19-scala_2.12-java17`, add Python 3.10, `pyflink==1.19.*`, `astropy-healpix`, `avro`, `fastavro`. Do **not** install `skyfield` in the Flink image — pass prediction runs in `pass-worker`. The `flink-sql-avro-confluent-registry` JAR is copied into `/opt/flink/lib/` so Avro-Confluent source/sink works natively.

### E.2 `01_hot_sky_cells.py`

Source: `satellite.position.v1`, Avro-Confluent. Watermark `envelope.occurred_at - INTERVAL '10' SECOND`.

```sql
SELECT
  payload.healpix_cell AS cell,
  window_end,
  COUNT(DISTINCT payload.norad_id) AS n_sats
FROM TABLE(TUMBLE(TABLE satellite_position, DESCRIPTOR(occurred_at), INTERVAL '1' MINUTE))
GROUP BY payload.healpix_cell, window_end
```

Sink: a `RedisSkyCellWriter` custom sink writes each row to ZSET `sky:hot:<window_end_ms>` with `zadd cell n_sats`, TTL 10 min. Query API reads the latest ZSET.

### E.3 `02_conjunction_detector.py`

Source: `satellite.position.v1`. Bucket by `(healpix_cell @ nside=32, altitude_band_50km)` via SQL. Each 10s window, for buckets with ≥2 sats compute pairwise great-circle distance via a Python UDF; if `range_km < 10` and `|Δalt_km| < 5` emit `SatelliteAnomalyDetected(type=conjunction_alert, evidence={other_norad, range_km})`.

Same job emits `orbit_decay` when `alt_km` trends monotonically downward across 6 consecutive windows with any window below 160 km, and `tle_stale` when `envelope.ingested_at - payload.tle_epoch > 7d`.

Sink: `satellite.anomaly.v1` via Avro-Confluent.

### E.4 `pass-worker/` (standalone, not Flink)

Skyfield in PyFlink UDFs is expensive and needs ephemerides on every TM. Use a dedicated Python service:

```
pass-worker/
  Dockerfile
  pyproject.toml        # aiokafka, skyfield, astropy-healpix, redis[async], structlog, pydantic-settings, opentelemetry-*
  app/
    main.py             # consumes positions; maintains per-(sat, observer) state; emits pass rows
    observers.py        # polls Redis set observers:active (populated by Query API)
    geometry.py         # skyfield elevation/azimuth computation
```

Emits `satellite.pass.v1` rows at elevation crossings `{0°, 10°, culmination, 10°, 0°}` per (sat, observer) pair.

### E.5 Submission (`Makefile`)

```
submit-flink-jobs:
	$(COMPOSE) exec -T flink-jm flink run -d -py /opt/flink/jobs/01_hot_sky_cells.py
	$(COMPOSE) exec -T flink-jm flink run -d -py /opt/flink/jobs/02_conjunction_detector.py
```

`pass-worker` is a long-running compose service, not a Flink job, so it boots with `make up`.

### E.6 Why HEALPix, not H3

HEALPix is equal-area on the sphere and standard in astronomy pipelines. `nside=64` gives ~0.92° (~100 km) cells — right-sized for a world-map heatmap. `nside=32` (~184 km) is used for conjunction bucketing. H3 is Earth-surface-oriented and less natural for orbital regimes.

---

## F. Durability, replay, DLQ

### F.1 Postgres schema (`postgres/init/02-schema.sql`)

- `satellites` — master metadata; upserted by persister on first-seen.
- `satellite_positions` — partitioned **monthly** by `sampled_at`; indexes `(norad_id, sampled_at desc)` and `(healpix_cell, sampled_at desc)`. Partition maintenance cron documented in README (create next month's partition weekly).
- `passes` — `(observer_id, rise_at)` index; `(norad_id, rise_at)` secondary.
- `anomalies` — `(detected_at desc)` + `(anomaly_type, detected_at desc)`.
- `api_keys` — `key_id uuid pk`, `key_hash bytea unique`, `owner text`, `scopes text[]`, `rate_per_minute int default 120`, `disabled_at timestamptz`.
- `rate_limit_events` — rolling audit of 429s (optional; TTL via pg_cron).
- `dlq_events` — `original_topic`, `failure_reason`, `failure_stage`, `failed_at`, `original_key bytea`, `original_value bytea`, `headers jsonb`.

### F.2 Seed (`postgres/init/03-seed.sql`)

- Dev API key: raw = `dev-key-satflow-local-only`, stored as `sha256` hex literal, owner `dev`, scopes `{read:satellites,write:events}`, 120 rpm.
- 3 seed satellites: ISS (25544), Hubble (20580), Tiangong (48274).

### F.3 `position-persister/` service

Chosen over Kafka Connect JDBC to avoid Connect plugin install:

```
position-persister/
  app/
    main.py             # aiokafka → Avro decode → asyncpg batch insert
    batcher.py          # flush on 500 rows OR 500 ms whichever first
```

`INSERT ... ON CONFLICT (norad_id, sampled_at) DO NOTHING`. Upserts `satellites` on first-seen. A sibling consumer task in the same process reads `satellite.anomaly.v1` → `anomalies` table. Consumer group id `satflow.position-persister` so Kafka tracks offsets and replay is just a group reset.

### F.4 `dlq-consumer/` service

Consumes all `*.dlq.v1` topics; decodes via `dlq_record.avsc`; writes rows to `dlq_events`. Emits structured WARN logs and, if `DLQ_ALERT_URL` is set, POSTs a compact JSON payload on a per-topic threshold (configurable `DLQ_ALERT_RATE_RPM`). Webhook body is static-shaped so Slack / PagerDuty templating works without code changes.

### F.5 Replay

Replay a single topic:
```bash
make replay TOPIC=satellite.position.v1 GROUP=satflow.replay FROM=2026-04-20T00:00:00Z
```
Runs `kafka-console-consumer --from-beginning` through the shared Avro codec, filtered by timestamp, and re-publishes via the normal ingest path. Documented in `SCHEMAS.md`.

---

## G. Observability

### G.1 Metrics (Prometheus)

`prometheus-fastapi-instrumentator` on both FastAPI services, exposed at `/metrics`:
```python
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
```

Per-service additions:
- **ingest-api**: parse `confluent_kafka.Producer.stats_cb` JSON → gauges (msgs in flight, rtt ms, batch size, internal queue depth).
- **query-api / persister / dlq-consumer / pass-worker**: aiokafka's built-in metrics.
- **kafka-exporter** container (`danielqsj/kafka-exporter`) for topic lag per consumer group — crucial for replay diagnostics.
- **redis-exporter** container for hit rate + memory.
- **Flink Prometheus reporter** — set `FLINK_PROPERTIES` in `docker-compose.yml`:
  ```
  metrics.reporters: prom
  metrics.reporter.prom.factory.class: org.apache.flink.metrics.prometheus.PrometheusReporterFactory
  metrics.reporter.prom.port: 9249
  ```
  Expose 9249 on both JM and all TMs.

### G.2 Tracing (OpenTelemetry)

Add `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `-httpx`, `-asyncpg`, `-aiokafka` (where available; else manual spans), `-exporter-otlp` to every Python service. Bootstrap in a shared `tracing.py` module copied per service (extract to `satflow_obs` package after the third service, not before).

New container `otel-collector` (`otel/opentelemetry-collector-contrib`) — OTLP gRPC `4317`, OTLP HTTP `4318`. Pipeline routes spans to Jaeger and metrics to Prometheus via OTLP receiver. Config at `observability/otel-collector.yaml`.

Propagation: W3C `traceparent`. Every service logs `trace_id` as a structured field so logs, metrics, and traces correlate. Legacy `x-trace-id` is accepted and mapped to the span attribute `satflow.trace_id` for tools that don't speak W3C yet.

### G.3 Dashboards (Grafana)

Container `grafana/grafana:11.2.0` on port 3000, admin/admin for local dev. Provisioning under `observability/grafana/`:

- `provisioning/datasources/{prometheus,jaeger}.yml`
- `provisioning/dashboards/satflow.yml` → `dashboards/satflow-overview.json`

Panels on "Satflow Overview": ingest EPS per topic; producer error rate; Kafka consumer lag per (topic, group); Flink checkpoint duration + backpressure; Query API p50/p95/p99; Redis hit rate; active WS connections (custom gauge in query-api); DLQ event rate per topic.

### G.4 Compose services

| Service | Image | Port |
|---|---|---|
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.108.0` | 4317, 4318 |
| `jaeger` | `jaegertracing/all-in-one:1.60` | 16686 |
| `prometheus` | `prom/prometheus:v2.54.1` | 9090 |
| `grafana` | `grafana/grafana:11.2.0` | 3000 |
| `kafka-exporter` | `danielqsj/kafka-exporter:v1.7.0` | 9308 |
| `redis-exporter` | `oliver006/redis_exporter:v1.62.0` | 9121 |

---

## H. Frontend (`frontend/`) — Vite + React + TS + Chakra + Deck.gl + MapLibre

### H.1 Layout

```
frontend/
  Dockerfile                    # multi-stage: node build → nginx:alpine
  nginx.conf                    # SPA fallback, gzip, /env.js passthrough
  package.json                  # pnpm
  tsconfig.json
  vite.config.ts                # dev proxy /api→query-api:8001, /ws→query-api:8001
  index.html
  src/
    main.tsx                    # Chakra provider, React Query, Router
    theme.ts                    # dark mode default
    api/
      client.ts                 # axios instance, attaches X-API-Key from env.js
      satellites.ts passes.ts alerts.ts zones.ts
      types.ts
    ws/
      useLiveStream.ts          # singleton WS; routes to store
      store.ts                  # zustand: positions Map, alerts array, stats
    components/
      Globe.tsx                 # Deck.gl overlay + react-map-gl/maplibre base
      layers/
        satellitePointsLayer.ts # ScatterplotLayer from positions Map
        groundTrackLayer.ts     # PathLayer, 90-min window around now
        hotCellsLayer.ts        # PolygonLayer from /zones/hot GeoJSON
      SatelliteList.tsx
      PassTable.tsx
      AlertFeed.tsx
      StatusBar.tsx             # WS state, server clock skew, active sat count
    pages/
      DashboardPage.tsx         # /
      SatelliteDetailPage.tsx   # /satellite/:noradId
      PassesPage.tsx            # /passes
      AlertsPage.tsx            # /alerts
    hooks/
      useObserverLocation.ts    # geolocation + manual override
    utils/
      healpixCells.ts           # client-side helpers if we want hover cell lookup
      satelliteJs.ts            # satellite.js interpolation between server ticks
  .env.example                  # VITE_QUERY_API_URL, VITE_WS_URL, VITE_API_KEY
```

### H.2 Runtime dependencies

- UI: `@chakra-ui/react`, `@emotion/react`, `@emotion/styled`, `framer-motion`
- Map: `maplibre-gl`, `react-map-gl` (maplibre export), `deck.gl`, `@deck.gl/layers`, `@deck.gl/geo-layers`, `@deck.gl/react`
- Data: `@tanstack/react-query`, `axios`, `zod`
- State: `zustand`
- Routing: `react-router-dom` v6
- Time: `date-fns`
- Orbital interpolation between server ticks: `satellite.js`

Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `eslint`, `prettier`, `vitest`, `@testing-library/react`, `playwright`.

### H.3 Pages

- **`/` DashboardPage** — full-bleed MapLibre dark basemap (CartoDB Dark Matter style URL, or a local style JSON checked into the repo for offline-ish dev). Deck.gl overlay with three layers (hot cells, ground track for focused sat, live points). Side panel: top-10 active sats sorted by recency. Top bar: WS status pill, observer picker (geolocation button + manual lat/lon), API-key input saved to localStorage for dev only.
- **`/satellite/:noradId`** — map zoomed to current position + 2-hour ground track; Chakra `Stat` blocks (alt, velocity, inclination derived from TLE); altitude sparkline over last 30 min; upcoming passes for saved observer.
- **`/passes`** — observer picker (geolocation + manual fallback); table of next-24h passes from `GET /passes?lat=&lon=&hours=24` — columns: NORAD, name, rise (local + UTC), set, max elevation, visible badge. Sort by rise time.
- **`/alerts`** — live feed from WS `alerts` subscription; severity badges (`info|warn|critical`); filters by `anomaly_type` and by `norad_id`.

### H.4 Realtime hook (`src/ws/useLiveStream.ts`)

Opens a single WS on app mount. Frame router:
- `satellite.position` → `store.upsertPosition(norad_id, {...})`; bump monotonic `positionsVersion` counter so Deck.gl re-renders without deep compare.
- `alerts` → `store.prependAlert(...)` (cap 500).
- `passes` → `store.addPass(...)` (keyed by `(norad_id, observer_id, rise_at)`).
- `ping` → reply `pong` (trivial JSON).
Render throttled to 60 fps via `requestAnimationFrame` coalescing; batch `upsertPosition` writes into a single store update per frame.

Reconnect: exponential backoff 1s → 30s max; resubscribe on reconnect; server clock skew displayed in `StatusBar`.

### H.5 Chakra theme

`config: { initialColorMode: 'dark', useSystemColorMode: false }`. Accent palette: `#22d3ee` (cyan — satellite points), `#f43f5e` (alerts), `#a3e635` (visible pass).

### H.6 Dev proxy (`vite.config.ts`)

```ts
server: {
  port: 5173,
  proxy: {
    '/api': { target: 'http://localhost:8001', changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') },
    '/ws':  { target: 'ws://localhost:8001',   ws: true,          rewrite: p => p.replace(/^\/ws/, '') },
  },
}
```

### H.7 Docker

- **Prod**: stage 1 `node:20-alpine` → `pnpm build`; stage 2 `nginx:1.27-alpine` serves `/usr/share/nginx/html` with SPA fallback. Runtime config injected at container start by `docker-entrypoint.d/10-envsubst.sh`, which emits `/usr/share/nginx/html/env.js` from the container's `VITE_*` envs (so the same image runs against any backend).
- **Dev**: `docker-compose.override.yml` points `frontend` at a `dev` build stage running `pnpm dev --host`, volume-mounts `./frontend/src`, exposes `5173`.

---

## I. Repository layout (top level)

```
satflow/
  README.md
  SCHEMAS.md                  # evolution rules, replay instructions
  Makefile                    # up/down, register-schemas, submit-flink-jobs, fe-dev, fe-build, replay
  docker-compose.yml
  docker-compose.override.yml # dev FE hot-reload + debug overrides
  .env.example
  schemas/
    common/envelope.avsc common/geo_point.avsc
    satellite/satellite_position_sampled.avsc
    satellite/satellite_pass_predicted.avsc
    satellite/satellite_anomaly_detected.avsc
    satellite/tle_record.avsc
    dlq/dlq_record.avsc
    register.sh deregister.sh
  ingest-api/                 # §C
  query-api/                  # §D
  tle-producer/               # §B
  pass-worker/                # §E.4
  position-persister/         # §F.3
  dlq-consumer/               # §F.4
  flink-jobs/                 # §E
    Dockerfile pyproject.toml
    jobs/01_hot_sky_cells.py jobs/02_conjunction_detector.py
  postgres/
    init/01-extensions.sql init/02-schema.sql init/03-seed.sql
  observability/
    prometheus.yml
    otel-collector.yaml
    grafana/
      provisioning/datasources/{prometheus,jaeger}.yml
      provisioning/dashboards/satflow.yml
      dashboards/satflow-overview.json
  frontend/                   # §H
  .github/workflows/ci.yml    # lint, type-check, test, docker build (optional bootstrap step)
```

Bootstrap order when the new repo is empty:

1. `docker-compose.yml` + `schemas/` + `Makefile` + `postgres/init/` → `make up` brings Kafka, Schema Registry, Redis, Postgres, Flink (cluster only) online. Run `make register-schemas`.
2. `ingest-api/` + `query-api/` — smallest useful slice: `POST /events/satellite-position` on one side, `GET /satellites/active` on the other, backed by Redis writes from a temporary mini-consumer in query-api (replaced by the persister in step 4).
3. `tle-producer/` with Celestrak adapter pulling 3 satellites (ISS, Hubble, Tiangong). Verify `GET /satellites/active` returns 3 entries.
4. `position-persister/` → Postgres starts accumulating history. `GET /satellites/{id}/track` works.
5. `flink-jobs/01_hot_sky_cells.py` → `GET /zones/hot` works. Submit via `make submit-flink-jobs`.
6. `pass-worker/` → `GET /passes` works against saved observers.
7. `dlq-consumer/` + `flink-jobs/02_conjunction_detector.py` → `GET /alerts` works.
8. `ws.py` in query-api → `/ws/stream` streams positions.
9. `frontend/` — DashboardPage → SatelliteDetailPage → PassesPage → AlertsPage.
10. `observability/` layer (metrics, otel-collector, grafana, jaeger, exporters) — wire each service's `/metrics` + OTel exporter as you add it; dashboards come last.

---

## J. Verification

### J.1 Local smoke path

```bash
git clone <satflow-repo> && cd satflow
cp .env.example .env
make up                     # builds images, starts compose
make register-schemas       # registers common + satellite subjects
make submit-flink-jobs      # 01_hot_sky_cells + 02_conjunction_detector
# tle-producer, pass-worker, position-persister, dlq-consumer start via compose
```

Open `http://localhost:5173`:
- DevTools Network → WS `/ws/stream` established; messages arrive at ≥1 Hz.
- ISS (25544) visibly moves W→E; ground track curve drawn.
- Hot HEALPix cells highlight dense constellations (Starlink trains pop out).
- `/passes` shows upcoming ISS passes for browser geolocation.
- `/alerts` populated after producing a synthetic anomaly via `kafkacat` (snippet in README).

### J.2 `curl` checks (dev API key)

```bash
curl -s http://localhost:8001/health
curl -s -H 'X-API-Key: dev-key-satflow-local-only' 'http://localhost:8001/satellites/active?limit=10' | jq
curl -s -H 'X-API-Key: dev-key-satflow-local-only' 'http://localhost:8001/satellites/25544/track?window=5m' | jq
curl -s -H 'X-API-Key: dev-key-satflow-local-only' 'http://localhost:8001/zones/hot?limit=20' | jq
curl -s -H 'X-API-Key: dev-key-satflow-local-only' 'http://localhost:8001/passes?lat=37.77&lon=-122.42&hours=24' | jq
# Rate-limit proof
for i in $(seq 1 200); do
  curl -s -o /dev/null -w '%{http_code}\n' \
    -H 'X-API-Key: dev-key-satflow-local-only' \
    http://localhost:8001/satellites/active
done | sort | uniq -c
# Expect: 200s then 429s once the 120/min bucket drains.
```

### J.3 Observability

- `http://localhost:8001/metrics` → Prometheus text.
- `http://localhost:3000` (admin/admin) → "Satflow Overview" dashboard populates within 2 minutes.
- `http://localhost:16686` (Jaeger) → connected spans `tle-producer → ingest-api → kafka` for a `POST /events/satellite-position`.

### J.4 Tests

- **pytest**:
  - `ingest-api/tests/test_producers.py` — mock Schema Registry; assert `produce_position` uses the correct schema id and Confluent wire format.
  - `ingest-api/tests/test_routes.py` — happy path + validation errors for `/events/satellite-position`.
  - `query-api/tests/test_auth.py`, `test_ratelimit.py`, `test_avro_codec.py`, `test_ws.py` (mock aiokafka consumer with an async iterator).
  - `tle-producer/tests/test_celestrak.py` (fixture TLE block), `test_propagate.py` (SGP4 at a known epoch within documented tolerance).
  - `pass-worker/tests/test_geometry.py` (elevation crossing at a known observer).
- **Playwright**:
  - `frontend/tests/e2e/dashboard.spec.ts` — ISS marker appears within 30s and moves between samples.
  - `frontend/tests/e2e/auth.spec.ts` — bad API key surfaces an unauthorized banner; good key restores normal behavior.

### J.5 CI (GitHub Actions, `.github/workflows/ci.yml`)

- Matrix: Python 3.12 × Node 20.
- Stages: `uv sync` per service → ruff + mypy → pytest (with ephemeral Kafka + Postgres via `services:`) → `pnpm install` + `pnpm build` + `pnpm test` for frontend → `docker build` every service image (no push).
- Optional nightly job: replay-fidelity check (`make replay` against a known fixture and diff the resulting Redis state).

---

## K. Risks and callouts

1. **PyFlink + astropy-healpix wheels** — cp310 manylinux wheels exist; verify first. Fallback already wired: `tle-producer` precomputes `healpix_cell` so Flink only groups on `BIGINT`.
2. **Skyfield inside Flink UDFs** — heavy and needs ephemeris files on every TM. Pass prediction lives in `pass-worker` exactly to avoid this.
3. **WebSocket horizontal scaling** — single-process asyncio fanout is fine for one `query-api` replica. Multiple replicas need shared-group stickiness or a Redis/NATS fanout + L4 sticky LB. Local dev is single-replica; design accommodates the later swap by keeping fanout behind `pubsub.py`.
4. **Space-Track / N2YO throttling** — Space-Track requires a user-agent and is aggressive about rate limiting; N2YO has a low free tier. Default adapter is Celestrak for a reason. Secrets via env only; never baked into images.
5. **Avro BACKWARD compat** — no renames on existing subjects; only additive changes with defaults. New event types → new subjects + new topics.
6. **HEALPix polygons on the FE** — computing cell polygons in JS is awkward. Backend builds polygon GeoJSON once per window and caches in Redis; FE just renders.
7. **TLE accuracy drift** — SGP4 accuracy is ~1 km near epoch, ~5 km at 7 days. Emit `tle_stale` alert when `sampled_at - tle_epoch > 7d`; expose drift as a gauge.
8. **Clock sync** — containers inherit host time, so Flink watermarks are fine on Docker Desktop / Linux. Document that cloud deploys must rely on NTP.
9. **Browser geolocation** — requires HTTPS off-localhost. Terminate TLS at the FE nginx in production.
10. **Data volume** — Celestrak `active.txt` is ~11k satellites. 5s tick × 30s bulk cadence = ~370 events/s. Kafka + the persister's batched inserts handle this comfortably; budget ~0.5 vCPU for the persister.
11. **Secrets handling** — `dev-key-satflow-local-only` is seeded by SQL for dev convenience. Production must rotate via a real admin path (out of scope for this plan; leave a TODO in `auth.py`).
12. **Log compaction for `satellite.tle.v1`** — keys are `norad_id`. Compaction removes intermediate TLEs; consumers that want full history should prefer Postgres (persister also writes a `tle_history` row on every update — add that table in §F.1 when TLE history becomes a product requirement).

---

## Critical first files to write (bootstrap day)

- `docker-compose.yml`
- `schemas/common/envelope.avsc`, `schemas/register.sh`
- `ingest-api/app/main.py`, `ingest-api/app/producers.py`
- `query-api/app/main.py`, `query-api/app/redis_client.py`
- `tle-producer/app/main.py`, `tle-producer/app/sources/celestrak.py`
- `postgres/init/02-schema.sql`
- `Makefile`
- `.env.example`
