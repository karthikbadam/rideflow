# Experiments

This directory holds the per-experiment artifacts (notes, configs, scripts,
measurements) for the 23 experiments listed in `docs/03-tuning.md`.

## Template

For each experiment, create a subdirectory `E-<area><N>/` containing:

```
E-P1/
├── README.md        # hypothesis, setup, predicted outcome, actual outcome
├── baseline.env     # .env overrides for the baseline run
├── tuned.env        # .env overrides for the tuned run
└── results.md       # raw numbers + conclusions
```

## Running an experiment

```bash
# start from clean
make reset && make up && make register-schemas && make submit-hot-zones

# baseline
docker compose --env-file experiments/E-P1/baseline.env up -d ingest-api simulator
# collect metrics, then:
docker compose --env-file experiments/E-P1/tuned.env up -d ingest-api simulator
```

Keep the `.env` under version control alongside `results.md` so the comparison
is reproducible.

## Phase 3-6 experiments (to flesh out)

- **E-P1..E-P3** producer: batching/compression, acks, idempotence
- **E-C1..E-C2** consumer: rebalance strategies, manual-commit vs auto-commit
- **E-F1..E-F5** Flink: checkpointing, watermarks, parallelism, rescaling
- **E-S1..E-S5** schema: compatibility modes, evolution breakage
- **E-R1, E-X1..E-X5** Redis + failure: eviction, broker kills, partial outages
