# p2k-bridge

p2k-bridge is a local proxy that lets a modded Minecraft server (NeoForge
1.21.1, stock PlayerEngine + Player2 mods) use the Kilo Code AI Gateway free
tier (model `nvidia/nemotron-3-super-120b-a12b:free`) instead of the Player2
platform. It terminates TLS for `api.player2.game` with a locally generated
CA and answers the mods' login and API calls so nothing outside the machine is
involved.

**Data flow:** Minecraft mods -> local bridge (ports 4315/443/9800) -> Kilo
Gateway (nemotron-3-super free) -> back; Player2 platform and desktop app no
longer involved.

## Running the bridge

### Prerequisites (one time)

```bash
python3 --version                 # 3.10+ required; no pip packages
bash tls/gen_certs.sh             # local CA + api.player2.game cert (idempotent)
bash bootstrap.sh                 # restores .env / certs / system trust after a wipe
```

Then, once per machine:

```bash
echo "127.0.0.1 api.player2.game" | sudo tee -a /etc/hosts   # send the game domain here
bash tls/trust_jvm.sh <java-home>   # trust the CA in the server JVM AND every client JVM
```

`bootstrap.sh` covers the first two lines automatically; the `/etc/hosts` entry
and the per-JVM trust still need doing by hand — trusting the CA in only one JVM
is the single most common cause of handshake failures.

### Start it

```bash
python3 bridge/run.py --config bridge/config.json
```

`sudo` (or a `tls_port` under 1024 other than 443) is only needed to bind 443. If
you cannot bind 443, set `"tls_port": 8443` in `bridge/config.json` and redirect:

```bash
sudo iptables -t nat -A OUTPUT -p tcp -d 127.0.0.1 --dport 443 \
  -j REDIRECT --to-port 8443
```

A successful start prints this and then blocks until you stop it:

```
p2k-bridge 1.0.0
  plain HTTP : http://127.0.0.1:9800 [active]
  login shim : http://127.0.0.1:4315 [active]
  TLS        : https://0.0.0.0:443 (servername api.player2.game) [active]
  model      : nvidia/nemotron-3-super-120b-a12b:free
  quota      : 180 requests/hour (local token bucket)
  kilo key   : not configured (free tier)
  roster     :
    Blaze: model=nvidia/nemotron-3-ultra-550b-a55b:free temperature=0.9 max_tokens=2048 (overrides)
```

All three listeners must show `active`. If startup dies with a
`UnicodeEncodeError` the machine's hostname is longer than 63 characters (Docker's
default) — that is handled, not a failure; a port-in-use error means a previous
instance is still running.

If startup logs `model catalog unavailable ... self-signed certificate in chain`,
the machine is behind a TLS-intercepting proxy whose CA is not trusted. The bridge
still serves chat in that state, but it cannot read per-model context windows or
validate a character's model. Fix it with:

```bash
sudo cp /etc/cloudflare/certs/*.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
python3 bridge/run.py --check
```

### Verify it

```bash
curl -s http://127.0.0.1:9800/bridge/healthz    # {"ok":true,...}
curl -s http://127.0.0.1:9800/bridge/stats      # counters, token budget, recent calls
python3 bridge/doctor.py                        # 13-check pre-flight (exit 0 = all good)
python3 bridge/run.py --check                   # catalog + roster models (exit 0/1)
```

Run a whole end-to-end order through the mods and the bot with
`bash scripts/test_bridge.sh` (see **docs/SETUP.md**).

### Logs

The bridge logs one line per chat call, which is what the E2E scripts read as
evidence — so start it with a file if you want them to work:

```bash
python3 bridge/run.py --config bridge/config.json > bridge.log 2>&1
```

```bash
python3 bridge/monitor.py --once      # one snapshot
python3 bridge/monitor.py             # live dashboard
```

### Stop it

`Ctrl-C`, or `kill $(pgrep -f 'bridge/run\.py')`. If a restart reports the port in
use, the old process is still up; match on the **python** process, not the wrapper
shell, or you will signal the wrong pid:

```bash
for p in $(pgrep -f 'bridge/run\.py'); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "python3" ] && kill "$p"
done
```

