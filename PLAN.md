# RideFlow → Satellite Tracking Platform

## Context

RideFlow today is a ride-hailing event-streaming reference stack (Kafka + Schema Registry + Flink + Redis + Postgres + FastAPI `ingest-api` and `query-api` + a synthetic driver simulator). The goal is to repurpose it into a **real-time satellite tracking platform** that consumes **real TLE data** (Celestrak by default, with N2YO / Space-Track adapter seams), with a **Vite + React + TypeScript + Chakra UI** frontend that visualizes live positions on **Deck.gl + MapLibre**, driven by a **WebSocket** live stream. Along the way, the backend gets the four improvements it has been missing: API-key auth + rate limiting, durable Postgres persistence + replay, observability (Prometheus + OTel + Grafana + Jaeger), and a DLQ consumer.

Design priorities:

- **Additive migration.** New Avro subjects / Kafka topics / services live alongside the ride schemas during Phase 1. Phase 2 (separate PR) retires ride schemas, topics, and the simulator.
- **Respect existing compat policy.** Schema Registry is `BACKWARD` — no renames of existing fields; new event types get new subjects.
- **Keep the `rideflow.common.Envelope`** unchanged; it is event-type-agnostic and already wired through tracing.
- **Adapter seams** for TLE source, auth backend, and metrics exporters.
- **Local first, cloud-ready.** Everything runs via `docker compose up`.

Target demo: open `http://localhost:5173`, see the ISS (NORAD 25544) and the active Celestrak catalog moving on a dark MapLibre globe, click a satellite, see its ground track and upcoming passes for the browser's geolocation, and watch a live alert feed for conjunctions / orbit-decay / stale-TLE anomalies.

---

## A. Domain remap — Avro, topics, services

### A.1 New Kafka topics (added in `docker-compose.yml` `topic-init`)

| Topic | Partitions | Retention | Key |
|---|---|---|---|
| `satellite.position.v1` | 12 | 24h | `norad_id` |
| `satellite.pass.v1` | 6 | 7d | `norad_id\|observer_id` |
| `satellite.anomaly.v1` | 3 | 30d | `norad_id` |
| `satellite.tle.v1` | 1 | 30d (compact) | `norad_id` |
| `satellite.position.v1.dlq` / `.pass.v1.dlq` / `.anomaly.v1.dlq` | 1 each | 30d | — |

Keep existing `driver.location.v1` / `ride.*` topics during Phase 1.

### A.2 New Avro schemas (`schemas/`, namespace `rideflow.satellite`)

- `satellite_position_sampled.avsc` — `norad_id`, `name`, `lat`, `lon`, `alt_km`, `velocity_kms`, `healpix_cell` (long, nside=64 nested), `tle_epoch` (ts-ms), `sampled_at` (ts-ms), `source` enum `{celestrak, n2yo, spacetrack}`. Uses shared `Envelope`.
- `satellite_pass_predicted.avsc` — `norad_id`, `observer_id`, `observer` (`GeoPoint`), `rise_at`, `culminate_at`, `set_at`, `max_elevation_deg`, `rise_azimuth_deg`, `set_azimuth_deg`, `visible`.
- `satellite_anomaly_detected.avsc` — new subject; shape like existing `anomaly_detected.avsc`. Enum `SatelliteAnomalyType = {orbit_decay, conjunction_alert, unexpected_maneuver, tle_stale, reentry_candidate}`.
- `tle_record.avsc` — backs log-compacted `satellite.tle.v1`.

### A.3 Schema Registry wiring

Edit `schemas/register.sh`: add the satellite value subjects with `env_ref`; add new DLQ topics to the DLQ-subject loop. Mirror in `schemas/deregister.sh`.

### A.4 Ingest API changes (`ingest-api/app/`)

