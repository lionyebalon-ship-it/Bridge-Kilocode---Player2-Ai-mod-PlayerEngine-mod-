#!/usr/bin/env bash
# start_mc.sh — boot the NeoForge test server with a FIFO console.
#
# Leaves the server RUNNING and hands you a writable console through a named
# pipe. The pipe is held open by a background writer so the JVM's stdin never
# sees EOF (which would shut the server down). Send commands with mc_cmd.sh:
#
# usage:
#   bash scripts/start_mc.sh              # boot the server and stay running
#   bash scripts/mc_cmd.sh <command...>   # drive the console afterwards
#   echo 'say hello' > /tmp/p2k_mc_fifo    # direct fifo write (equivalent)
#
# env overrides (all optional):
#   MC_TEST_DIR       rig directory     (default /workspace/mc-test)
#   NEOFORGE_VERSION  NeoForge version  (default 21.1.201)
#   P2K_FIFO          fifo path         (default /tmp/p2k_mc_fifo)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MC_TEST_DIR="${MC_TEST_DIR:-/workspace/mc-test}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.201}"
FIFO="${P2K_FIFO:-/tmp/p2k_mc_fifo}"
UNIX_ARGS="libraries/net/neoforged/neoforge/${NEOFORGE_VERSION}/unix_args.txt"
BOOT_LOG="$MC_TEST_DIR/orders_boot.log"

# --- prepare the FIFO console ------------------------------------------
# mkfifo -f style behaviour without clobbering a live pipe.
if [[ -p "$FIFO" ]]; then
  :
elif [[ -e "$FIFO" ]]; then
  rm -f "$FIFO"
fi
if [[ ! -p "$FIFO" ]]; then
  mkfifo "$FIFO"
  echo "created fifo: $FIFO"
fi

# Writer-holder FIRST: opening the fifo for writing blocks until a reader
# appears, so background it — the JVM becomes the reader. This keeps the fifo
# permanently open so the server never gets EOF on stdin.
sleep 2147483647 > "$FIFO" &
HOLDER_PID=$!
echo "fifo holder pid=$HOLDER_PID"

mkdir -p "$MC_TEST_DIR"

# --- boot the server ----------------------------------------------------
# @user_jvm_args.txt (heap) + @unix_args.txt (neoforge classpath/module args).
# Run from MC_TEST_DIR so the relative @library paths resolve. stdin <- fifo.
( cd "$MC_TEST_DIR" && exec java @user_jvm_args.txt "@$UNIX_ARGS" nogui ) \
  < "$FIFO" > "$BOOT_LOG" 2>&1 &
SERVER_PID=$!
echo "server pid=$SERVER_PID — boot log: $BOOT_LOG"
echo

# --- wait for "Done (" ---------------------------------------------------
echo "waiting for the server to finish loading (up to 240s)..."
DONE=0
for _ in $(seq 1 240); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server process exited early — last 40 log lines:"
    tail -n 40 "$BOOT_LOG" 2>/dev/null || true
    exit 1
  fi
  if grep -q 'Done (' "$BOOT_LOG" 2>/dev/null; then
    DONE=1
    break
  fi
  sleep 1
done

echo
echo "===== boot log tail ($BOOT_LOG) ====="
tail -n 25 "$BOOT_LOG" 2>/dev/null || true
echo "====================================="
if [[ $DONE -eq 1 ]]; then
  echo "server is UP. Send console commands with any of:"
  echo "  bash scripts/mc_cmd.sh <command...>"
  echo "  echo '<command>' > $FIFO"
else
  echo "WARNING: did not see \"Done (\" after 240s — the server may still be loading; check $BOOT_LOG" >&2
fi

echo "server left running (pid=$SERVER_PID, fifo=$FIFO). Stop with 'stop' via the console or: kill $SERVER_PID $HOLDER_PID"
