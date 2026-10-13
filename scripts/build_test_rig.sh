#!/usr/bin/env bash
# build_test_rig.sh — full from-scratch rebuild of the p2k-bridge NeoForge test rig.
#
# Rebuilds /workspace/mc-test so a total environment wipe is recoverable with
# ONE command. Touch nothing but MC_TEST_DIR (and system packages/trust stores).
#
# usage:
#   bash scripts/build_test_rig.sh
#
# env overrides (all optional):
#   MC_TEST_DIR        target rig directory         (default /workspace/mc-test)
#   MC_VERSION         Minecraft version            (default 1.21.1)
#   NEOFORGE_VERSION   NeoForge version             (default 21.1.201)
#   MC_MEM             max heap for the server JVM  (default 1500M)
#
# Safe to re-run: every step is idempotent (existing files are overwritten,
# downloads are re-fetched, package installs are no-ops when already present).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

MC_TEST_DIR="${MC_TEST_DIR:-/workspace/mc-test}"
MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.201}"
MC_MEM="${MC_MEM:-1500M}"

ATTENTION=0
warn() { # non-fatal warning (kept going, counted for the summary)
  echo "  ! $1" >&2
  ATTENTION=$((ATTENTION + 1))
}
die() { # fatal error (stop with a clear message)
  echo "  X $1" >&2
  exit 1
}

echo "p2k-bridge test-rig build"
echo "  MC_TEST_DIR      = $MC_TEST_DIR"
echo "  MC_VERSION       = $MC_VERSION"
echo "  NEOFORGE_VERSION = $NEOFORGE_VERSION"
echo "  MC_MEM           = $MC_MEM"
echo

# --- 1. Preflight --------------------------------------------------------
echo "[1/7] Preflight"

# java — required to run the installer and the server
if command -v java >/dev/null 2>&1; then
  echo "  java present: $(java -version 2>&1 | head -1)"
else
  echo "  java missing — installing openjdk-21-jdk-headless"
  apt-get update -y >/dev/null 2>&1 || true
  if apt-get install -y openjdk-21-jdk-headless >/dev/null 2>&1; then
    echo "  java installed: $(java -version 2>&1 | head -1)"
  else
    die "could not install openjdk-21-jdk-headless (needs root/apt) — install a JDK 21 and re-run"
  fi
fi

# node + npm for the bot (non-fatal: the rig still builds server + mods)
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  echo "  node present: $(node --version)"
else
  warn "node/npm missing — the mineflayer bot will NOT be installed (orders need it)"
fi

# basic tools
for tool in curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found (install it and re-run)"
done

# bridge should be running for the mods to reach (checked later via healthz)
echo "  bridge check: $(curl -s --max-time 3 http://127.0.0.1:9800/bridge/healthz || echo 'NOT RUNNING (start: sudo python3 bridge/run.py)')"

# /etc/hosts must point the game domain at the bridge
if grep -q 'api\.player2\.game' /etc/hosts 2>/dev/null; then
  echo "  hosts entry present"
else
  if mount -o remount,rw /etc/hosts 2>/dev/null && echo "127.0.0.1 api.player2.game" >> /etc/hosts 2>/dev/null; then
    echo "  hosts entry added"
  else
    warn "could not add the /etc/hosts entry — add '127.0.0.1 api.player2.game' manually"
  fi
fi