- `main.py` — new route `POST /events/satellite-position` (202), parallel to `post_driver_location`. Keep `/events/driver-location` in Phase 1.
- `schemas.py` — add `SatellitePositionIn` (Pydantic).
- `producers.py` — generalize `EventProducer` to a dict of subject→(schema_id, parsed_schema); add `produce_position(...)`.
- `config.py` — add `POSITION_TOPIC`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `CORS_ORIGINS`, API-key store env.
- `healpix_util.py` (new) — `cell_for_sky(lat, lon, nside=64)` using `astropy_healpix`. Retire `h3util.py` in Phase 2.
- `pyproject.toml` — add `astropy-healpix`, `prometheus-fastapi-instrumentator`, OTel packages.

---

## B. Real-data producer — new `tle-producer` service

Replaces the role of the ride `simulator`, but coexists in Phase 1. Layout mirrors `simulator/` (Python + uv + async httpx).

```
tle-producer/
  Dockerfile
  pyproject.toml        # httpx, sgp4, skyfield, astropy-healpix, structlog, pydantic-settings
  app/
    main.py             # fetch loop + propagation loop
    config.py
    sources/
      base.py           # TleSource Protocol + TleRecord dataclass
      celestrak.py      # default: celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle
      n2yo.py           # stub, reads N2YO_API_KEY
      spacetrack.py     # stub, reads SPACETRACK_USER/PASS
    propagate.py        # skyfield primary, sgp4 fallback
    publish.py          # bounded-concurrency httpx.AsyncClient to ingest-api
```

**Behavior**
- **TLE fetch loop**: every `TLE_REFRESH_MINUTES` (default 180). In-memory `dict[norad_id, Satrec]`.
- **Propagation loop**: every `PROPAGATE_INTERVAL_S` (default 5s).
  - Watchlist (`WATCHLIST_NORAD_IDS`, default `25544,20580,48274` = ISS, Hubble, Tiangong): 5s.
  - All others in `active.txt`: 30s, round-robin across ticks.
- **Sample compute**: Skyfield `EarthSatellite.at(ts).subpoint()` primary; SGP4 for unit tests.
- **Publish**: `POST http://ingest-api:8000/events/satellite-position` with `X-API-Key` + `x-trace-id`.

Source adapter chosen at startup via `TLE_SOURCE=celestrak|n2yo|spacetrack`. Secrets (`N2YO_API_KEY`, `SPACETRACK_USER/PASS`) via compose env + `.env.example`.

---

## C. Query API additions (`query-api/app/`)

### C.1 New REST endpoints

| Method | Path | Backing store |
|---|---|---|
| `GET /satellites/active?limit=` | Redis ZSET `sats:active` |
| `GET /satellites/{norad_id}` | Postgres `satellites` |
| `GET /satellites/{norad_id}/track?window=5m` | Redis list `sats:track:{norad_id}`; fallback Postgres |
| `GET /zones/hot?limit=` | Redis `sky:hot:<window_end_ms>` ZSET → GeoJSON of HEALPix cells |
| `GET /passes?lat=&lon=&hours=24` | On-demand skyfield; else Redis `passes:{observer_id}` |
| `GET /alerts?since=` | Postgres `anomalies` or Redis `alerts:recent` |
| `WS /ws/stream` | Multiplexed live stream (see §C.2) |

### C.2 WebSocket protocol

Endpoint `GET /ws/stream`. Client subscribes on connect:
```json
{"subscribe": ["satellite.position", "alerts"], "filter": {"norad_ids": ["25544"]}}
```
Server streams `{topic, ts, data}` JSON frames. Heartbeat: server `ping` every 20s; disconnect at 60s idle.

Backing: single long-lived `aiokafka.AIOKafkaConsumer` subscribed to `satellite.position.v1` + `satellite.anomaly.v1`; in-process asyncio PubSub fanout to per-WS filtered queues. Avro decoding in new `query-api/app/avro_codec.py` (reverse of the Confluent wire format used by the producer).

### C.3 Auth + rate limiting

