#!/usr/bin/env bash
# Full teardown: stops services and removes named volumes.
# Destroys all Kafka log segments, Flink checkpoints, Postgres data.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> docker compose down -v"
docker compose down -v --remove-orphans

echo "==> pruning dangling volumes tagged for this project"
docker volume ls --filter "label=com.docker.compose.project=rideflow" -q | xargs -r docker volume rm

echo "==> done. run 'make up' to start fresh."
