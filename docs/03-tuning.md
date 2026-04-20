# 03 — Tuning Guide & Experiments

**Status:** locked
**Audience:** you, with this doc open while you run experiments

## 1. How to use this doc

Each section lists parameters in **priority order** — the ones at the top
have the biggest observable effect at our scale. Don't try to tune everything.
Pick one knob, change it, measure, write down what you learned.

Every experiment has: hypothesis, setup, measurement, procedure, expected
result. Real results go in `experiments/`.

## 2. Kafka broker parameters

### Top priority

| Parameter | Our value | What it does | Experiment hook |
|---|---|---|---|
| `num.partitions` | per-topic | Default for auto-created topics | Compare 1 vs 12 partitions for location data |
| `default.replication.factor` | 3 | Copies of each partition | The reason we have 3 brokers |
| `min.insync.replicas` | 2 | Min replicas acknowledging on `acks=all` | Set to 3, kill a broker, watch producer block |
| `log.retention.hours` | per-topic | How long data sticks | 1h for location, 168h for lifecycle |
| `log.segment.bytes` | 100MB | Size of log segment files | Smaller = faster rolls = easier to observe |

### Educational

| Parameter | What to learn |
|---|---|
| `unclean.leader.election.enable` | Turn on, kill leader + ISR, watch data loss |
| `message.max.bytes` | Send a 2MB event, watch rejection |
| `replica.fetch.max.bytes` | Must be >= `message.max.bytes` or replication breaks |

## 3. Producer parameters (the write path)

### The triangle: throughput, latency, durability — pick two

| Parameter | Default | Effect |
|---|---|---|
| `acks` | `all` | `0`=fire-forget, `1`=leader only, `all`=all ISR |
| `batch.size` | 16384 | Larger = better throughput, worse p99 latency |
| `linger.ms` | 0 | Wait this long to fill a batch |
| `compression.type` | none | `zstd` usually best; `snappy` fastest |
| `max.in.flight.requests.per.connection` | 5 | Lower = stricter retry ordering |
| `enable.idempotence` | true | Prevents duplicate produces from retries |
| `buffer.memory` | 32MB | Producer's in-memory buffer before blocking |

### Experiments

**E-P1: Baseline vs tuned throughput**
- Hypothesis: `linger.ms=20` + `batch.size=128KB` + `compression=zstd` gives 3–5x throughput vs defaults
- Measure: events/sec sustained over 60s, p50/p99 produce latency
- Method: simulator at max rate, read JMX from broker

**E-P2: Durability cost**
- Hypothesis: `acks=all` + `min.insync=2` is ~30% slower than `acks=1`
- Measure: p99 produce latency
- Method: toggle `acks`, same load

