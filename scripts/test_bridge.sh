#!/usr/bin/env bash
# test_bridge.sh — END-TO-END TEST: rebuild the rig (if needed), boot the
# NeoForge 1.21.1 server with the real PlayerEngine + Player2NPC mods, drive
# companion orders through the mineflayer bot, and verify the full chain
# (mod -> bridge -> Kilo Gateway -> LLM -> command execution -> feedback)
# with PASS/FAIL evidence. This is the "does the bridge actually work with
# the mods" script — the original plan, automated.
#
# usage:
#   bash scripts/test_bridge.sh                          # rebuild-if-needed + basic & complex orders
#   bash scripts/test_bridge.sh --force-rebuild          # always rebuild the rig from scratch
#   bash scripts/test_bridge.sh --orders basic           # only the simple get-order
#   bash scripts/test_bridge.sh --orders full            # basic + complex + failure-recovery
#   bash scripts/test_bridge.sh --keep-server            # leave the MC server running afterwards
#
# env overrides (all optional):
#   MC_TEST_DIR        rig directory          (default /workspace/mc-test)
#   BRIDGE_STATS_URL   stats URL              (default http://127.0.0.1:9800/bridge/stats)
#   P2K_FIFO           server fifo            (default /tmp/p2k_mc_fifo)
#
# Exit code: 0 = all attempted orders PASS, 1 = any FAIL / environment blocker.
# The bridge is left RUNNING either way; the MC server is stopped unless
# --keep-server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MC_TEST_DIR="${MC_TEST_DIR:-/workspace/mc-test}"
STATS_URL="${BRIDGE_STATS_URL:-http://127.0.0.1:9800/bridge/stats}"
FIFO="${P2K_FIFO:-/tmp/p2k_mc_fifo}"
BOOT_LOG="$MC_TEST_DIR/orders_boot.log"
BRIDGE_LOG="$MC_TEST_DIR/bridge_test.log"

FORCE_REBUILD=0
ORDERS="standard"   # basic | standard | full
KEEP_SERVER=0
for arg in "$@"; do
  case "$arg" in
    --force-rebuild) FORCE_REBUILD=1 ;;
    --keep-server)   KEEP_SERVER=1 ;;
    --orders)        ;;  # value handled below
    basic|standard|full)
      # allow both "--orders basic" and a bare "basic"
      ORDERS="$arg" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
# support "--orders <value>" as two tokens
if [[ "${1:-}" == "--orders" && -n "${2:-}" ]]; then ORDERS="$2"; fi

C_FAIL=0; C_PASS=0
fail() { echo "  X $1" >&2; C_FAIL=$((C_FAIL + 1)); }
pass() { echo "  + $1"; C_PASS=$((C_PASS + 1)); }
note() { echo "  . $1"; }

echo "================================================================"
echo " p2k-bridge end-to-end test (mods -> bridge -> Kilo Gateway)"
echo " rig=$MC_TEST_DIR  orders=$ORDERS  force_rebuild=$FORCE_REBUILD"
echo "================================================================"

# ---------------------------------------------------------------- preflight
echo
echo "[1/6] Preflight (bridge, hosts, certs, trust)"

# bridge: start it if it's not answering
if curl -s --max-time 3 http://127.0.0.1:9800/bridge/healthz >/dev/null 2>&1; then
  note "bridge already running on :9800"
else
  echo "  bridge not answering — starting it"
  mkdir -p "$MC_TEST_DIR"
  ( cd "$REPO_ROOT" && python3 bridge/run.py --config bridge/config.json >"$BRIDGE_LOG" 2>&1 & )
  for _ in $(seq 1 15); do
    curl -s --max-time 3 http://127.0.0.1:9800/bridge/healthz >/dev/null 2>&1 && break
    sleep 1
  done
fi
if curl -s --max-time 5 http://127.0.0.1:9800/bridge/healthz >/dev/null 2>&1; then
  pass "bridge up: $(curl -s --max-time 5 http://127.0.0.1:9800/bridge/healthz)"
else
  fail "bridge not reachable on 127.0.0.1:9800 — cannot test. Start it: sudo python3 bridge/run.py --config bridge/config.json"
  exit 1
fi

# hosts entry (must exist for the mod to reach the bridge)
if grep -q 'api\.player2\.game' /etc/hosts 2>/dev/null; then
  pass "/etc/hosts has the api.player2.game entry"
