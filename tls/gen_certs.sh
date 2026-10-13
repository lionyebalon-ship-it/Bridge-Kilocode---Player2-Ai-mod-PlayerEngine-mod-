#!/usr/bin/env bash
# gen_certs.sh — local CA + server cert for intercepting api.player2.game.
#
# The mods hardcode https://api.player2.game. We point that hostname at
# 127.0.0.1 (via /etc/hosts) and terminate TLS locally, so the JVM needs to
# trust our CA (see trust_jvm.sh). This script is idempotent: it skips
# generation when valid certificates already exist.
#
# Outputs (all under tls/certs/, resolved relative to THIS script):
#   ca.key / ca.crt        self-signed local CA (RSA-4096, 10y)
#   server.key / server.crt leaf for api.player2.game (RSA-2048, 825d)
#   server.csr             the signing request
#   server.pem             fullchain (server.crt + ca.crt)
#   p2k_ca.crt             copy of ca.crt (the file to import / distribute)
#
# usage: bash tls/gen_certs.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="$SCRIPT_DIR/certs"

CA_KEY="$CERTS_DIR/ca.key"
CA_CRT="$CERTS_DIR/ca.crt"
SRV_KEY="$CERTS_DIR/server.key"
SRV_CRT="$CERTS_DIR/server.crt"
SRV_PEM="$CERTS_DIR/server.pem"
CA_COPY="$CERTS_DIR/p2k_ca.crt"

mkdir -p "$CERTS_DIR"

# --- idempotency: skip when existing certs are valid for >30 more days -------
if [[ -f "$CA_CRT" && -f "$SRV_CRT" ]]; then
  if openssl x509 -in "$SRV_CRT" -noout -checkend $((30 * 24 * 3600)) >/dev/null 2>&1; then
    echo "certs already present and valid >30 days — nothing to do"
    echo "  $CERTS_DIR"
    exit 0
  fi
  echo "existing certs expire within 30 days — regenerating"
fi

echo "generating local CA (RSA-4096)..."
openssl genrsa -out "$CA_KEY" 4096 2>/dev/null
openssl req -x509 -new -key "$CA_KEY" -sha256 -days 3650 \
  -subj "/O=p2k-bridge/CN=p2k-bridge Local CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out "$CA_CRT" 2>/dev/null

echo "generating server certificate for api.player2.game (RSA-2048)..."
openssl genrsa -out "$SRV_KEY" 2048 2>/dev/null
openssl req -new -key "$SRV_KEY" -subj "/O=p2k-bridge/CN=api.player2.game" \
  -out "$CERTS_DIR/server.csr" 2>/dev/null

cat > "$CERTS_DIR/san.cnf" <<'EOF'
subjectAltName = DNS:api.player2.game, DNS:*.player2.game, DNS:player2.game, IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF

openssl x509 -req -in "$CERTS_DIR/server.csr" \
  -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial -days 825 -sha256 \
  -extfile "$CERTS_DIR/san.cnf" \
  -out "$SRV_CRT" 2>/dev/null

# fullchain + distributable CA copy
cat "$SRV_CRT" "$CA_CRT" > "$SRV_PEM"
cp "$CA_CRT" "$CA_COPY"

# never leave private keys world-readable
chmod 600 "$CA_KEY" "$SRV_KEY"
chmod 644 "$CA_CRT" "$SRV_CRT" "$SRV_PEM" "$CA_COPY" "$CERTS_DIR/server.csr" "$CERTS_DIR/san.cnf"

# --- verification -------------------------------------------------------------
echo
echo "verifying..."
openssl verify -CAfile "$CA_CRT" "$SRV_CRT"

if ! openssl x509 -in "$SRV_CRT" -noout -text | grep -q 'DNS:api.player2.game'; then
  echo "ERROR: server cert is missing the api.player2.game SAN" >&2
  exit 1
fi
echo "OK: SAN covers api.player2.game"
echo
echo "next steps:"
echo "  1. hosts:       echo '127.0.0.1 api.player2.game' | sudo tee -a /etc/hosts"
echo "  2. trust (JVM): bash tls/trust_jvm.sh <java-home>"
echo "certs written to: $CERTS_DIR"
