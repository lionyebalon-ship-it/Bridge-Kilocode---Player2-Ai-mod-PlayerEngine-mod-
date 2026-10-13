#!/usr/bin/env bash
# mc_cmd.sh — one-liner to drive the running NeoForge server's console.
#
# Writes the given command(s) into the FIFO console opened by start_mc.sh.
#
# usage:
#   bash scripts/mc_cmd.sh <command...>
#   bash scripts/mc_cmd.sh list
#   bash scripts/mc_cmd.sh say hello everyone
#   bash scripts/mc_cmd.sh effect give @a speed 60
#
# env overrides (all optional):
#   P2K_FIFO   fifo path (default /tmp/p2k_mc_fifo)
set -euo pipefail

FIFO="${P2K_FIFO:-/tmp/p2k_mc_fifo}"

if [[ $# -eq 0 ]]; then
  echo "usage: mc_cmd.sh <command...>" >&2
  exit 2
fi

if [[ ! -p "$FIFO" ]]; then
  echo "console fifo $FIFO does not exist — start the server first: bash scripts/start_mc.sh" >&2
  exit 1
fi

printf '%s\n' "$*" > "$FIFO"