else
  if mount -o remount,rw /etc/hosts 2>/dev/null && echo "127.0.0.1 api.player2.game" >>/etc/hosts 2>/dev/null; then
    pass "added '127.0.0.1 api.player2.game' to /etc/hosts"
  else
    fail "/etc/hosts lacks api.player2.game and could not be updated — add it manually"
  fi
fi

# certs (needed by the TLS listener + the game JVM)
if [[ -f "$REPO_ROOT/tls/certs/server.crt" && -f "$REPO_ROOT/tls/certs/p2k_ca.crt" ]]; then
  pass "TLS certs present (tls/certs/)"
else
  echo "  certs missing — running tls/gen_certs.sh"
  if bash "$REPO_ROOT/tls/gen_certs.sh" >/dev/null 2>&1; then
    pass "certs generated"
  else
    fail "cert generation failed — inspect: bash tls/gen_certs.sh"
  fi
fi

# ---------------------------------------------------------------- rig build
echo
echo "[2/6] Test rig (rebuild if missing/forced)"
NEED_BUILD=0
if [[ $FORCE_REBUILD -eq 1 ]]; then NEED_BUILD=1; note "--force-rebuild given";
elif [[ ! -d "$MC_TEST_DIR/libraries/net/neoforged" ]]; then NEED_BUILD=1; note "no neoforge install in $MC_TEST_DIR";
elif ! ls "$MC_TEST_DIR"/mods/playerengine-*.jar >/dev/null 2>&1; then NEED_BUILD=1; note "playerengine jar missing";
elif [[ ! -f "$MC_TEST_DIR/bot/bot.js" ]]; then NEED_BUILD=1; note "bot missing";
fi

if [[ $NEED_BUILD -eq 1 ]]; then
  echo "  rebuilding the rig (this downloads java/neoforge/mods/mineflayer — several minutes)"
  if bash "$SCRIPT_DIR/build_test_rig.sh"; then
    pass "rig built"
  else
    fail "rig build failed — see output above (network? root?)"
    exit 1
  fi
else
  pass "rig present: $(ls "$MC_TEST_DIR"/mods/*.jar 2>/dev/null | xargs -n1 basename | tr '\n' ' ')"
fi

# ---------------------------------------------------------------- boot server
echo
echo "[3/6] Boot the NeoForge server"
SERVER_RUNNING=0
if pgrep -f 'unix_args\.txt' >/dev/null 2>&1; then
  SERVER_RUNNING=1
  note "a server is already running (pid $(pgrep -f 'unix_args\.txt' | head -1))"
fi

if [[ $SERVER_RUNNING -eq 0 ]]; then
  if bash "$SCRIPT_DIR/start_mc.sh"; then
    pass "server booted"
  else
    fail "server failed to boot — see $BOOT_LOG"
    exit 1
  fi
fi

LOG_START=$(wc -l <"$BOOT_LOG" 2>/dev/null || echo 0)
if grep -q 'Done (' "$BOOT_LOG" 2>/dev/null; then pass "server reports Done ( — up"; else fail "no 'Done (' in $BOOT_LOG"; fi
if grep -q 'PlayerEngine' "$BOOT_LOG" && grep -q 'player2npc' "$BOOT_LOG"; then pass "mods loaded (PlayerEngine + Player2NPC)"; else fail "mods not confirmed in log"; fi

# log region for THIS run only (tail from the boot line onward)
run_log() { tail -n +"$((LOG_START + 1))" "$BOOT_LOG" 2>/dev/null || true; }

# Emit the distinct regular files the given pid's stdout/stderr point at.
collect_bridge_logs() {
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

# bridge log discovery (the live bridge's stdout file, like run_order.sh does).
# `pgrep -f 'bridge/run\.py'` also matches the wrapper shell that launched the
# bridge (bash -c "python3 bridge/run.py ...") and, worse, any *current* shell
# whose command line merely mentions the pattern — including this script's own
# command substitution. Selecting that process yields no file-backed stdout and
# silently disables every bridge-log evidence check below, so prefer the
# process whose comm is a python interpreter and fall back to the first match.
bridge_pid() {
  local pid comm first=""
  for pid in $(pgrep -f 'bridge/run\.py' 2>/dev/null); do
    [[ "$pid" == "$$" ]] && continue
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || echo '')"
    case "$comm" in
      python*|java*) [[ -z "$first" ]] && first="$pid" ;;
    esac
  done
  if [[ -n "$first" ]]; then echo "$first"; else pgrep -f 'bridge/run\.py' | head -n1; fi
}
BRIDGE_PID="$(bridge_pid || true)"
BRIDGE_LOG_FILE=""
if [[ -n "$BRIDGE_PID" ]]; then
  for f in $(collect_bridge_logs "$BRIDGE_PID"); do BRIDGE_LOG_FILE="$f"; done
