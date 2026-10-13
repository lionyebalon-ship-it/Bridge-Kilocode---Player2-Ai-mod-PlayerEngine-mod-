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

## What the mods still need

Nothing. Stock jars, no mod edits, no Player2 app. Two numbers govern each chat
call:

- **`max_tokens` = 500,000** requested per call (the user's "free reign"). Since
  the pinned model has a 262,144-token context window, the bridge clamps it per
  request to `context − input − 1024` (floor 256), so the model gets the largest
  possible output without gateway errors. 500k intentionally exceeds the model's
  context window — the clamp makes it safe. Change `max_tokens` /
  `model_context_limit` in `bridge/config.json` (a `model_context_limit` of `0`
  disables clamping).
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
├── bridge/          # bridge implementation (endpoints, config, run.py)
├── tls/             # cert generation + JVM trust helpers (gen_certs.sh, trust_jvm.sh)
└── docs/
    └── SETUP.md     # full runbook (incl. per-client Java trust)
```

## Quickstart

```bash
bash tls/gen_certs.sh                      # 1. generate local CA + certs
cp .env.example .env                       # 2. configure KILO_API_KEY (optional — free tier works without it)
sudo python3 bridge/run.py --config bridge/config.json   # 3. start bridge (sudo needed for 443)
echo "127.0.0.1 api.player2.game" | sudo tee -a /etc/hosts   # 4. point the game domain at the bridge
bash tls/trust_jvm.sh <java-home>          # 5. trust the CA in server + every client JVM
```

Done — join the server and the NPC replies without any web login.

Monitor it live with `python3 bridge/monitor.py` and manage NPC companions with
`python3 bridge/characters.py` (see docs/CHARACTERS.md).

See **docs/SETUP.md** for the full runbook, including how to trust the CA in
each client's bundled Java runtime, the systemd unit, health checks, and
troubleshooting.

## Status

- Bridge endpoints implemented (chat completions, login shim, character list,
  STT/TTS stubs).
- Model fixed to `nvidia/nemotron-3-super-120b-a12b:free`.
- TTS/STT stubbed (no audio in this version).
- `max_tokens` = 500,000 per call, clamped per request to the model's
  262,144-token context window (the token budget per window is disabled —
  tracking only), with a 180 req/hr cap as the only hard limiter; both are
  visible in `/bridge/stats`.
- Local-only deployment; nothing here is exposed to the internet.
- Pre-flight checks: `python3 bridge/tests/selftest.py` (offline HTTP layer +
  TLS handshake, `--live` for one real gateway call) and
  `python3 bridge/run.py --check` (catalog preflight).
- Operator tooling: `python3 bridge/monitor.py` live dashboard (`--once` for a
  single snapshot) and `python3 bridge/characters.py` for the NPC roster
  (see docs/CHARACTERS.md).

## License

MIT for the bridge scripts.