The roster in `bridge/characters.json` hot-reloads on change — no restart needed,
though you must reopen the in-game H menu to spawn a newly added companion. Start
from **bridge/characters.example.json** for the field reference. Removing a
character from the roster does **not** remove the NPC: dismiss it in game, see
**docs/CHARACTERS.md**.

## What the mods still need

Nothing. Stock jars, no mod edits, no Player2 app. Three numbers govern each chat
call:

- **`max_tokens` = 500,000** requested per call (the user's "free reign"), then
  clamped per request. The limit comes from the **gateway catalog for the model
  actually used** (`p2k_bridge.catalog`), not just from the global
  `model_context_limit`: the floor is `context − input − 1024` (min 256), and a
  model's per-response completion cap is honoured too, because some models publish
  a large context but a much smaller completion window. 500k intentionally exceeds
  the context window — the clamp makes it safe. A `model_context_limit` of `0`
  disables clamping.
- **Token budget per window: DISABLED** (`token_budget_per_window: 0`). Token
  usage is still **tracked, not capped** — `/bridge/stats` reports the
  `token_budget` object. Set it to a positive number (e.g. `5000000`) to
  re-enable enforcement.

The only hard limiter is **180 requests/hour** (Kilo's free models top out at
~200 req/hr/IP). When it is exhausted the NPC silently idles (all-empty
envelope, HTTP 200) until the bucket refills (one request every 20 s).

Watch it live:

```bash
curl -s http://127.0.0.1:9800/bridge/stats   # includes the token_budget object
```

`token_budget` reports `tokens_per_window`, `window_used`, `remaining`,
`lifetime_prompt`, `lifetime_completion`, `lifetime_total` and
`lifetime_denials` — see **docs/SETUP.md** for the full breakdown.

## Repo layout

```
.
├── bridge/
│   ├── run.py            # entry point (--check for a catalog pre-flight)
│   ├── doctor.py         # 13-check pre-flight diagnostic
│   ├── monitor.py        # live dashboard
│   ├── characters.py     # roster CLI (add/remove/list/show)
│   ├── config.json       # global settings (model, quota, token budget)
│   ├── characters.json   # the live NPC roster (hot-reloads)
│   ├── characters.example.json   # field reference + worked examples
│   └── p2k_bridge/       # routing, catalog, normalize, budget, rate limit
├── scripts/              # test rig + mineflayer bot + E2E harness
├── tls/                  # cert generation + JVM trust (gen_certs.sh, trust_jvm.sh)
├── bootstrap.sh          # restores runtime artifacts after a wipe
└── docs/
    ├── SETUP.md          # full runbook (incl. per-client Java trust, systemd)
    └── CHARACTERS.md     # roster reference and per-character overrides
```

Known limits and quirks (the 180 req/hr bucket, the burst capacity, the
hot-reload rules) are collected in **AGENTS.md**.

## Status

- Bridge endpoints implemented (chat completions, login shim, character list,
  STT/TTS stubs, Joules balance).
- Global model `nvidia/nemotron-3-super-120b-a12b:free`; **per-character model,
  temperature and max_tokens overrides** in `bridge/characters.json` (see
  docs/CHARACTERS.md). Context limits come from the gateway catalog.
- Every chat call is classified (`conversation`, `command_feedback`, `greeting`,
  `event`, `npc_dialogue`, `memory_extraction`, `unknown`) and attributed to a
  character; both are recorded in `/bridge/stats`. Classification is currently
  observability only — it changes no routing behaviour.
- TTS/STT stubbed (no audio in this version).
- `max_tokens` = 500,000 per call, clamped per request to the target model's
  context window and completion cap (the token budget per window is disabled —
  tracking only), with a 180 req/hr cap as the only hard limiter; both are
  visible in `/bridge/stats`.
- Local-only deployment; nothing here is exposed to the internet.
- Pre-flight checks: `python3 bridge/tests/selftest.py` (offline HTTP layer +
  TLS handshake, `--live` for one real gateway call) and
  `python3 bridge/run.py --check` (catalog + roster models, exit 0/1).
- Operator tooling: `python3 bridge/monitor.py` live dashboard (`--once` for a
  single snapshot), `python3 bridge/doctor.py` (13-check diagnostic) and
  `python3 bridge/characters.py` for the NPC roster (see docs/CHARACTERS.md).

## License

MIT for the bridge scripts.
