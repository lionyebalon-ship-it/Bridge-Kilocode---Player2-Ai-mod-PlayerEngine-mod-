#!/usr/bin/env bash
# bootstrap.sh — restore p2k-bridge's gitignored runtime artifacts.
#
# This sandbox periodically wipes untracked/gitignored files. The two the
# bridge needs at runtime — tls/certs/ and .env — are gitignored, so an
# environment reset deletes them. This script regenerates them (idempotently)
# so the bridge comes back up cleanly. Safe to re-run any number of times.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PLACEHOLDER_LINE='^KILO_API_KEY=paste-your-kilocode-api-key-here'
JAVA_HOME_GUESS="${P2K_JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
ATTENTION=0

note() { # print an actionable warning and count it as needing attention
  echo "  ! $1" >&2
  ATTENTION=$((ATTENTION + 1))
}

echo "p2k-bridge bootstrap — restoring gitignored runtime artifacts"
echo "repo: $SCRIPT_DIR"
echo

# --- 1. TLS certificates -------------------------------------------------
echo "[1/6] TLS certificates (tls/gen_certs.sh)"
# gen_certs.sh is itself idempotent: it keeps valid certs (>30 days left) and
# only regenerates when they are missing or expiring.
if bash tls/gen_certs.sh >/dev/null 2>&1; then
  echo "  certs present/valid in tls/certs/ (regenerated if missing/expiring)"
else
  note "gen_certs.sh failed — inspect manually: bash tls/gen_certs.sh"
fi

# --- 2. Import the CA into the system trust store (best effort) ----------
echo
echo "[2/6] Trust the local CA in the system trust store (debug convenience)"
SYSTEM_CA_DIR=/usr/local/share/ca-certificates
if [[ ! -f tls/certs/p2k_ca.crt ]]; then
  echo "  skipped — tls/certs/p2k_ca.crt is missing (see [1/6])"
elif [[ "$(id -u)" -ne 0 ]]; then
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

# --- 3. .env (Kilo API key) ---------------------------------------------
echo
echo "[3/6] Kilo API key (.env)"
if [[ -f .env ]]; then
  lines="$(wc -l < .env | tr -d ' ')"
  if grep -Eq "$PLACEHOLDER_LINE" .env || grep -Eq '^KILO_API_KEY=[[:space:]]*$' .env; then
    echo "  .env exists ($lines line(s)) but the key is still the placeholder"
    note ".env has the PLACEHOLDER KILO_API_KEY — paste your Kilo API key into .env"
  else
    echo "  .env exists ($lines line(s)); KILO_API_KEY looks set (value not shown)"
    chmod 600 .env 2>/dev/null || true
  fi
else
  echo "  .env missing — creating it from .env.example"
  cp .env.example .env
  chmod 600 .env
  note ".env created from .env.example with the PLACEHOLDER key — paste your KILO_API_KEY into .env"
fi

# --- 4. Trust the CA in the system JVM (only if keytool is present) ------
echo
echo "[4/6] Trust the local CA in the system JVM"
KEYTOOL="$(command -v keytool || true)"
if [[ -z "$KEYTOOL" ]]; then
  note "keytool not found — install a JDK (e.g. 'apt-get install -y openjdk-21-jdk-headless'), then run: bash tls/trust_jvm.sh $JAVA_HOME_GUESS"
elif [[ ! -d "$JAVA_HOME_GUESS" ]]; then
  note "system JVM not found at $JAVA_HOME_GUESS — trust the CA once a JDK is there: bash tls/trust_jvm.sh <java-home>"
else
  if bash tls/trust_jvm.sh "$JAVA_HOME_GUESS" >/dev/null 2>&1; then
    echo "  TRUST OK: $JAVA_HOME_GUESS/lib/security/cacerts (alias p2k-bridge-ca)"
  else
    note "trust_jvm.sh failed — re-run: bash tls/trust_jvm.sh $JAVA_HOME_GUESS"
  fi
fi

# --- 5. /etc/hosts entry for api.player2.game ---------------------------
echo
echo "[5/6] /etc/hosts entry for api.player2.game"
if grep -q 'api.player2.game' /etc/hosts 2>/dev/null; then
  echo "  present"
else
  echo "  missing — add this EXACT line to /etc/hosts (needs root; /etc/hosts may be read-only):"
  echo "      127.0.0.1 api.player2.game"
  note "/etc/hosts lacks 'api.player2.game' — add: 127.0.0.1 api.player2.game"
fi

# --- 6. python3 presence ------------------------------------------------
echo
echo "[6/6] Python 3"
if command -v python3 >/dev/null 2>&1; then
  echo "  $(python3 --version 2>&1) at $(command -v python3)"
else
  note "python3 not found on PATH — install Python 3.10+ and re-run"
fi

# --- summary -------------------------------------------------------------
echo
echo "======================================================"
if [[ "$ATTENTION" -eq 0 ]]; then
  echo "bootstrap complete / 0 items need attention"
else
  echo "bootstrap complete / $ATTENTION item(s) need attention (see the '!' lines above)"
fi
echo "Restart the bridge after this to bring all listeners up:"
echo "  python3 bridge/run.py --config bridge/config.json"
echo "======================================================"

exit 0
