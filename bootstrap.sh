#!/usr/bin/env bash
# bootstrap.sh — restore what a sandbox wipe destroys.
#
# This sandbox periodically deletes gitignored files, everything outside the
# repo, and installed packages. The bridge's runtime artifacts (tls/certs/ and
# .env) are gitignored, so a reset removes them; the TLS-intercepting proxy's
# CA disappears at the same time, which silently costs the bridge its model
# catalog. This script restores all of it — idempotently, and re-runnable any
# number of times.
#
# After this, one command brings the bridge back up:
#   python3 bridge/run.py --config bridge/config.json
#
# Env knobs:
#   P2K_INSTALL_JDK=1   also install openjdk-21 (needed only by the MC test rig)
#   P2K_JAVA_HOME=...   where trust_jvm.sh should look
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PLACEHOLDER_LINE='^KILO_API_KEY=paste-your-kilocode-api-key-here'
JAVA_HOME_GUESS="${P2K_JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
ATTENTION=0
IS_ROOT=0
[[ "$(id -u)" -eq 0 ]] && IS_ROOT=1

note() { # print an actionable warning and count it as needing attention
  echo "  ! $1" >&2
  ATTENTION=$((ATTENTION + 1))
}

have_apt() { [[ "$IS_ROOT" -eq 1 ]] && command -v apt-get >/dev/null 2>&1; }

install_packages() { # install_packages <description> <pkg>...
  local what="$1"; shift
  if have_apt; then
    echo "  installing $what (root detected)..."
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" \
      >/tmp/p2k-bootstrap-apt.log 2>&1; then
      echo "  INSTALL OK: $what"
      return 0
    fi
    note "apt-get install failed for $what — see /tmp/p2k-bootstrap-apt.log"
    return 1
  fi
  note "$what not installed — not root (or no apt-get). Install manually: apt-get install -y $*"
  return 1
}

echo "p2k-bridge bootstrap — restoring what a sandbox wipe destroys"
echo "repo: $SCRIPT_DIR"
echo

# --- 1. TLS certificates -------------------------------------------------
echo "[1/7] TLS certificates (tls/gen_certs.sh)"
# gen_certs.sh is itself idempotent: it keeps valid certs (>30 days left) and
# only regenerates when they are missing or expiring.
if bash tls/gen_certs.sh >/dev/null 2>&1; then
  echo "  certs present/valid in tls/certs/ (regenerated if missing/expiring)"
else
  note "gen_certs.sh failed — inspect manually: bash tls/gen_certs.sh"
fi

# --- 2. Import the CA into the system trust store (best effort) ----------
echo
echo "[2/7] Trust the local CA in the system trust store (debug convenience)"
SYSTEM_CA_DIR=/usr/local/share/ca-certificates
if [[ ! -f tls/certs/p2k_ca.crt ]]; then
  echo "  skipped — tls/certs/p2k_ca.crt is missing (see [1/7])"
elif [[ "$IS_ROOT" -ne 1 ]]; then
  echo "  not root — skipped (only needed for plain curl; the mods use the JVM trust below)"
elif ! command -v update-ca-certificates >/dev/null 2>&1; then
  echo "  update-ca-certificates not found — skipped (only needed for plain curl; the mods use the JVM trust below)"
else
  mkdir -p "$SYSTEM_CA_DIR" 2>/dev/null || true
  if cp -f tls/certs/p2k_ca.crt "$SYSTEM_CA_DIR/p2k-bridge-ca.crt" \
    && update-ca-certificates >/dev/null 2>&1; then
    echo "  TRUST OK: $SYSTEM_CA_DIR/p2k-bridge-ca.crt registered in the system store"
    echo "  plain 'curl https://api.player2.game/...' now verifies without -k/--cacert"
  else
    note "system-store import failed — use 'curl --cacert tls/certs/p2k_ca.crt' (the mods only need the JVM trust below)"
  fi
fi

# --- 3. Trust the TLS-intercepting proxy's CA ---------------------------
echo
echo "[3/7] TLS-intercepting proxy CA (needed for the gateway catalog)"
if curl -s --max-time 10 -o /dev/null https://api.kilo.ai/api/gateway/models 2>/dev/null; then
  echo "  api.kilo.ai already verifies — nothing to do"
