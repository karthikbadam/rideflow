#!/usr/bin/env bash
# Wipe every subject. Used between compatibility experiments (doc/03 E-S series).
# Does soft delete first, then permanent (=true) to free subject names for reuse.
set -euo pipefail

SR_URL="${SCHEMA_REGISTRY_URL:-http://localhost:8081}"
require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
require jq
require curl

subjects=$(curl -sf "$SR_URL/subjects" | jq -r '.[]?')
if [ -z "$subjects" ]; then
  echo "no subjects to delete"
  exit 0
fi

echo "==> soft delete"
for s in $subjects; do
  printf "  %s -> " "$s"
  curl -sf -X DELETE "$SR_URL/subjects/$s" | jq -c .
done

echo "==> permanent delete"
for s in $subjects; do
  printf "  %s -> " "$s"
  curl -sf -X DELETE "$SR_URL/subjects/$s?permanent=true" | jq -c .
done

echo "done. remaining subjects: $(curl -s "$SR_URL/subjects" | jq -r '. | length')"
