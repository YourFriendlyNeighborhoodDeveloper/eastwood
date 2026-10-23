#!/usr/bin/env bash
# Per-turn conversational latency report for recent call(s).
#
# Shows where the time goes between the prospect finishing a sentence and
# hearing a reply — the number that decides whether the call feels
# conversational, which the watchdog (a total-stall backstop) never captures.
#
# Usage:
#   scripts/turn_latency_report.sh          # last 30 minutes
#   scripts/turn_latency_report.sh 10m
#   scripts/turn_latency_report.sh 2h
set -euo pipefail

SINCE="${1:-30m}"
cd "$(dirname "$0")/.."

docker compose logs ai-agent --since "$SINCE" --no-log-prefix 2>/dev/null \
  | grep '"msg":"turn latency"' \
  | python3 scripts/turn_latency_report.py