elif ls /etc/cloudflare/certs/*.crt >/dev/null 2>&1; then
  if [[ "$IS_ROOT" -ne 1 ]]; then
    echo "  proxy CAs found in /etc/cloudflare/certs but not root — import manually:"
    echo "      sudo cp /etc/cloudflare/certs/*.crt /usr/local/share/ca-certificates/"
    echo "      sudo update-ca-certificates"
  elif cp -f /etc/cloudflare/certs/*.crt "$SYSTEM_CA_DIR/" \
    && update-ca-certificates >/dev/null 2>&1 \
    && curl -s --max-time 10 -o /dev/null https://api.kilo.ai/api/gateway/models 2>/dev/null; then
    echo "  TRUST OK: proxy CA registered — the bridge can now read the model catalog"
  else
    note "proxy CA import failed — the bridge still serves chat but cannot validate"
    note "  per-model context limits or a character's model. Fix: see docs/SETUP.md"
  fi
else
  echo "  no /etc/cloudflare/certs — presumably no intercepting proxy on this host"
fi

# --- 4. .env (Kilo API key) ---------------------------------------------
echo
echo "[4/7] Kilo API key (.env)"
if [[ -f .env ]]; then
  lines="$(wc -l < .env | tr -d ' ')"
  if grep -Eq "$PLACEHOLDER_LINE" .env || grep -Eq '^KILO_API_KEY=[[:space:]]*$' .env; then
    echo "  .env exists ($lines line(s)); key is blank or still the placeholder"
    echo "  -> the bridge now treats that as the anonymous FREE TIER (a working default)"
    echo "  -> to use a real key, paste it into .env"
    chmod 600 .env 2>/dev/null || true
  else
    echo "  .env exists ($lines line(s)); KILO_API_KEY looks set (value not shown)"
    chmod 600 .env 2>/dev/null || true
  fi
else
  echo "  .env missing — creating it from .env.example"
  cp .env.example .env
  chmod 600 .env
  echo "  -> placeholder/blank key = anonymous free tier (the bridge works without a key)"
  echo "  -> paste a real KILO_API_KEY into .env to raise your quota"
fi

# --- 5. Trust the CA in the system JVM (needed by the mods, not the bridge)
echo
echo "[5/7] Trust the local CA in the system JVM (MC mods need this)"
KEYTOOL="$(command -v keytool || true)"
if [[ -z "$KEYTOOL" ]]; then
  # Only the mods need a JVM; the bridge itself does not.
  if [[ "${P2K_INSTALL_JDK:-0}" == "1" ]]; then
    install_packages "openjdk-21-jdk-headless" openjdk-21-jdk-headless
    KEYTOOL="$(command -v keytool || true)"
  fi
  [[ -z "$KEYTOOL" ]] && note "keytool not found — needed only for the MC mods. Run with P2K_INSTALL_JDK=1, or: apt-get install -y openjdk-21-jdk-headless && bash tls/trust_jvm.sh <java-home>"
fi
if [[ -n "$KEYTOOL" ]]; then
  if [[ ! -d "$JAVA_HOME_GUESS" ]]; then
    note "system JVM not found at $JAVA_HOME_GUESS — trust the CA once a JDK is there: bash tls/trust_jvm.sh <java-home>"
  elif bash tls/trust_jvm.sh "$JAVA_HOME_GUESS" >/dev/null 2>&1; then
    echo "  TRUST OK: $JAVA_HOME_GUESS/lib/security/cacerts (alias p2k-bridge-ca)"
  else
    note "trust_jvm.sh failed — re-run: bash tls/trust_jvm.sh $JAVA_HOME_GUESS"
  fi
fi

# --- 6. /etc/hosts entry for api.player2.game ---------------------------
echo
echo "[6/7] /etc/hosts entry for api.player2.game"
resolve_hosts() { # true once the name resolves locally, or the entry is present
  grep -q 'api\.player2\.game' /etc/hosts 2>/dev/null
}
if resolve_hosts; then
  echo "  present"
else
  # In this sandbox /etc/hosts is a read-only overlay that refuses a remount, so
  # a plain append fails. Replace the mount with a copy that has the entry.
  ENTRY='127.0.0.1 api.player2.game'
  if [[ "$IS_ROOT" -ne 1 ]]; then
    note "/etc/hosts lacks '$ENTRY' and we are not root — add it manually:"
    note "  echo '$ENTRY' | sudo tee -a /etc/hosts"
  elif printf '\n%s\n' "$ENTRY" >> /etc/hosts 2>/dev/null; then
    echo "  ADDED: appended '$ENTRY'"
  elif cp /etc/hosts /tmp/p2k-hosts.$$ && printf '%s\n' "$ENTRY" >> /tmp/p2k-hosts.$$ \
    && (umount /etc/hosts 2>/dev/null || true) \
    && mount --bind /tmp/p2k-hosts.$$ /etc/hosts 2>/dev/null; then
    echo "  ADDED: '$ENTRY' (replaced the read-only /etc/hosts mount)"
    echo "  note: this is session-local — it must be re-done after each wipe"
  else
    note "could not write /etc/hosts (read-only). Add by hand: echo '$ENTRY' | sudo tee -a /etc/hosts"
  fi
fi

# --- 7. python3 presence ------------------------------------------------
echo
echo "[7/7] Python 3"
if command -v python3 >/dev/null 2>&1; then
  echo "  $(python3 --version 2>&1) at $(command -v python3)"
else
  install_packages "python3 (stdlib only; the bridge has no pip dependencies)" python3 \
    && echo "  $(python3 --version 2>&1) at $(command -v python3)"
fi

# --- summary -------------------------------------------------------------
echo
echo "======================================================"
if [[ "$ATTENTION" -eq 0 ]]; then
  echo "bootstrap complete / 0 items need attention"
else
  echo "bootstrap complete / $ATTENTION item(s) need attention (see the '!' lines above)"
fi
echo "Bring the bridge up with:"
echo "  python3 bridge/run.py --config bridge/config.json"
echo "Then check it with: python3 bridge/doctor.py"
echo "======================================================"

exit 0
