# AGENTS.md — p2k-bridge

Local proxy that lets a modded Minecraft server (NeoForge 1.21.1, stock
PlayerEngine + Player2NPC mods) use the Kilo Code Gateway
(`nvidia/nemotron-3-super-120b-a12b:free`) instead of the Player2 platform.
The mods are **never modified** — interception works via `/etc/hosts` + a
local TLS bridge on 443 + a login shim on 4315. All mod AI traffic funnels
through exactly one seam: `POST /v1/chat/completions`.

## Commands (run from repo root unless noted)

```bash
python3 bridge/run.py --config bridge/config.json   # start bridge (443 needs root)
python3 bridge/run.py --check                       # model-catalog preflight (exit 0/1)
python3 bridge/doctor.py [--json] [--quiet]         # pre-flight: ports/certs/hosts/JVM trust/model/one live call
python3 bridge/monitor.py [--once]                  # live dashboard (req/hr remaining, tokens, recent requests)

cd bridge && python3 -m unittest discover -s tests  # full unit suite (275 tests, unittest — NOT pytest)
cd bridge && python3 -m unittest tests.test_config  # single test module
python3 bridge/tests/selftest.py                    # offline HTTP layer + TLS handshake (run from bridge/)
python3 bridge/tests/selftest.py --live             # + one real Kilo Gateway call (needs .env/KILO_API_KEY)
python3 bridge/characters.py list|add|remove|show    # NPC roster management

bash tls/gen_certs.sh                               # local CA + api.player2.game cert (idempotent)
bash tls/trust_jvm.sh <java-home-or-cacerts>        # import CA into a JVM (alias p2k-bridge-ca)
bash bootstrap.sh                                   # restore everything after an environment wipe

bash scripts/build_test_rig.sh                      # rebuild the wiped MC test rig at /workspace/mc-test
bash scripts/start_mc.sh                            # boot NeoForge server (FIFO console; leaves it running)
bash scripts/mc_cmd.sh <command>                    # send a server console command
bash scripts/run_order.sh <user> "<msg>"            # drive one NPC order via the mineflayer bot
```

## Architecture facts that change how you work

- **One seam, one contract:** `server.py::_handle_chat` is the only place mod
  traffic meets the LLM. It must **never return non-200** on chat — quota
  exhaustion and upstream failures degrade to an idle envelope
  (`{"reason":"","command":"","message":""}` / `{"reason":"kilo unavailable",...}`)
  so the NPC silently idles instead of breaking its turn. Don't "fix" this by
  propagating errors.
- **Model math:** `max_tokens: 500000` is requested but clamped per call to
  `model_context_limit (262144) − input − 1024`, floor 256 (`kilo.py:compute_max_tokens`).
  Exceeding the context window is intentional; the clamp makes it safe.
  `model_context_limit: 0` disables clamping.
- **Token budget is intentionally disabled** (`token_budget_per_window: 0` =
  tracking only). Positive values re-enable reserve→reconcile→refund. Don't
  change the default without being asked.
- **Rate limiter quirk:** `TokenBucket(180)` has *burst capacity*
  `max(1, rate/60) = 3` — "3 remaining this hour" right after startup is
  correct behavior, not a bug. 180 req/hr is the only hard limiter (Kilo caps
  free models at ~200 req/hr/IP).
- **Python 3.10+ stdlib only.** No pip dependencies — do not add requirements.
  Tests use `unittest`, not pytest.
- **Config precedence for the key:** JSON `kilo_api_key` > env var named by
  `kilo_api_key_env` (`KILO_API_KEY`) > `.env` files (config dir → cwd → repo
  root). Never log or print the key.
- `P2K_MODEL` / `P2K_REQUESTS_PER_HOUR` appear in `.env.example` but are
  **inert** — the code never reads them. Real knobs: `bridge/config.json`.
- **Characters:** the mod rejects characters without a non-empty `id`
  (`PersistentDataManager` error aborts spawning). `characters.py add`
  auto-generates the id (slug of `--short`), the server backfills missing ids
  at serve time, and `bridge/characters.json` hot-reloads by mtime.
- TLS listener binds **0.0.0.0:443** (not localhost-only) — note the firewall
  implication on internet-facing hosts.

## Mod endpoints the bridge must serve

`/v1/chat/completions`, `/v1/health`, `/v1/selected_characters`,
`/v1/login/web/{gameKey}`, `/v1/login/device/new` + `/token`,
`/v1/stt/start|stop`, `/v1/tts/speak` (stubs), `/v1/minecraft/schematics/search`
(stub). Plus ops-only `GET /bridge/healthz`, `/bridge/stats` (plain HTTP, no auth).

## Environment gotchas (this sandbox)

- **Untracked files get wiped periodically** — `.env`, `tls/certs/`, git
  history, and the whole MC rig have all been destroyed mid-session multiple
  times. Commit early; recover runtime artifacts with `bash bootstrap.sh`.
- The MC test rig lives at `/workspace/mc-test` **outside the repo** — rebuild
  with `scripts/build_test_rig.sh` (NeoForge 21.1.201, Java 21, mods:
  PlayerEngine/Player2NPC 1.4.0 + Architectury via Modrinth IDs `nUOXthgN`,
  `CB68UWRl`, `lhGA9TYQ`).
- Outbound HTTPS goes through a TLS-intercepting proxy: if `api.kilo.ai` fails
  with "self-signed in chain", add `/etc/cloudflare/certs/*.crt` to the system
  store (`/usr/local/share/ca-certificates/` + `update-ca-certificates`).
- `/etc/hosts` needs `127.0.0.1 api.player2.game` (may be mounted read-only:
  `mount -o remount,rw /etc/hosts` first).
- Port 443 needs root, or set `"tls_port": 8443` + `sudo iptables -t nat -A
  OUTPUT -p tcp -d 127.0.0.1 --dport 443 -j REDIRECT --to-port 8443`.
- **Server JVM and each MC client JVM are different trust stores** — run
  `trust_jvm.sh` per JVM. Importing into the wrong runtime is the #1 cause of
  handshake failures.
- MC console via FIFO: `tail -f fifo | java` does **not** deliver input — java
  must read the FIFO directly with a background writer-holder (see
  `scripts/start_mc.sh`).
- mineflayer bot sends `playerengine:user_message` as **nested varint byte
  arrays** (outer `varint(len)` + inner `varint(utf8len)` + utf8); the message
  must contain the companion name ("Kilo") for the mod's by-name gate. See
  `scripts/bot_template.js`.

## Testing notes

- `selftest.py` skips the TLS check when `tls/certs/` is missing; `--live`
  skips gracefully (exit 0) without a key.
- `doctor.py` check #8 SKIPs when no JDK/keytool exists or the storepass
  isn't `changeit` (set `P2K_STOREPASS`).
- Full battery = unit suite + selftest (`--live`) + doctor + live endpoint
  probes. The mods drive the LLM only through in-game chat; the bot is the
  test client (`scripts/run_order.sh`).
- Unit tests must never read the real `.env` — use fake keys and temp dirs.
