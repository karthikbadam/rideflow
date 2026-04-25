#!/usr/bin/env bash
set -euo pipefail

# POST connect/connectors/debezium-postgres.json to Kafka Connect.
# Idempotent: if the connector already exists, PUT its config instead.

CONNECT_URL="${CONNECT_URL:-http://localhost:8083}"
CONFIG_FILE="${CONFIG_FILE:-connect/connectors/debezium-postgres.json}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "missing $CONFIG_FILE" >&2
  exit 1
fi

name="$(jq -r .name "$CONFIG_FILE")"

# Wait for Connect REST to be up — the base image takes ~60s on first boot.
for i in $(seq 1 30); do
  if curl -sf "$CONNECT_URL/connectors" >/dev/null; then break; fi
  echo "waiting for Connect ($i/30)..."
  sleep 2
done

if curl -sf "$CONNECT_URL/connectors/$name" >/dev/null; then
  echo "connector $name exists — updating config"
  jq .config "$CONFIG_FILE" | curl -sf \
    -X PUT \
    -H "Content-Type: application/json" \
    --data @- \
    "$CONNECT_URL/connectors/$name/config" | jq -r '.name // "updated"'
else
  echo "creating connector $name"
  curl -sf \
    -X POST \
    -H "Content-Type: application/json" \
    --data @"$CONFIG_FILE" \
    "$CONNECT_URL/connectors" | jq -r '.name // "created"'
fi

echo "status:"
curl -sf "$CONNECT_URL/connectors/$name/status" | jq '.connector.state, .tasks[].state'
