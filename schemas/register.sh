#!/usr/bin/env bash
# Register all Avro schemas with Schema Registry.
#
# Strategy (docs/02 §3, §7, §8):
#   - Common types (Envelope, GeoPoint) register first under their record-name
#     subjects so topic schemas can reference them by version.
#   - Core topic schemas register under <topic>-value (TopicNameStrategy) with a
#     `references` array pinning the common-type versions.
#   - DLQ uses one flat schema registered under 4 separate <topic>.dlq-value
#     subjects, no references.
#
# SR global compat was set to BACKWARD at startup (compose env var).
set -euo pipefail

SR_URL="${SCHEMA_REGISTRY_URL:-http://localhost:8081}"
SCHEMAS_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTENT_TYPE="Content-Type: application/vnd.schemaregistry.v1+json"

require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
require jq
require curl

# Wait for SR to be reachable (first run after compose up).
for i in $(seq 1 30); do
  if curl -sf "$SR_URL/subjects" >/dev/null; then break; fi
  [ "$i" -eq 30 ] && { echo "Schema Registry not reachable at $SR_URL"; exit 1; }
  sleep 1
done

# Register one subject. $3 is a JSON array of references (default [] = none).
register() {
  local subject=$1 schema_file=$2 refs=${3:-"[]"}
  local schema_str body resp http_code
  schema_str=$(jq -c . < "$schema_file")
  body=$(jq -n --arg schema "$schema_str" --argjson references "$refs" \
    '{schema: $schema, schemaType: "AVRO", references: $references}')
  resp=$(curl -sS -o /tmp/sr-resp.$$ -w "%{http_code}" \
    -X POST -H "$CONTENT_TYPE" --data "$body" \
    "$SR_URL/subjects/$subject/versions")
  http_code="$resp"
  if [ "$http_code" = "200" ] || [ "$http_code" = "201" ]; then
    local id=$(jq -r '.id' /tmp/sr-resp.$$)
    printf "  OK   %-40s id=%s\n" "$subject" "$id"
  else
    printf "  FAIL %-40s http=%s\n" "$subject" "$http_code"
    cat /tmp/sr-resp.$$; echo
    rm -f /tmp/sr-resp.$$
    exit 1
  fi
  rm -f /tmp/sr-resp.$$
}

# Reference factories — use "latest" version so re-runs after schema edits
# still resolve correctly. Swap to fixed versions if you're pinning for a
# compatibility experiment.
env_ref='[{"name":"rideflow.common.Envelope","subject":"rideflow.common.Envelope","version":-1}]'
env_geo_ref='[{"name":"rideflow.common.Envelope","subject":"rideflow.common.Envelope","version":-1},{"name":"rideflow.common.GeoPoint","subject":"rideflow.common.GeoPoint","version":-1}]'

echo "==> common types"
register "rideflow.common.Envelope" "$SCHEMAS_DIR/common/Envelope.avsc"
register "rideflow.common.GeoPoint" "$SCHEMAS_DIR/common/GeoPoint.avsc"

echo "==> core topic schemas"
register "driver.location.v1-value"   "$SCHEMAS_DIR/driver_location_pinged.avsc"   "$env_ref"
register "ride.request.v1-value"      "$SCHEMAS_DIR/ride_requested.avsc"           "$env_geo_ref"
register "ride.lifecycle.v1-value"    "$SCHEMAS_DIR/ride_lifecycle_changed.avsc"   "$env_ref"
register "anomaly.detected.v1-value"  "$SCHEMAS_DIR/anomaly_detected.avsc"         "$env_ref"

echo "==> DLQ subjects (same schema, 4 subjects)"
for topic in driver.location.v1 ride.request.v1 ride.lifecycle.v1 anomaly.detected.v1; do
  register "${topic}.dlq-value" "$SCHEMAS_DIR/dlq_record.avsc"
done

echo
echo "Subjects now registered:"
curl -s "$SR_URL/subjects" | jq -r '.[]' | sort | sed 's/^/  /'
