# 04 — Master Checklist

A flat, pick-any-order checklist. No weekend modules, no sprints. Do what
interests you; most items are independent once the infra is up.

## Phase 0 — Foundation (do these first, in order)

- [ ] **0.1** Install prerequisites: Docker Desktop (8GB+ RAM allocated), Python 3.11+, `curl`, `jq`
- [ ] **0.2** Clone/create project directory; copy all `docs/*.md` in
- [ ] **0.3** Have Claude Code read `05-claude-code-brief.md` and scaffold the repo structure
- [ ] **0.4** Bring up `docker compose up -d`; verify all services healthy via Kafka UI (8080) and Flink UI (8082)
- [ ] **0.5** Run the smoke test (CLI): produce a test message to a non-core topic and consume it
- [ ] **0.6** Register all Avro schemas in Schema Registry via `schemas/register.sh`
- [ ] **0.7** Confirm Debezium connector is running and tailing Postgres

## Phase 1 — Write path (locked "first slice")

- [ ] **1.1** Simulator emits 10 events/sec of valid `DriverLocationPinged` to Ingest API
- [ ] **1.2** Ingest API validates against schema, produces to Kafka
- [ ] **1.3** First Flink job: per-zone driver count, 1-min tumbling window, sink to Redis
- [ ] **1.4** Query API returns top-10 hottest zones from Redis in <10ms
- [ ] **1.5** End-to-end trace: put `trace_id` in simulator event, grep it through API/Kafka/Flink/Redis logs

## Phase 2 — More Flink jobs (pick any order)

- [ ] **2.1** Surge pricing: sliding 5-min windows of requests per zone
- [ ] **2.2** Driver idle detector: session window, 10-min inactivity gap
- [ ] **2.3** Ride matching: interval join on `ride.request.v1` × `driver.location.v1`
- [ ] **2.4** Anomaly detection: impossible driver speed (ProcessFunction with timers)
- [ ] **2.5** CDC enrichment: join `ride.request.v1` with `dbz.public.drivers`

## Phase 3 — Kafka tuning experiments

- [ ] **3.1** E-P1 baseline vs tuned producer throughput
- [ ] **3.2** E-P2 durability cost of `acks=all`
- [ ] **3.3** E-P3 idempotence proof
- [ ] **3.4** E-C1 rebalance storm
- [ ] **3.5** E-C2 auto-commit data loss

## Phase 4 — Flink tuning experiments

- [ ] **4.1** E-F1 checkpoint interval sweep
- [ ] **4.2** E-F2 RocksDB block cache effect
- [ ] **4.3** E-F3 watermark pathology + idleness fix
- [ ] **4.4** E-F4 parallelism vs partitions mismatch
- [ ] **4.5** E-F5 savepoint-based rescaling

## Phase 5 — Schema evolution experiments

- [ ] **5.1** E-S1 safe add
- [ ] **5.2** E-S2 safe rename via alias
- [ ] **5.3** E-S3 unsafe type change — watch rejection
- [ ] **5.4** E-S4 required-field trap — understand why defaults matter
- [ ] **5.5** E-S5 breaking v2 migration with dual-write

## Phase 6 — Failure injection

- [ ] **6.1** E-X1 kill a Kafka broker; watch ISR shrink and recover
- [ ] **6.2** E-X2 kill a Flink TaskManager; watch key-group reassignment
- [ ] **6.3** E-X3 hot partition under clustered load (key by h3_cell)
- [ ] **6.4** E-X4 malformed-event flood; observe DLQ routing
- [ ] **6.5** E-R1 Redis eviction behaviour

## Phase 7 — Systems design mocks (using your built system)

For each: write your answer in `experiments/design-mocks/` with reference
to concrete pieces of your running lab.

- [ ] **7.1** "Design a real-time dashboard for drivers online now" — solve with topics, partitions, materialized views
- [ ] **7.2** "Design a fraud detection pipeline" — latency budget, state size, checkpointing
- [ ] **7.3** "Migrate an event schema in production with zero downtime" — use E-S5 as the reference
- [ ] **7.4** "System handling 1M events/sec — what breaks first?" — extrapolate from your p99 measurements
- [ ] **7.5** "Exactly-once for billing events" — explain 2PC using your at-least-once baseline

## Phase 8 — Stretch goals

- [ ] **8.1** Add one `RecordNameStrategy` topic for comparison
- [ ] **8.2** Add Prometheus + Grafana with pre-built Flink and Kafka dashboards
- [ ] **8.3** Add Jaeger for distributed tracing across APIs + Flink
- [ ] **8.4** Port one Flink job from PyFlink to Java/Scala and benchmark
- [ ] **8.5** Deploy a read-only demo to Railway (Ingest API + Query API only)

## Notes on order

- Phases 0, 1 are foundational — do those in order first.
- Phases 2–6 are independent. Pick what interests you each session.
- Phase 7 rewards having run experiments in 3–6 first.
- Phase 8 is bonus after you're fluent.
