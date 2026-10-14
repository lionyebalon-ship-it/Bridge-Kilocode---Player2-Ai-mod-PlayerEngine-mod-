#!/usr/bin/env bash
# run_order.sh — run one companion order through the bot and report results.
#
# Drives the mineflayer bot (see scripts/bot_template.js) with a single
# username + message, tees its output to bot_out.log, then prints the bot log
# tail, the bridge's most recent chat log lines, and the live /bridge/stats
# chat counters — everything needed to confirm the order ran and what changed.
#
# usage:
#   bash scripts/run_order.sh "<username>" "<message>" [seconds]
#   bash scripts/run_order.sh TestBot "Kilo please get me 3 oak logs" 90
#
# If [seconds] is omitted the bot uses its own default watchdog (170s).
#
# env overrides (all optional):
#   MC_TEST_DIR      rig directory (default /workspace/mc-test)
#   BRIDGE_STATS_URL bridge stats URL (default http://127.0.0.1:9800/bridge/stats)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MC_TEST_DIR="${MC_TEST_DIR:-/workspace/mc-test}"
BOT_DIR="$MC_TEST_DIR/bot"
BOT_LOG="$MC_TEST_DIR/bot_out.log"
STATS_URL="${BRIDGE_STATS_URL:-http://127.0.0.1:9800/bridge/stats}"

USERNAME="${1:-}"
MESSAGE="${2:-}"
SECONDS_WATCH="${3:-}"

if [[ -z "$USERNAME" || -z "$MESSAGE" ]]; then
  echo "usage: run_order.sh <username> \"<message>\" [seconds]" >&2
  exit 2
fi

if [[ ! -f "$BOT_DIR/bot.js" ]]; then
  echo "bot not found at $BOT_DIR/bot.js — build the rig first: bash scripts/build_test_rig.sh" >&2
  exit 1
fi

# --- run the bot (its own watchdog decides when it exits) ---------------
echo "=== running order: user=$USERNAME watch=${SECONDS_WATCH:-<default>} ==="
echo "=== message: $MESSAGE ==="
set +e
if [[ -n "$SECONDS_WATCH" ]]; then
  ( cd "$BOT_DIR" && node bot.js "$USERNAME" "$MESSAGE" "$SECONDS_WATCH" ) 2>&1 | tee "$BOT_LOG"
else
  ( cd "$BOT_DIR" && node bot.js "$USERNAME" "$MESSAGE" ) 2>&1 | tee "$BOT_LOG"
fi
BOT_RC=${PIPESTATUS[0]}
set -e
echo
echo "=== bot log tail ($BOT_LOG, exit=$BOT_RC) ==="
tail -n 30 "$BOT_LOG" 2>/dev/null || true

# --- bridge chat log lines (discover the live bridge log via ps) ---------
# `pgrep -f 'bridge/run\.py'` also matches the wrapper shell that launched the
# bridge (bash -c "python3 bridge/run.py ..."), whose stdout is a socket/pipe
# rather than the bridge's own log file — picking it silently yields no log.
# Prefer the process whose comm is a python interpreter.
bridge_pid() {
  local pid comm first=""
  for pid in $(pgrep -f 'bridge/run\.py' 2>/dev/null); do
    [[ "$pid" == "$$" ]] && continue
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || echo '')"
    case "$comm" in python*|java*) [[ -z "$first" ]] && first="$pid" ;; esac
  done
  if [[ -n "$first" ]]; then echo "$first"; else pgrep -f 'bridge/run\.py' | head -n1; fi
}
echo
echo "=== bridge chat log (live) ==="
BRIDGE_PID="$(bridge_pid || true)"
if [[ -z "$BRIDGE_PID" ]]; then
  BRIDGE_PID="$(pgrep -f 'run\.py' | head -n1 || true)"
fi

collect_bridge_logs() {
  # Emit the distinct regular files the bridge's stdout/stderr point at.
  local pid="$1" fd tgt seen=""
  for fd in 1 2; do
    [[ -r "/proc/$pid/fd/$fd" ]] || continue
    tgt="$(readlink "/proc/$pid/fd/$fd" 2>/dev/null || true)"
    case "$tgt" in
      /*) if [[ -f "$tgt" ]]; then
            case " $seen " in *" $tgt "*) : ;; *) seen="$seen $tgt"; echo "$tgt" ;; esac
          fi ;;
    esac
  done
}

if [[ -n "$BRIDGE_PID" ]]; then
  mapfile -t BLOGS < <(collect_bridge_logs "$BRIDGE_PID")
  if [[ ${#BLOGS[@]} -eq 0 ]]; then
    echo "bridge pid=$BRIDGE_PID has no file-backed stdout/stderr (log not capturable)"
  else
    for f in "${BLOGS[@]}"; do
      echo "--- $f ---"
      # prefer lines that mention chat, fall back to a plain tail
      grep -i 'chat' "$f" 2>/dev/null | tail -n 12 || tail -n 12 "$f" 2>/dev/null || true
    done
  fi
else
  echo "bridge process not found (is it running? sudo python3 bridge/run.py ...)"
fi

# --- live /bridge/stats chat counters ------------------------------------
echo
echo "=== bridge /bridge/stats chat counters ($STATS_URL) ==="
STATS="$(curl -s --max-time 5 "$STATS_URL" 2>/dev/null || true)"
if [[ -z "$STATS" ]]; then
  echo "could not reach $STATS_URL (bridge down?)"
elif command -v python3 >/dev/null 2>&1; then
  printf '%s' "$STATS" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(sys.stdin.read()); sys.exit()
keys = {k: v for k, v in d.items() if "chat" in k.lower() or k in ("requests", "rate_limited", "kilo_errors")}
print("chat counters:")
for k in sorted(keys):
    print(f"  {k} = {keys[k]}")
tb = d.get("token_budget")
if isinstance(tb, dict):
    print("token_budget:", ", ".join(f"{k}={v}" for k, v in sorted(tb.items())))
' 2>/dev/null || printf '%s\n' "$STATS"
else
  printf '%s\n' "$STATS"
fi