- `auth.py` — `APIKeyMiddleware` reads `X-API-Key` (or WS `?api_key=...`). Looks up `api_keys` by `sha256(key)`, 60s TTL cache. `/health`, `/metrics` excluded.
- `ratelimit.py` — Redis token bucket per `key_id` via Lua on `rl:{key_id}:{window_minute}`. 429 + `Retry-After`.
- `deps.py` — `require_scope("read:satellites")` dependency factory.

Same middleware on `ingest-api` (so `tle-producer` must present a key).

### C.4 CORS

`CORSMiddleware` on both APIs. Origins from env `CORS_ORIGINS` (default `http://localhost:5173,http://localhost:4173`). Methods `GET,POST,OPTIONS`; headers `X-API-Key,X-Trace-Id,Content-Type`.

### C.5 Dependencies (`query-api/pyproject.toml`)

Add: `aiokafka`, `fastavro`, `websockets`, `prometheus-fastapi-instrumentator`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-exporter-otlp`, `skyfield`, `astropy-healpix`, `bcrypt`.

---

## D. Flink jobs (`flink-jobs/jobs/`)

### D.1 `01_hot_sky_cells.py` — replaces `01_hot_zones.py`

Source: `satellite.position.v1` (Avro-Confluent, same SR wiring).
Watermark: `envelope.occurred_at - INTERVAL '10' SECOND`.
Group: `payload.healpix_cell` over `TUMBLE(1 min)`. Aggregate: `COUNT(DISTINCT payload.norad_id)`. Sink: Redis `sky:hot:<window_end_ms>` via renamed `RedisHotZoneWriter`.

### D.2 `02_pass_predictor` — standalone `pass-worker/` service (not Flink)

Skyfield inside PyFlink UDFs is heavy. Plan uses a Python service: reads `satellite.position.v1` with aiokafka, broadcasts observers from Redis set `observers:active` (populated by Query API), computes elevation/azimuth, emits to `satellite.pass.v1` when elevation crosses 0° / 10° / culminates.

### D.3 `03_conjunction_detector.py`

Source: `satellite.position.v1`. Bucket by `(healpix_cell @ nside=32, altitude_band_50km)`. Each 10s window, for buckets with ≥2 sats compute pairwise great-circle distance; if `range_km < 10` and `|Δalt_km| < 5` → emit `SatelliteAnomalyDetected(type=conjunction_alert)`. Also emits `orbit_decay` when `alt_km` drops monotonically across 6 windows below 160 km, and `tle_stale` when `ingested_at - tle_epoch > 7d`. Sink: `satellite.anomaly.v1`.

### D.4 Dockerfile & deps

`flink-jobs/Dockerfile`: add `astropy-healpix`. Do **not** add `skyfield` to the Flink image (pass prediction lives in pass-worker). Precompute `healpix_cell` in `tle-producer` so Flink only sees `BIGINT`.

### D.5 Why HEALPix, not H3

HEALPix is equal-area on the sphere and standard in astronomy. `nside=64` gives ~0.92° (~100 km) cells. Retire `h3util.py` in Phase 2.

### D.6 Submission (`Makefile`)

```
submit-flink-jobs:
	$(COMPOSE) exec -T flink-jm flink run -d -py /opt/flink/jobs/01_hot_sky_cells.py
	$(COMPOSE) exec -T flink-jm flink run -d -py /opt/flink/jobs/03_conjunction_detector.py