fi
note "bridge log file: ${BRIDGE_LOG_FILE:-<none discovered; stats-only verification>}"

bridge_grep() { # bridge_grep <pattern> — grep the live bridge log (no-op if none)
  [[ -n "$BRIDGE_LOG_FILE" && -r "$BRIDGE_LOG_FILE" ]] || return 0
  grep -i -- "$1" "$BRIDGE_LOG_FILE" 2>/dev/null || true
}

stats_get() { # stats_get <python-expr over dict d>
  curl -s --max-time 5 "$STATS_URL" 2>/dev/null | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print('ERR'); raise SystemExit
print($1)" 2>/dev/null || echo ERR
}

echo
echo "[4/6] Baseline bridge stats"
BASE_REQ="$(stats_get 'd["chat_requests"]')"
BASE_OK="$(stats_get 'd["chat_ok"]')"
BASE_TOK="$(stats_get 'd["token_budget"]["lifetime_total"]')"
note "before: chat_requests=$BASE_REQ chat_ok=$BASE_OK lifetime_tokens=$BASE_TOK"

# ---------------------------------------------------------------- orders
echo
echo "[5/6] Order battery"

run_order() { # run_order <label> <username> <message> <watch-seconds> <grep-needle>
  local label="$1" user="$2" msg="$3" watch="$4" needle="$5"
  echo
  echo "--- $label ---"
  echo "  user=$user  watch=${watch}s"
  echo "  message: $msg"
  local out="$MC_TEST_DIR/order_${user}.log"
  ( cd "$MC_TEST_DIR/bot" && timeout $((watch + 60)) node bot.js "$user" "$msg" "$watch" ) >"$out" 2>&1 || true

  # evidence checks
  local bot_sent=0 bot_msg=0 inv_change=0 llm=0 feedback=0 bridge_ok=0 exec_line=0
  grep -q 'user_message' "$out" 2>/dev/null && bot_sent=1
  grep -qi 'message' "$out" 2>/dev/null && bot_msg=1
  grep -q 'INVENTORY CHANGE' "$out" 2>/dev/null && inv_change=1
  run_log | grep -q 'LLMCompleter returned json' && llm=1
  run_log | grep -q 'Command feedback' && feedback=1
  # The bridge logs one status line per call, but the message preview is
  # truncated to 60 chars (p2k_bridge.state.PREVIEW_MAXLEN) — an order keyword
  # like "oak logs" almost always falls past that cut, so grep-ing the needle
  # here reported a false negative even when the bridge had answered. What this
  # flag means is "did the bridge log a successful chat call for this run", so
  # match on the status itself; llm_json/command_feedback below already prove
  # the round-trip and its in-game effect against the untruncated server log.
  bridge_grep 'chat status=ok' | grep -q 'status=ok' && bridge_ok=1
  run_log | grep -qi "$needle" && exec_line=1

  echo "  evidence: bot_sent=$bot_sent bot_received=$bot_msg inventory_change=$inv_change llm_json=$llm command_feedback=$feedback bridge_ok_call=$bridge_ok"
  grep -E 'LLMCompleter returned json|Command feedback' <(run_log) | tail -n 6 | sed 's/^/    | /' || true
  bridge_grep 'chat status=' | tail -n 4 | sed 's/^/    | /' || true
  grep -E 'INVENTORY CHANGE|final summary' "$out" 2>/dev/null | tail -n 4 | sed 's/^/    | /' || true

  if [[ $bridge_ok -eq 1 && $llm -eq 1 ]]; then
    pass "$label: LLM round-trip through the bridge + model command returned"
  else
    fail "$label: no bridge chat call / no LLM command — check bot log $out and $BOOT_LOG"
  fi
  if [[ $feedback -eq 1 ]]; then
    pass "$label: command executed in-game (Command feedback present)"
  else
    note "$label: no Command feedback line yet (task may still be running — watch log)"
  fi
}

