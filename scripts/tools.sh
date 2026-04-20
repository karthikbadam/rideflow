#!/usr/bin/env bash
# Wrappers around Kafka CLI tools, run inside kafka-0 container.
# Usage: scripts/tools.sh <command> [args...]
#   topics-list                      list topics with partition counts
#   topic-describe <topic>           describe a topic
#   consume <topic> [--from-start]   console consumer (Avro not decoded)
#   groups-list                      list consumer groups
#   group-describe <group>           describe a consumer group (lag, members)
set -euo pipefail

BOOT="kafka-0:9092"
EXEC="docker compose exec -T kafka-0"

cmd="${1:-help}"
shift || true

case "$cmd" in
  topics-list)
    $EXEC kafka-topics --bootstrap-server "$BOOT" --list
    ;;
  topic-describe)
    $EXEC kafka-topics --bootstrap-server "$BOOT" --describe --topic "${1:?topic required}"
    ;;
  consume)
    topic="${1:?topic required}"; shift || true
    extra=()
    [[ "${1:-}" == "--from-start" ]] && extra+=(--from-beginning)
    $EXEC kafka-console-consumer --bootstrap-server "$BOOT" --topic "$topic" "${extra[@]}"
    ;;
  groups-list)
    $EXEC kafka-consumer-groups --bootstrap-server "$BOOT" --list
    ;;
  group-describe)
    $EXEC kafka-consumer-groups --bootstrap-server "$BOOT" --describe --group "${1:?group required}"
    ;;
  help|*)
    sed -n '2,12p' "$0"
    ;;
esac