```

Do not submit the old `01_hot_zones.py` once satellite work starts.

---

## E. Durability, replay, DLQ

### E.1 Postgres schema (`postgres/init/02-schema.sql`)

Tables: `satellites`, `satellite_positions` (partitioned monthly by `sampled_at`), `passes`, `anomalies`, `api_keys`, `rate_limit_events`, `dlq_events`. Indexes: `(norad_id, sampled_at desc)`, `(healpix_cell, sampled_at desc)`, `(observer_id, rise_at)`, `api_keys(key_hash)`. `api_keys`: `key_id uuid`, `key_hash bytea unique`, `owner`, `scopes text[]`, `rate_per_minute int default 120`, `disabled_at`.

### E.2 Seed (`03-seed.sql`)

- Dev API key: raw `dev-key-rideflow-local-only`, stored as `sha256` hex, owner `dev`, scopes `{read:satellites,write:events}`, 120 rpm.
- 3 seed satellites: ISS (25544), Hubble (20580), Tiangong (48274).

### E.3 `position-persister` service

aiokafka → shared Avro codec → asyncpg `INSERT ... ON CONFLICT (norad_id, sampled_at) DO NOTHING`, batched at 500 rows or 500 ms. Upserts `satellites` on first-seen. Anomalies persisted by a sibling consumer task in the same service → `anomalies` table.

### E.4 `dlq-consumer` service

Reads all `*.dlq.v1` topics → `dlq_events` (`original_topic`, `failure_reason`, `failure_stage`, `failed_at`, `original_key bytea`, `original_value bytea`, `headers jsonb`). Emits `structlog` WARN with fields from `dlq_record.avsc`. Optional webhook via `DLQ_ALERT_URL`.

### E.5 Evolution docs

New `SCHEMAS.md`: BACKWARD policy (optional fields only, never rename/retype); how to add a new event type (new subject, new topic); how to evolve (`["null", T]` with `default null`; bump `event_version` in Envelope).

---

## F. Observability

### F.1 Prometheus + `/metrics`

`prometheus-fastapi-instrumentator` on both APIs, exposed at `/metrics`. Also:
- Ingest API: parse `confluent_kafka.Producer.stats_cb` JSON → gauges.
- Query API / persister / dlq-consumer: aiokafka built-in metrics.
- `kafka-exporter` container for topic lag per consumer group.
- Flink Prometheus reporter (set `FLINK_PROPERTIES` in compose, expose port 9249 on JM + TMs).

### F.2 OpenTelemetry

Add `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `-httpx`, `-asyncpg`, `-exporter-otlp` to both APIs + `tle-producer` + `pass-worker` + `position-persister` + `dlq-consumer`. New `otel-collector` container (OTLP gRPC 4317) → Jaeger + Prometheus. Config at `observability/otel-collector.yaml`. W3C trace context propagation; existing `trace_middleware` stays; `x-trace-id` mapped to span attribute `rideflow.trace_id`.

### F.3 Grafana

`grafana/grafana:11.2.0` on port 3000. Provisioning under `observability/grafana/`. Dashboards: ingest EPS; producer error rate; Kafka lag per topic; Flink checkpoint duration; Query API p50/p95/p99; Redis hit rate; active WS connections; DLQ event rate.

### F.4 New compose services

| Service | Image | Port |
|---|---|---|
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.108.0` | 4317, 4318 |
| `jaeger` | `jaegertracing/all-in-one:1.60` | 16686 |
| `prometheus` | `prom/prometheus:v2.54.1` | 9090 |
| `grafana` | `grafana/grafana:11.2.0` | 3000 |
| `kafka-exporter` | `danielqsj/kafka-exporter:v1.7.0` | 9308 |
| `redis-exporter` | `oliver006/redis_exporter:v1.62.0` | 9121 |

---

## G. Frontend — Vite + React + TS + Chakra + Deck.gl + MapLibre

### G.1 Layout (`frontend/`)

```
frontend/
  Dockerfile                    # multi-stage: node build → nginx:alpine static
  nginx.conf                    # SPA fallback + gzip
  package.json                  # pnpm
  tsconfig.json
  vite.config.ts                # dev proxy /api→query-api:8001, /ws→query-api:8001
  index.html
  src/
    main.tsx                    # Chakra provider, React Query, Router
    theme.ts                    # dark mode default
    api/{client,satellites,passes,alerts,types}.ts
    ws/{useLiveStream,store}.ts
    components/
      Globe.tsx                 # Deck.gl + react-map-gl/maplibre
      layers/{satellitePointsLayer,groundTrackLayer,hotCellsLayer}.ts
      SatelliteList.tsx PassTable.tsx AlertFeed.tsx StatusBar.tsx
    pages/
      DashboardPage.tsx         # /
      SatelliteDetailPage.tsx   # /satellite/:noradId
      PassesPage.tsx            # /passes
      AlertsPage.tsx            # /alerts
    hooks/useObserverLocation.ts
    utils/{healpix,satelliteJs}.ts
  .env.example                  # VITE_QUERY_API_URL, VITE_WS_URL, VITE_API_KEY
