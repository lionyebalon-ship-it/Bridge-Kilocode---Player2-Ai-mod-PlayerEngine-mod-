# TLS interception runbook (`tls/`)

The PlayerEngine/Player2NPC mods hardcode `https://api.player2.game`. p2k-bridge
intercepts that hostname locally: `/etc/hosts` points it at `127.0.0.1`, the
bridge terminates TLS for it with a locally generated CA, and every JVM that
touches the domain trusts that CA. **No mod is edited, patched, or recompiled.**

## 1. Generate the CA + server cert

```bash
bash tls/gen_certs.sh
```

Produces (under `tls/certs/`, gitignored):

| File | Purpose |
|---|---|
| `ca.key` / `ca.crt` | local CA (RSA-4096, 10y) |
| `server.key` / `server.crt` | leaf for the game domain (RSA-2048, 825d) |
| `server.pem` | fullchain (leaf + CA) |
| `p2k_ca.crt` | the CA to import — this is the file you distribute to trust stores |
| `san.cnf`, `server.csr` | signing material (harmless leftovers) |

The leaf SANs are `api.player2.game`, `*.player2.game`, `player2.game`,
`127.0.0.1`. The script is idempotent (skips when certs are valid >30 days) and
self-verifies (`openssl verify` + SAN assertion).

## 2. Point the hostname at the bridge

```bash
# Linux / macOS
echo "127.0.0.1 api.player2.game" | sudo tee -a /etc/hosts
# Windows (PowerShell as Administrator)
Add-Content -Path $env:windir\System32\drivers\etc\hosts -Value "127.0.0.1 api.player2.game"
```

Back up `/etc/hosts` first. If it is mounted read-only (containers):
`sudo mount -o remount,rw /etc/hosts`. Flush DNS afterwards:

```bash
sudo resolvectl flush-caches      # Linux (systemd-resolved)
sudo dscacheutil -flushcache      # macOS
ipconfig /flushdns                # Windows
```

## 3. Trust the CA in every JVM

```bash
bash tls/trust_jvm.sh /usr/lib/jvm/java-21-openjdk-amd64
```

Run it once per JVM: the dedicated server's JVM **and** each Minecraft launcher's
bundled client runtime (they are different JVMs — this is the #1 cause of
`SSLHandshakeException`). See the end of `trust_jvm.sh` output for where the
launcher runtimes live per OS. `P2K_STOREPASS` overrides the storepass (default
`changeit`). Verify with:

```bash
keytool -list -cacerts -storepass changeit -alias p2k-bridge-ca
```

Revert with:

```bash
keytool -delete -alias p2k-bridge-ca -keystore <cacerts> -storepass changeit
```

Optional, for plain `curl` debugging: import the CA into the **system** store too
(`sudo cp tls/certs/p2k_ca.crt /usr/local/share/ca-certificates/ && sudo
update-ca-certificates`, or run `bash bootstrap.sh`). The game itself only needs
the JVM trust.

## 4. Verify end-to-end

```bash
openssl s_client -connect 127.0.0.1:443 -servername api.player2.game </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName      # expect our cert + SANs
curl -s https://127.0.0.1/v1/health                       # or curl --cacert tls/certs/p2k_ca.crt
```

In game: the companion authenticates without any click — no web login, no device
code link (the log shows the local shim issuing a `p2Key` or the device flow
completing instantly).

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SSLHandshakeException` in the server log | CA not in **that** JVM's `cacerts`. Re-run `trust_jvm.sh` against the exact runtime — server JVM and launcher JVM differ; vanilla and Prism/MultiMC runtimes differ. Restart the JVM after import. |
| Connection refused on 443 | Bridge not running (start it: `sudo python3 bridge/run.py`) or port 443 blocked — see the iptables alternative below. |
| `curl` fails issuer verification | CA not in the system store — `bash bootstrap.sh` (root) or use `--cacert tls/certs/p2k_ca.crt`. |
| Mod hangs at login / "click link" prompt | `/etc/hosts` not updated or DNS cached — check the entry, flush caches, restart both JVMs. The login shim on `127.0.0.1:4315` must be reachable. |
| `keytool: command not found` | Install any JDK 17+ — its keytool can edit any JVM's truststore file. |
| Cert expired | `bash tls/gen_certs.sh` (regenerates), then re-run `trust_jvm.sh` per JVM. |

## 6. Running unprivileged (iptables alternative)

Port 443 normally needs root. To run the bridge unprivileged, set
`"tls_port": 8443` in `bridge/config.json` and add a one-time redirect (needs
root once, not per-run):

```bash
sudo iptables -t nat -A OUTPUT -p tcp -d 127.0.0.1 --dport 443 -j REDIRECT --to-port 8443
```

Persist with `iptables-save > /etc/iptables/rules.v4` (Debian/Ubuntu) or a
systemd unit that re-applies it on boot.

## 7. Security note

This CA is a **local, single-machine trust anchor**. Never install it on
machines you don't control, and never share `ca.key`/`server.key` — anyone
holding `ca.key` can impersonate any site to a JVM that trusts it. The scope
here is one machine running both the server and the bridge.
