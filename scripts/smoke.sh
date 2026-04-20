#!/usr/bin/env bash
# End-to-end smoke test — Phase 0.8.
#   1. POST 100 events with varied lat/lon across the SF bbox to ingest-api
#   2. sleep 90 (1-min window close + 10s watermark + 10s checkpoint cushion)
#   3. GET /zones/hot?limit=10 from query-api
#   4. Assert .zones | length > 0
set -euo pipefail

INGEST_URL="${INGEST_URL:-http://localhost:8000}"
QUERY_URL="${QUERY_URL:-http://localhost:8001}"
EVENT_COUNT="${SMOKE_EVENT_COUNT:-100}"
SLEEP_SECONDS="${SMOKE_SLEEP_SECONDS:-90}"

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 1; }

echo "[smoke] checking service health"
curl -fsS "${INGEST_URL}/health" >/dev/null || { echo "ERROR: ingest-api not healthy at ${INGEST_URL}"; exit 1; }
curl -fsS "${QUERY_URL}/health"  >/dev/null || { echo "ERROR: query-api not healthy at ${QUERY_URL}";  exit 1; }

echo "[smoke] generating ${EVENT_COUNT} events across SF bbox"
PAYLOADS_FILE="$(mktemp -t rideflow-smoke-XXXXXX.jsonl)"
trap 'rm -f "${PAYLOADS_FILE}"' EXIT

python3 - "${EVENT_COUNT}" "${PAYLOADS_FILE}" <<'PY'
import json, random, sys
count = int(sys.argv[1])
out = sys.argv[2]
rng = random.Random(42)
# SF bounding box (matches simulator/app/city.py)
LAT_MIN, LAT_MAX = 37.70, 37.82
LON_MIN, LON_MAX = -122.52, -122.37
with open(out, "w") as f:
    for i in range(count):
        lat = rng.uniform(LAT_MIN, LAT_MAX)
        lon = rng.uniform(LON_MIN, LON_MAX)
        f.write(json.dumps({
            "driver_id": f"smoke-driver-{i % 20:02d}",
            "lat": lat,
            "lon": lon,
        }) + "\n")
PY

echo "[smoke] posting events to ${INGEST_URL}/events/driver-location"
accepted=0
rejected=0
i=0
while IFS= read -r line; do
    i=$((i+1))
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
        -X POST "${INGEST_URL}/events/driver-location" \
        -H "content-type: application/json" \
        -H "x-trace-id: smoke-$(printf '%03d' "${i}")" \
        --data-binary "${line}")
    if [[ "${code}" == "202" || "${code}" == "200" ]]; then
        accepted=$((accepted+1))
    else
        rejected=$((rejected+1))
        echo "  event ${i} unexpected status ${code}" >&2
    fi
done < "${PAYLOADS_FILE}"

echo "[smoke] posted ${i} events: accepted=${accepted} rejected=${rejected}"
if [[ "${accepted}" -eq 0 ]]; then
    echo "ERROR: no events accepted by ingest-api" >&2
    exit 1
fi

echo "[smoke] sleeping ${SLEEP_SECONDS}s (window close + watermark + checkpoint)"
sleep "${SLEEP_SECONDS}"

echo "[smoke] querying ${QUERY_URL}/zones/hot?limit=10"
response="$(curl -fsS "${QUERY_URL}/zones/hot?limit=10")"
echo "${response}" | jq .

zones_len=$(echo "${response}" | jq '.zones | length')
if [[ "${zones_len}" -gt 0 ]]; then
    window_ms=$(echo "${response}" | jq -r '.window_end_ms')
    top_cell=$(echo "${response}" | jq -r '.zones[0].h3_cell')
    top_count=$(echo "${response}" | jq -r '.zones[0].driver_count')
    echo "[smoke] PASS — ${zones_len} zones from window_end_ms=${window_ms}, top: ${top_cell}=${top_count}"
    exit 0
else
    echo "[smoke] FAIL — .zones is empty (expected > 0)" >&2
    exit 1
fi