```

### G.2 Dependencies

- UI: `@chakra-ui/react`, `@emotion/react`, `@emotion/styled`, `framer-motion`
- Map: `maplibre-gl`, `react-map-gl` (maplibre export), `deck.gl`, `@deck.gl/layers`, `@deck.gl/geo-layers`, `@deck.gl/react`
- Data: `@tanstack/react-query`, `axios`, `zod`
- State: `zustand`
- Routing: `react-router-dom` v6
- Time: `date-fns`
- Client-side orbital refinement: `satellite.js`

Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `eslint`, `prettier`, `vitest`, `@testing-library/react`, `playwright`.

### G.3 Pages

- **`/` Dashboard** — full-bleed MapLibre dark basemap (CartoDB Dark Matter). Layers: `HotCellsLayer` (PolygonLayer from `/zones/hot` GeoJSON), `GroundTrackLayer` (PathLayer), `SatellitePointsLayer` (ScatterplotLayer from WS store). Side panel: top-10 active sats; top bar: WS status pill, observer picker, API-key input (dev).
- **`/satellite/:noradId`** — map + 2-hour ground track; alt/velocity/incl. stats; altitude sparkline; upcoming passes.
- **`/passes`** — observer picker (geolocation + manual); table of next-24h passes.
- **`/alerts`** — live feed from WS `alerts`; severity badges; type filter.

### G.4 Realtime hook (`src/ws/useLiveStream.ts`)

Single WS on app mount. `satellite.position` → `store.upsertPosition(...)` (bump monotonic `positionsVersion` for Deck.gl re-render). `alerts` → `store.prependAlert(...)` (cap 500). `ping` → `pong`. Render throttled to 60 fps.

### G.5 Chakra theme

`initialColorMode: 'dark'`, `useSystemColorMode: false`. Accents `#22d3ee` (cyan points), `#f43f5e` (alerts), `#a3e635` (visible pass).

### G.6 Dev proxy (`vite.config.ts`)

```ts
server: {
  port: 5173,
  proxy: {
    '/api': { target: 'http://localhost:8001', changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') },
    '/ws':  { target: 'ws://localhost:8001',   ws: true,          rewrite: p => p.replace(/^\/ws/, '') },
  },
}
```

### G.7 Docker

- **Prod**: stage 1 `node:20-alpine` → `pnpm build`; stage 2 `nginx:1.27-alpine` serves `/usr/share/nginx/html` with SPA fallback. Runtime config via entrypoint emitting `env.js`.
- **Dev**: `docker-compose.override.yml` runs `pnpm dev --host`, volume-mounts `./frontend/src`, exposes 5173.

---

## H. File change list

### H.1 New files

- `schemas/satellite_position_sampled.avsc`, `satellite_pass_predicted.avsc`, `satellite_anomaly_detected.avsc`, `tle_record.avsc`
- `ingest-api/app/healpix_util.py`
- `query-api/app/auth.py`, `ratelimit.py`, `deps.py`, `avro_codec.py`, `ws.py`, `satellites.py`, `passes.py`
- `tle-producer/` (whole dir)
- `pass-worker/` (whole dir — §D.2)
- `position-persister/` (whole dir)
- `dlq-consumer/` (whole dir)
- `flink-jobs/jobs/01_hot_sky_cells.py`, `03_conjunction_detector.py`
- `observability/otel-collector.yaml`, `prometheus.yml`, `grafana/provisioning/**`, `grafana/dashboards/rideflow-overview.json`
- `frontend/` (whole dir)
- `docker-compose.override.yml` (dev FE + tuning)
- `SCHEMAS.md`