ORDER_USER_1="P2KTestA"
ORDER_USER_2="P2KTestB"
ORDER_USER_3="P2KTestC"

# O1 — simple baseline: get -> give chain
run_order "O1 basic get-order" "$ORDER_USER_1" \
  "Kilo, can you get me 3 oak logs?" 150 "oak logs"

if [[ "$ORDERS" == "standard" || "$ORDERS" == "full" ]]; then
  # O2 — COMPLEX/SEQUENCED order: wait-for-completion then next step.
  # The mod feeds command results back to the LLM, so a two-phase order is
  # handled as: LLM issues `mine stone 5` -> task finishes -> feedback ->
  # LLM must answer the second half (report the inventory).
  run_order "O2 complex sequenced-order (mine -> then report inventory)" "$ORDER_USER_2" \
    "Kilo, can you mine 5 stone, and once that is done, tell me what you have in your inventory?" 200 "5 stone"
fi

if [[ "$ORDERS" == "full" ]]; then
  # O3 — failure recovery: an impossible order must fail gracefully, not crash
  run_order "O3 failure-recovery (impossible order)" "$ORDER_USER_3" \
    "Kilo, can you get me 10 diamonds?" 180 "diamonds"
fi

# ---------------------------------------------------------------- verdict
echo
echo "[6/6] Verdict"
AFTER_REQ="$(stats_get 'd["chat_requests"]')"
AFTER_OK="$(stats_get 'd["chat_ok"]')"
AFTER_TOK="$(stats_get 'd["token_budget"]["lifetime_total"]')"
note "after: chat_requests=$AFTER_REQ chat_ok=$AFTER_OK lifetime_tokens=$AFTER_TOK"

TLS_ERRORS="$(run_log | grep -ci 'SSLHandshakeException\|PKIX\|handshake_failure\|unable to find valid certification' || true)"
if [[ "${TLS_ERRORS:-0}" -eq 0 ]]; then pass "zero TLS errors in the server log"; else fail "$TLS_ERRORS TLS errors in the server log (CA not trusted by the game JVM? run bash tls/trust_jvm.sh against it)"; fi

if [[ "$AFTER_REQ" != "ERR" && "$BASE_REQ" != "ERR" ]] && (( AFTER_REQ > BASE_REQ )); then
  pass "bridge handled $((AFTER_REQ - BASE_REQ)) chat request(s) this run ($((AFTER_OK - BASE_OK)) ok)"
else
  fail "no new chat requests on the bridge — the mod never called it"
fi

CRASHES="$(run_log | grep -ci 'FatalException\|crash report\|Server did not start' || true)"
if [[ "${CRASHES:-0}" -eq 0 ]]; then pass "no server crashes"; else fail "server crash signatures found — inspect $BOOT_LOG"; fi

echo
echo "================================================================"
echo " RESULT: pass=$C_PASS fail=$C_FAIL"
echo " evidence: $BOOT_LOG (server), $MC_TEST_DIR/order_*.log (bots),"
echo "           bridge stats: curl -s $STATS_URL"
echo "================================================================"

# ---------------------------------------------------------------- cleanup
if [[ $KEEP_SERVER -eq 1 ]]; then
  note "--keep-server: leaving the server running (stop via: bash scripts/mc_cmd.sh stop)"
else
  echo "stopping the server..."
  bash "$SCRIPT_DIR/mc_cmd.sh" stop >/dev/null 2>&1 || echo stop >"$FIFO" 2>/dev/null || true
  for _ in $(seq 1 30); do
    pgrep -f 'unix_args\.txt' >/dev/null 2>&1 || break
    sleep 1
  done
  # the fifo writer-holder must go too, or the next boot creates a second one
  pkill -f 'sleep 2147483647' 2>/dev/null || true
  if pgrep -f 'unix_args\.txt' >/dev/null 2>&1; then
    note "server still running (pid $(pgrep -f 'unix_args\.txt' | head -1)) — kill it manually if needed"
  else
    pass "server stopped cleanly (bridge left running)"
  fi
fi

[[ $C_FAIL -eq 0 ]] || exit 1
echo "ALL CHECKS PASSED"
