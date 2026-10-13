#!/usr/bin/env bash
# trust_jvm.sh — import the local bridge CA into a JVM's trust store.
#
# Because TLS for api.player2.game is terminated locally, every JVM that makes
# those calls must trust the CA: the dedicated server's JVM AND each Minecraft
# launcher-bundled client JVM. They are usually DIFFERENT JVMs — importing into
# the wrong runtime is the #1 cause of handshake failures.
#
# usage:
#   bash tls/trust_jvm.sh <path-to-java-home>            # resolves <home>/lib/security/cacerts
#   bash tls/trust_jvm.sh <path-to-cacerts>              # a truststore file directly
#   bash tls/trust_jvm.sh <path> [alias]                 # custom alias (default p2k-bridge-ca)
#
# env: P2K_STOREPASS  truststore password (default: changeit)
#
# Idempotent: an existing alias is deleted before import, so re-runs succeed.
# Exits 0 with "TRUST OK: <store>" on success, 1 with a clear message otherwise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CA_CRT="$SCRIPT_DIR/certs/p2k_ca.crt"

ALIAS="${2:-p2k-bridge-ca}"
STOREPASS="${P2K_STOREPASS:-changeit}"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "usage: trust_jvm.sh <path-to-cacerts-or-java-home> [alias]" >&2
  echo "  e.g. bash tls/trust_jvm.sh /usr/lib/jvm/java-21-openjdk-amd64" >&2
  exit 1
fi
TARGET="$1"

if [[ ! -f "$CA_CRT" ]]; then
  echo "ERROR: CA not found at $CA_CRT — run 'bash tls/gen_certs.sh' first" >&2
  exit 1
fi

# --- resolve the target to a cacerts file ------------------------------------
TRUSTSTORE=""
if [[ -d "$TARGET" ]]; then
  if [[ -x "$TARGET/bin/java" ]]; then
    # java home layout
    TRUSTSTORE="$TARGET/lib/security/cacerts"
  elif [[ -f "$TARGET/lib/security/cacerts" ]]; then
    TRUSTSTORE="$TARGET/lib/security/cacerts"
  fi
elif [[ -f "$TARGET" ]]; then
  TRUSTSTORE="$TARGET"   # a truststore file given directly
fi

if [[ -z "$TRUSTSTORE" || ! -f "$TRUSTSTORE" ]]; then
  echo "ERROR: could not resolve a truststore from '$TARGET'" >&2
  echo "  give a java home (contains bin/java) or a path to a cacerts file" >&2
  exit 1
fi

if ! command -v keytool >/dev/null 2>&1; then
  echo "ERROR: keytool not found — install any JDK 17+ (the keytool works for" >&2
  echo "  any JVM's truststore file, it does not need to be the same JVM)" >&2
  exit 1
fi

echo "truststore: $TRUSTSTORE"
echo "importing : $CA_CRT (alias $ALIAS)"

# --- import (idempotent: delete existing alias first) -------------------------
if keytool -list -keystore "$TRUSTSTORE" -storepass "$STOREPASS" -alias "$ALIAS" >/dev/null 2>&1; then
  echo "alias '$ALIAS' already exists — deleting it first"
  keytool -delete -alias "$ALIAS" -keystore "$TRUSTSTORE" -storepass "$STOREPASS" >/dev/null 2>&1
fi

if ! keytool -importcert -noprompt -alias "$ALIAS" -file "$CA_CRT" \
     -keystore "$TRUSTSTORE" -storepass "$STOREPASS" >/dev/null 2>&1; then
  echo "ERROR: import failed — wrong storepass? (set P2K_STOREPASS; default is 'changeit')" >&2
  exit 1
fi

# --- verify --------------------------------------------------------------------
if keytool -list -keystore "$TRUSTSTORE" -storepass "$STOREPASS" -alias "$ALIAS" >/dev/null 2>&1; then
  echo "TRUST OK: $TRUSTSTORE"
else
  echo "ERROR: alias not present after import" >&2
  exit 1
fi

cat <<'EOF'

Which JVMs need this (run once per JVM):
  * dedicated server:  the java that runs the server, e.g.
      readlink -f "$(which java)"   # drop the trailing bin/java for the home
  * Minecraft client:  the launcher's BUNDLED runtime (usually a different JVM):
      Linux:   ~/.minecraft/runtimes/java-runtime-*/
      macOS:   ~/Library/Application Support/minecraft/runtimes/java-runtime-*/
      Windows: %APPDATA%\.minecraft\runtimes\java-runtime-*
               %PROGRAMFILES%\Minecraft Launcher\runtime\java-*-jre\
      Prism/MultiMC: the instance's own bundled runtime
  Undo with: keytool -delete -alias p2k-bridge-ca -keystore <cacerts> -storepass changeit
EOF