### H.2 Edited files

- `docker-compose.yml` — new topics; new services (frontend, tle-producer, pass-worker, position-persister, dlq-consumer, otel-collector, jaeger, prometheus, grafana, kafka-exporter, redis-exporter); new envs; expose Flink Prom port 9249.
- `schemas/register.sh`, `deregister.sh` — add satellite subjects + DLQ loop.
- `ingest-api/app/main.py` — `/events/satellite-position`, OTel, CORS, auth.
- `ingest-api/app/producers.py` — multi-subject schema load; `produce_position`.
- `ingest-api/app/schemas.py` — `SatellitePositionIn`.
- `ingest-api/app/config.py` — new envs.
- `ingest-api/pyproject.toml` — `astropy-healpix`, Prom instrumentator, OTel.
- `query-api/app/main.py` — auth + ratelimit + CORS + OTel + Prom; new routers; lifespan starts aiokafka fanout.
- `query-api/app/redis_client.py` — `track_positions`, `active_satellites`, `top_hot_sky_cells`; key prefix `sky:hot:`.
- `query-api/pyproject.toml` — see §C.5.
- `flink-jobs/Dockerfile` + `pyproject.toml` — `astropy-healpix`.
- `postgres/init/02-schema.sql` — full schema per §E.1.
- `postgres/init/03-seed.sql` — dev API key + seed sats.
- `Makefile` — `submit-flink-jobs`, `start-tle`, `fe-dev`, `fe-build`, `observability-up`.
- `.env.example` — TLE_SOURCE, WATCHLIST_NORAD_IDS, N2YO_API_KEY, SPACETRACK_*, CORS_ORIGINS, OTel endpoint, FE vars.
- `README.md` — quickstart swap: ISS tracking demo.

### H.3 Deferred to Phase 2

- Delete `flink-jobs/jobs/01_hot_zones.py`.
- Delete `simulator/`.
- Delete rider-ride Avro schemas, topics, and `/events/driver-location` route.
- Extract shared `rideflow_auth` package.

### H.4 Existing utilities to reuse

- `ingest-api/app/producers.py` `_load_refs` recursion → multi-subject load.
- `ingest-api/app/producers.py` Confluent wire-format serializer → mirror in `query-api/app/avro_codec.py` for decode.
- `ingest-api/app/main.py` `trace_middleware` → keep, add OTel alongside.
- `ingest-api/app/main.py` `post_driver_location` → structural template for `post_satellite_position`.
- `ingest-api/app/schemas.py` lat/lon validators → reuse on `SatellitePositionIn`.
- `simulator/app/main.py` bounded `httpx.AsyncClient` pattern → reuse in `tle-producer/app/publish.py`.
- `flink-jobs/jobs/01_hot_zones.py` `RedisHotZoneWriter` sink → rename, logic unchanged.
- `flink-jobs/jobs/01_hot_zones.py` SR + Avro-Confluent source wiring → reuse verbatim.
- `schemas/dlq_record.avsc` → reused for all satellite DLQ subjects.
- Existing `anomaly_detected.avsc` shape → template for `satellite_anomaly_detected.avsc`.

---

## I. Verification

### I.1 Local smoke path

```bash
cp .env.example .env
make up                     # builds all images including frontend
make register-schemas       # registers common + satellite subjects
make submit-flink-jobs      # 01_hot_sky_cells + 03_conjunction_detector
```