# sandbox TLS-intercepting proxy: if external HTTPS fails, trust its CA
if [[ -d /etc/cloudflare/certs ]] && ls /etc/cloudflare/certs/*.crt >/dev/null 2>&1; then
  echo "  sandbox egress CA detected — importing into the system store"
  cp -f /etc/cloudflare/certs/*.crt /usr/local/share/ca-certificates/ 2>/dev/null || true
  update-ca-certificates >/dev/null 2>&1 || true
fi

# --- 2. NeoForge server install ------------------------------------------
echo
echo "[2/7] NeoForge $NEOFORGE_VERSION server"
mkdir -p "$MC_TEST_DIR"
cd "$MC_TEST_DIR"
INSTALLER="neoforge-${NEOFORGE_VERSION}-installer.jar"
if [[ ! -f "libraries/net/neoforged/neoforge/${NEOFORGE_VERSION}/unix_args.txt" ]]; then
  curl -fsSL -o "$INSTALLER" \
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/${NEOFORGE_VERSION}/${INSTALLER}" \
    || die "failed to download the NeoForge installer (network?)"
  java -jar "$INSTALLER" --installServer "$MC_TEST_DIR" \
    || die "installer failed — see output above"
else
  echo "  neoforge already installed — skipping"
fi

# --- 3. Server files -------------------------------------------------------
echo
echo "[3/7] Server files (eula / properties / jvm args)"
echo "eula=true" > "$MC_TEST_DIR/eula.txt"
cat > "$MC_TEST_DIR/server.properties" <<'EOF'
online-mode=false
motd=p2k-bridge test rig
level-name=world
max-players=10
view-distance=8
allow-flight=true
EOF
echo "-Xmx${MC_MEM} -Xms512M" > "$MC_TEST_DIR/user_jvm_args.txt"
echo "  eula=true, online-mode=false (offline bot joins need this), heap ${MC_MEM}"

# --- 4. Mods from Modrinth -------------------------------------------------
echo
echo "[4/7] Mods (Modrinth API, newest neoforge 1.21.1 builds)"
mkdir -p "$MC_TEST_DIR/mods"
declare -A PROJECTS=(
  [playerengine]=nUOXthgN
  [player2npc]=CB68UWRl
  [architectury-api]=lhGA9TYQ
)
for name in "${!PROJECTS[@]}"; do
  pid="${PROJECTS[$name]}"
  url="$(
    curl -fsSL "https://api.modrinth.com/v2/project/${pid}/version?loaders=%5B%22neoforge%22%5D&game_versions=%5B%22${MC_VERSION}%22%5D" \
    | python3 -c "
import sys, json
vers = json.load(sys.stdin)
if not vers: sys.exit(1)
v = vers[0]
f = next((f for f in v['files'] if f.get('primary')), v['files'][0])
print(f['url'])
" 2>/dev/null || true
  )"
  if [[ -z "$url" ]]; then
    warn "no ${MC_VERSION}/neoforge version found for ${name} (${pid}) — check the modrinth page"
    continue
  fi
  fname="$(basename "$url")"
  echo "  ${name} -> mods/${fname}"
  curl -fsSL -o "$MC_TEST_DIR/mods/${fname}" "$url" \
    || warn "download failed for ${name}: ${url}"
done

# --- 5. JVM trust for the bridge CA ----------------------------------------
echo
echo "[5/7] JVM trust"
JAVA_HOME_GUESS="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
if [[ -f "$JAVA_HOME_GUESS/lib/security/cacerts" ]]; then
  if bash "$REPO_ROOT/tls/trust_jvm.sh" "$JAVA_HOME_GUESS" >/dev/null 2>&1; then
    echo "  p2k-bridge CA trusted in: $JAVA_HOME_GUESS/lib/security/cacerts"
  else
    warn "trust_jvm.sh failed for ${JAVA_HOME_GUESS} (storepass? certs missing?) — mods will fail TLS without it"
  fi
else
  warn "no cacerts found under ${JAVA_HOME_GUESS} — run 'bash tls/trust_jvm.sh <java-home>' manually after starting the bridge"
fi

# --- 6. Bot -----------------------------------------------------------------
echo
echo "[6/7] Bot (mineflayer)"
if command -v npm >/dev/null 2>&1; then
  mkdir -p "$MC_TEST_DIR/bot"
  if [[ ! -d "$MC_TEST_DIR/bot/node_modules/mineflayer" ]]; then
    ( cd "$MC_TEST_DIR/bot" && timeout 240 npm install mineflayer >/dev/null 2>&1 ) \
      && echo "  mineflayer installed" \
      || warn "npm install mineflayer failed (registry blocked?) — orders will not run until it does"
  else
    echo "  mineflayer already present"
  fi
  # the template is the single source of truth for the bot
  cp "$SCRIPT_DIR/bot_template.js" "$MC_TEST_DIR/bot/bot.js"
  echo "  bot.js copied from scripts/bot_template.js"
else
  warn "npm missing — bot not installed"
fi

# --- 7. Summary ---------------------------------------------------------------
echo
echo "[7/7] Done"
echo "  rig directory : $MC_TEST_DIR"
echo "  mods          : $(ls "$MC_TEST_DIR"/mods/*.jar 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
echo "  neoforge      : $NEOFORGE_VERSION"
echo "  bot           : $([[ -f "$MC_TEST_DIR/bot/bot.js" ]] && echo ready || echo MISSING)"
echo
if [[ $ATTENTION -eq 0 ]]; then
  echo "build complete — 0 items need attention"
else
  echo "build complete — ${ATTENTION} item(s) need attention (see warnings above)"
fi
echo
echo "next: bash scripts/start_mc.sh            (boot the server, FIFO console)"
echo "      bash scripts/test_bridge.sh          (one-command end-to-end test)"
echo "      bash scripts/run_order.sh <user> \"Kilo, get me 3 oak logs\""