**E-P3: Idempotence proof**
- Hypothesis: with `enable.idempotence=true`, forced retries produce zero downstream duplicates for a given produce call (but application-level retries still can — that's different)
- Method: 10k events, inject broker errors mid-flight, count downstream

## 4. Consumer parameters

| Parameter | Default | Effect |
|---|---|---|
| `fetch.min.bytes` | 1 | Wait until this much is available |
| `fetch.max.wait.ms` | 500 | Max wait even if min not met |
| `max.poll.records` | 500 | Records per poll — tune for processing speed |
| `max.poll.interval.ms` | 300000 | Consumer is "dead" if no poll in this time |
| `session.timeout.ms` | 45000 | Heartbeat-based death detection |
| `auto.offset.reset` | latest | `earliest` or `latest` on no committed offset |
| `enable.auto.commit` | true | Turn OFF for anything that matters |

### Experiments

**E-C1: Rebalance storms**
- Hypothesis: adding a consumer causes a rebalance that stalls the group for hundreds of ms
- Method: 3 consumers running, start a 4th, measure lag spike
- Compare: eager vs `cooperative-sticky` rebalance protocol

**E-C2: The auto-commit trap**
- Hypothesis: auto-commit loses messages on crash mid-batch
- Method: consume 1000 messages with auto-commit on, SIGKILL, count reprocesses
- Repeat with manual commit after processing — watch the number go to zero

## 5. Flink parameters

### Resource

| Parameter | Starting value | Notes |
|---|---|---|
| `parallelism.default` | 4 | Must be ≤ sum of slots across TMs |
| `taskmanager.numberOfTaskSlots` | 2 per TM | 2 TMs × 2 slots = 4 slots |
| `taskmanager.memory.process.size` | 1728MB | Memory budget per TM |
| `state.backend` | `rocksdb` | Locked decision — from day one |
| `pipeline.max-parallelism` | 128 (default) | Sets key group count; cannot change later without savepoint |

**Rule:** `parallelism ≤ kafka_partitions` for source topics, or some subtasks idle.

### Checkpointing

| Parameter | Starting value | Notes |
|---|---|---|
| `execution.checkpointing.interval` | 10s | Experiment with 1s and 60s |
| `execution.checkpointing.mode` | AT_LEAST_ONCE | Matches our delivery choice |
| `execution.checkpointing.timeout` | 10min | Shorter catches stuck checkpoints |
| `execution.checkpointing.min-pause` | 5s | Forces gap between checkpoints |
| `execution.checkpointing.unaligned` | true | Helps under backpressure |

### Watermarks and event time

| Concept | What to try |
|---|---|
| `forBoundedOutOfOrderness(Duration)` | 0s (strict) vs 30s (sloppy) |
| `allowedLateness` | Emit updates for late-arriving data |
| `withIdleness` | Idle partition detection — without this, one idle partition stalls the whole pipeline |

### Flink experiments

**E-F1: Checkpoint interval sweep**
- Hypothesis: 1s checkpoints noticeably reduce throughput vs 10s/60s
- Measure: throughput, checkpoint duration, state size
- Kill a TM during each setting; measure recovery time

**E-F2: State backend stress** (since we're on RocksDB from day one)
- Hypothesis: RocksDB block cache size has a big effect on read-heavy jobs
- Method: vary `state.backend.rocksdb.block.cache-size` 8MB → 256MB
- Measure: per-record latency in the session window job

**E-F3: Watermark pathology**
- Hypothesis: one driver sending events with 1h-offset timestamps stalls all windows
- Method: induce the offset in simulator; observe windows never firing
- Fix: enable `withIdleness` — windows fire again

**E-F4: Parallelism vs partitions mismatch**
- Hypothesis: parallelism=3 with 12 partitions means each task reads 4 partitions (balanced); parallelism=13 means one subtask idle
- Method: run at 1, 3, 6, 12, 13, 24 parallelism on `driver.location.v1`
- Observe: Flink UI's subtask throughput view

**E-F5: Key group rescaling**
- Hypothesis: rescaling parallelism from 4 → 8 via savepoint preserves state correctly
- Method: run session window job, take savepoint, restart with higher parallelism, verify window results unchanged

## 6. Redis parameters

| Parameter | Effect |
|---|---|
| `maxmemory` | Hard cap — set low (100MB) on purpose to observe eviction |
| `maxmemory-policy` | `allkeys-lru` vs `volatile-lru` vs `noeviction` |
| `appendonly` | AOF durability vs default RDB snapshots |

**E-R1: Eviction under pressure**
- Hypothesis: with `noeviction`, Flink's Redis sink starts failing when full
- Method: set 100MB cap, run until full
- Compare: `allkeys-lru` — stale reads happen silently

## 7. Schema evolution experiments (from doc 02)

**E-S1: Safe add** — add optional `accuracy_meters`; old and new producers coexist
**E-S2: Safe rename** — rename field using Avro `aliases`
**E-S3: Unsafe change** — change `lat` from `double` to `float`; Registry rejects
**E-S4: Required-field trap** — add required field without default; Registry rejects under BACKWARD
**E-S5: Breaking v2** — create `driver.location.v2`, dual-write for migration window, cut over consumers

## 8. Cross-cutting: observability we rely on

- **Kafka UI** (port 8080): topics, partitions, consumer lag, ISR
- **Flink UI** (port 8082): job graph, backpressure indicators, checkpoint history
- **`kafka-consumer-groups.sh`**: command-line lag inspection
- **`kafka-producer-perf-test.sh`**: built-in benchmark
- **Redis `MONITOR` and `INFO`**: realtime ops, memory
- **Structured logs** from our APIs with trace_id, event_id, duration

## 9. The experiment template

```markdown
# Experiment <id>: <short name>

## Hypothesis
One sentence prediction.

## Setup
- What's running
- Baseline state
- What changes between runs

## Measurement
- Metric(s)
- Source (JMX / Flink UI / CLI)
- Duration of each run

## Procedure
1. Step by step

## Results
| Run | Parameter | Metric | Notes |
|-----|-----------|--------|-------|

## What I learned
Prose. Surprises? Next question?
```

## 10. Full experiment catalogue (checklist — pick any session)

- [ ] E-P1 Producer baseline vs tuned throughput
- [ ] E-P2 Durability cost of `acks=all`
- [ ] E-P3 Producer idempotence proof
- [ ] E-C1 Rebalance storm measurement
- [ ] E-C2 Auto-commit data loss
- [ ] E-F1 Checkpoint interval sweep
- [ ] E-F2 RocksDB block cache effect
- [ ] E-F3 Watermark pathology + idleness fix
- [ ] E-F4 Parallelism vs partitions mismatch
- [ ] E-F5 Savepoint-based rescaling
- [ ] E-R1 Redis eviction behaviour
- [ ] E-S1 Safe schema add
- [ ] E-S2 Safe field rename via alias
- [ ] E-S3 Unsafe type change rejection
- [ ] E-S4 Required-field trap
- [ ] E-S5 Breaking v2 migration
- [ ] E-X1 Kill broker, observe ISR shrink/recovery
- [ ] E-X2 Kill TaskManager, observe key-group reassignment
- [ ] E-X3 Hot partition (key by h3_cell) under clustered load
- [ ] E-X4 DLQ routing under malformed-event flood
- [ ] E-X5 CDC: change a row in Postgres, trace event through Flink to Redis