Open `http://localhost:5173`:
- DevTools: WS `/ws/stream` established; messages ≥1 Hz.
- ISS (25544) moves W→E; ground track drawn.
- Hot HEALPix cells highlight dense constellations (Starlink trains).
- `/passes` shows upcoming ISS passes for browser geolocation.
- `/alerts` feed populated by synthetic anomaly (README documents kafkacat snippet).

### I.2 curl checks

```bash
curl -s http://localhost:8001/health
curl -s -H 'X-API-Key: dev-key-rideflow-local-only' 'http://localhost:8001/satellites/active?limit=10' | jq
curl -s -H 'X-API-Key: dev-key-rideflow-local-only' 'http://localhost:8001/satellites/25544/track?window=5m' | jq
curl -s -H 'X-API-Key: dev-key-rideflow-local-only' 'http://localhost:8001/zones/hot?limit=20' | jq
curl -s -H 'X-API-Key: dev-key-rideflow-local-only' 'http://localhost:8001/passes?lat=37.77&lon=-122.42&hours=24' | jq
for i in $(seq 1 200); do
  curl -s -o /dev/null -w '%{http_code}\n' -H 'X-API-Key: dev-key-rideflow-local-only' http://localhost:8001/satellites/active
done | sort | uniq -c
# Expect 200s then 429s once 120/min bucket drains.
```

### I.3 Observability checks

- `http://localhost:8001/metrics` → Prometheus text.
- `http://localhost:3000` → "RideFlow Overview" dashboard populates within 2 min.
- `http://localhost:16686` (Jaeger) → connected spans `tle-producer → ingest-api → kafka`.

### I.4 Tests

- **pytest**:
  - `ingest-api/tests/test_producers.py` — mock SR; assert `produce_position` uses correct schema id.
  - `ingest-api/tests/test_routes.py` — happy + validation errors for `/events/satellite-position`.
  - `query-api/tests/test_auth.py`, `test_ratelimit.py`, `test_avro_codec.py`, `test_ws.py`.
  - `tle-producer/tests/test_celestrak.py`, `test_propagate.py`.
- **Playwright**:
  - `frontend/tests/e2e/dashboard.spec.ts` — ISS marker appears within 30s and moves.
  - `frontend/tests/e2e/auth.spec.ts` — bad API key surfaces unauthorized banner.

---

## J. Risks and callouts

1. **PyFlink + astropy-healpix wheels** — cp310 manylinux wheels exist; verify in Phase 0. Fallback: precompute `healpix_cell` upstream in `tle-producer`.
2. **Skyfield in Flink UDFs** — heavy; use standalone `pass-worker` instead.
3. **WS horizontal scaling** — single-process asyncio fanout is fine for one `query-api`. Multiple replicas need sticky LB + shared fanout (Redis/NATS).
4. **Space-Track / N2YO throttling** — default to Celestrak. Secrets via env.
5. **Avro BACKWARD compat** — no renames; new subjects coexist with ride subjects until Phase 2.
6. **HEALPix cell polygons on FE** — backend produces GeoJSON at query time (cached per window in Redis).
7. **TLE accuracy drift** — SGP4 ~1 km near epoch, ~5 km at 7d. Emit `tle_stale` when `sampled_at - tle_epoch > 7d`.
8. **Clock sync in local compose** — containers inherit host time; Flink watermarks assume alignment.
9. **Browser geolocation** — HTTPS required off-localhost.
10. **`simulator` coexistence in Phase 1** — still runs; do not resubmit `01_hot_zones.py`.
11. **Data volume** — Celestrak `active.txt` ~11k sats. 5s tick, 30s cadence ≈ 333 events/sec. Position-persister uses batched writes.

---

## Critical files to modify

- `ingest-api/app/producers.py`
- `ingest-api/app/main.py`
- `query-api/app/main.py`
- `docker-compose.yml`
- `schemas/register.sh`
- `flink-jobs/jobs/01_hot_zones.py` (rewritten as `01_hot_sky_cells.py`)
- `postgres/init/02-schema.sql`
- `Makefile`
- `.env.example`
