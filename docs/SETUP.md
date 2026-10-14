# p2k-bridge Setup Runbook

This runbook takes a stock NeoForge 1.21.1 dedicated server with the stock **PlayerEngine** + **Player2** mods from a fresh checkout to an in-game working AI companion. The bridge replaces the Player2 platform and desktop app entirely; no account, no web login, no Joules.

Ports used by the bridge:
| Listener | Address | Purpose |
| Main HTTP bridge | 127.0.0.1:9800 | operational endpoints: GET /bridge/healthz, GET /bridge/stats (plain HTTP) |
| Local login shim | 127.0.0.1:4315 | instant p2Key login for the mods |
| TLS interception | 0.0.0.0:443 | terminates TLS for api.player2.game |

## Prerequisites
- Python 3.10+ (python3 --version).
- openssl on PATH (used by tls/gen_certs.sh to mint the local CA and the leaf certificate for api.player2.game).
- A JDK with keytool to trust the local CA (any JDK 17+; the same keytool works for both the server JVM and launcher-bundled client JVMs).
- sudo privileges for binding port 443 and editing /etc/hosts.
- Stock PlayerEngine + Player2 jars in the server's mods/ folder (nothing in the mods is edited, patched, or recompiled).

## Environment resets / recovery
The runtime artifacts are gitignored and can be lost to VM resets or fresh clones. If anything is missing (`tls/certs/`, `.env`, the hosts entry, the CA trust), run:

```bash
bash bootstrap.sh
```

It regenerates TLS certs if missing, creates `.env` from `.env.example` if missing (with a WARNING to paste `KILO_API_KEY` if it is still the placeholder), imports the CA into the system trust store (best-effort, root) and the system JVM (when keytool exists), checks the `/etc/hosts` entry, and checks python3. It ends with `bootstrap complete / N items need attention`. Then restart the bridge. Note: if the machine sits behind a TLS-intercepting proxy, that proxy's CA must also be in the system store for the bridge to reach api.kilo.ai.

If the bridge logs `model catalog unavailable (network error: ... CERTIFICATE_VERIFY_FAILED ... self-signed certificate in chain)` at startup, that is exactly this problem — and it is worth fixing rather than ignoring, because without the catalog the bridge cannot read per-model context windows or validate a character's model, so it falls back to the global limits. Fix it with:

```bash
sudo cp /etc/cloudflare/certs/*.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
python3 bridge/run.py --check      # must print "kilo gateway models: N ids returned"
```

The bridge still starts and still serves chat without the catalog (it degrades to the configured limits, and every per-model lookup falls back to the global value) — it is a warning, not a failure.

## Step 1 — Generate TLS certificates
```bash
bash tls/gen_certs.sh
```
Writes the CA + server certs under `tls/certs/` (gitignored, safe to re-run). See `tls/README.md` for the file layout, the per-OS hosts/trust commands, and the iptables alternative to running as root.

## Step 2 — Configure the Kilo API key
```bash
cp .env.example .env   # then edit and set KILO_API_KEY
```
`.env` is gitignored. The Kilo Gateway free tier works without a key, but the key exists for reliability. The bridge also accepts `KILO_API_KEY` as an environment variable (it wins over the `.env` file). Optional overrides (config values, NOT env vars): `model`, `requests_per_hour`, `model_context_limit` in `bridge/config.json` — these are the supported knobs.

## Step 3 — Run the bridge
```bash
python3 bridge/run.py --config bridge/config.json
```
Binding port 443 requires privileges — either:
```bash
sudo python3 bridge/run.py --config bridge/config.json
```
or run unprivileged behind a redirect (set `"tls_port": 8443` in bridge/config.json, then):
```bash
sudo iptables -t nat -A OUTPUT -p tcp -d 127.0.0.1 --dport 443 -j REDIRECT --to-port 8443
```
Leave the process running. The bridge logs to stdout only — pipe through `tee` to keep a copy (`tee bridge.log`; `*.log` is gitignored). systemd unit example:
```ini
[Service]
ExecStart=/usr/bin/python3 bridge/run.py --config bridge/config.json
WorkingDirectory=/path/to/p2k-bridge
User=root            # or run as user + AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure
[Install]
WantedBy=multi-user.target
```
Enable with `sudo systemctl enable --now p2k-bridge`.

## Step 4 — Point api.player2.game at the bridge
Add `127.0.0.1 api.player2.game` to /etc/hosts on the machine running the server (and each client machine). One-liners:
```bash
# Linux / macOS
echo "127.0.0.1 api.player2.game" | sudo tee -a /etc/hosts
# Windows (PowerShell as Administrator)
Add-Content -Path $env:windir\System32\drivers\etc\hosts -Value "127.0.0.1 api.player2.game"
```
Flush DNS afterwards: `sudo resolvectl flush-caches` (Linux), `sudo dscacheutil -flushcache` (macOS), or `ipconfig /flushdns` (Windows).

## Step 5 — Trust the CA in every JVM
Because TLS is terminated locally, the CA must be trusted in BOTH:
1. the dedicated-server JVM
2. every Minecraft client JVM — the launcher's bundled runtime (one per launcher installation, NOT one per instance)

```bash
bash tls/trust_jvm.sh <path-to-java-home>
```
Pass the directory that contains `bin/` and `lib/` (or point at a `cacerts` file directly). Runtime locations: the dedicated server — `readlink -f "$(which java)"` then drop the trailing `bin/java`; vanilla launcher — Linux `~/.minecraft/runtimes/java-runtime-*/`, macOS `~/Library/Application Support/minecraft/runtimes/java-runtime-*/`, Windows `%APPDATA%\.minecraft\runtimes\java-runtime-*\` and `%PROGRAMFILES%\Minecraft Launcher\runtime\java-*-jre\`; Prism/MultiMC — the instance's bundled runtime. The script prints `TRUST OK: <cacerts>` on success, imports alias `p2k-bridge-ca`, and supports `P2K_STOREPASS` to override the storepass (default `changeit`). Undo with `keytool -delete -alias p2k-bridge-ca -keystore <cacerts> -storepass changeit`. Importing into the wrong runtime is the number one cause of handshake failures — the server and the launcher use DIFFERENT JVMs.

Network layout notes: this bridge is local-only — nothing is reachable from the internet. If the server and clients run on separate machines, the hosts entry must point at the bridge machine's LAN IP, the bridge must listen on that IP (set `listen_host` and consider that the TLS listener binds 0.0.0.0 — add a firewall rule for internet-facing hosts), and Step 5 must be repeated for each player machine.

## Step 6 — Verify end-to-end
1. Start the dedicated server: `java -jar dedicated-server.jar nogui` (or your launcher script).
2. Join with your client and open chat (T).
3. Say hello to the companion (the message must contain the companion name — "Kilo" by default) and wait for the reply.

Expected: the mod authenticates without any click — no web login, no device-code link — and the NPC replies in chat.

Bridge-side checks:
```bash
python3 bridge/tests/selftest.py           # offline HTTP layer + TLS handshake
python3 bridge/tests/selftest.py --live    # + one real Kilo Gateway call (needs .env)
python3 bridge/run.py --check              # pinned model still listed? exit 0/1
curl -s http://127.0.0.1:9800/bridge/healthz
curl -s http://127.0.0.1:9800/bridge/stats
```
Startup log lines to expect (stdout): the listener banner (`plain HTTP :9800`, `login shim :4315`, `TLS :443 (servername api.player2.game)` all `[active]`, `model`, `quota: 180 requests/hour`, `kilo key: configured`), then per chat call one line:
```
[INFO] p2k.bridge: chat status=ok 242tok (74in/168out) 1523ms :: "preview"
```

## How it works
The mods still believe they are talking to the Player2 platform. The bridge answers every endpoint they use:

| Endpoint | Behavior |
|---|---|
| POST `/v1/chat/completions` | Forwards to the Kilo Gateway; the model comes from the identified character's override when set, else the global one (`nvidia/nemotron-3-super-120b-a12b:free`); normalizes the model output into the mod's `{reason, command, message}` JSON; never returns non-200 — degraded idle replies instead |
| GET `/v1/health` | Heartbeat (`client_version` key present) |
| GET `/v1/selected_characters` | Served from `bridge/characters.json` when present, else from `bridge/config.json`; hot-reloaded on change |
| POST `/v1/login/web/{gameKey}` | Instant p2Key (served by both the 4315 shim and the TLS listener) |
| POST `/v1/login/device/new` + `/token` | Device flow answered locally (same routes on both listeners) |
| POST `/v1/stt/start` / `/v1/stt/stop` | The mods' own STT protocol. Stubbed, and deliberately left that way — see **Voice input** below |
| POST `/v1/stt/transcribe` | Speech-to-text for the voice addon. Returns `{"text": "..."}`; never a non-200 |
| POST `/v1/tts/speak` | No-op stub |
| POST `/v1/minecraft/schematics/search` | Stub returning `{"results": [], "schematicInfos": []}` (schematics disabled, logged) |
| GET `/` | Service fingerprint |

### Tokens, requests, and the (disabled) token budget
1. **`max_tokens` = 500,000 requested per call** (free reign). The pinned model's context window is 262,144 tokens, so the bridge clamps per request: `effective = min(500000, 262144 − input − 1024)`, floored at 256, never above `max_tokens`. 500k intentionally exceeds the model's context window — the clamp makes it safe. If a 500k+ *working* context is ever wanted, `nvidia/nemotron-3-ultra-550b-a55b:free` has a 1M context — pin it via the config's `model` and set `"model_context_limit": 1000000`.
2. **Token budget per window: DISABLED** (`token_budget_per_window: 0`) — no request is ever denied on tokens. Usage is still *tracked*: `/bridge/stats` reports the `token_budget` object (`window_used`, `lifetime_prompt`, `lifetime_completion`, `lifetime_total`, `lifetime_denials`). To re-enable enforcement set a positive value (e.g. `5000000`): the bridge then reserves estimated input + effective output, forwards only if it fits, reconciles to Kilo's reported usage, and refunds failed calls.
3. **The only hard limiter: 180 requests/hour** (continuously refilling token bucket; Kilo caps free models at ~200 req/hr/IP). When the cap trips, chat requests get an all-empty envelope — the NPC silently idles — `rate_limited` climbs, and a WARNING is logged; the bucket frees one request every 20 s.

Ops visibility: `GET /bridge/stats` embeds `token_budget` + `requests {limit_per_hour, available, refill_per_sec}` + `recent_requests` (the journal, newest-first, ≤20: `ts`, `status`, `duration_ms`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `preview`).

## Monitoring
- Per-request log line (one per chat call): `[INFO] p2k.bridge: chat status=ok 242tok (74in/168out) 1523ms :: "preview"` — statuses are `ok`, `rate_limited`, `kilo_error`, `budget_exhausted` (the last is unreachable with the default disabled budget). When the request could be attributed, the line also carries `kind=`, `char=` and `model=`. The API key is never logged.
- Live dashboard:
  ```bash
  python3 bridge/monitor.py [--url http://127.0.0.1:9800] [--interval 1.0] [--once]
  ```
  Shows requests remaining this hour + refill rate, the counters (`chat_requests/ok/failed`, `rate_limited`, `kilo_errors`, bytes up), token usage (window + lifetime, with the disabled-budget note), and a recent-requests table (time, status, in/out/total tokens, ms, preview — at most 10 rows live, ≤20 in the JSON).
- `curl -s http://127.0.0.1:9800/bridge/stats` — JSON keys: `started_at`, `uptime_seconds`, `chat_requests`, `chat_ok`, `chat_failed`, `kilo_errors`, `rate_limited`, `bytes_up`, `bytes_down` (always 0 — request bytes only), `recent_requests`, `token_budget`, `requests`.
## Voice input

**Use [Microphone Text Input](https://modrinth.com/mod/mcmti) (`mcmti`).** It runs
Whisper **locally on the player's machine**, sends the recognised text as an
ordinary chat message, and PlayerEngine already hooks exactly that — no cloud, no
API key, no quota, and no changes to this bridge at all.

```
press V and speak (mcmti, client side)
  → Whisper runs locally (offline, no LLM, no API key)
  → the text is sent as a NORMAL CHAT MESSAGE
  → PlayerEngine's own chat path: Architectury ChatEvent$Received → by-name gate
  → POST /v1/chat/completions   ← the bridge seam that already works
  → NPC replies in text
```

### Why this works with no new code

Verified on the rig rather than assumed. A plain chat message containing the
companion name produced exactly the same request a `playerengine:user_message`
packet produces — the bridge logged:

```
chat status=ok kind=conversation char=kilo model=nvidia/nemotron-3-super-120b-a12b:free
  5300tok (4879in/421out) 4854ms :: "{"userMessage":"User Message: [P2KTestA]: please say hello to me"}"
```

and the NPC answered with a greeting command. Note the companion name is
consumed by the by-name gate, so `Kilo, please say hello to me` arrives as
`User Message: [player]: please say hello to me` — identical to typed chat.

### Setup

Put these three jars in the **player's** `.minecraft/mods/` folder (the client,
not the server):

| Mod | File | Why |
|---|---|---|
| Microphone Text Input | `mcmti-2.2.1+1.21-neoforge.jar` | The speech mod. NeoForge, 1.21.1, MIT, client-side only. |
| MidnightLib | `midnightlib-neoforge-1.9.3+1.21.1.jar` | Required dependency (its config GUI). |
| Architectury API | `architectury-neoforge-13.0.11-neoforge.jar` | Required dependency. |

Then download a **GGML Whisper model** from
[huggingface.co/ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/tree/main)
— `ggml-base.en.bin` (~75 MB) is the sensible start, `ggml-small.en.bin`
(~460 MB) is noticeably better. Point the mod at it:
**Mods → Microphone Text Input → GGML Whisper Model**.

| Setting | Default | Notes |
|---|---|---|
| Mode | `RELEASE_KEY_TO_SEND` | Set to `AUTO_SEND` to send as soon as recognition finishes. |
| Keybinding | `V` | Held to record, released to transcribe and send. |
| `prefix` | `⌈Speech Input⌋` | Prepended to the recognised text. **Clear it** if you don't want it in the message the NPC sees. |
| `vad` | off | Optional; needs the whisper-vad model. |

It is **client-side only**, so the server needs nothing — but every player who
wants voice needs a modded client with those three jars and a Whisper model.
Vanilla clients cannot use it.

### Verified shapes, and one warning

`scripts/mcmti_format_probe.js` drives exactly what mcmti emits and asserts the
outcome. Results, on the rig:

| What mcmti sends | Expected | Result |
|---|---|---|
| `⌈Speech Input⌋ Kilo, get me three oak logs` | NPC turn | ✅ |
| `⌈Speech Input⌋ hey Kilo can you help me` | NPC turn | ✅ |
| `⌈Speech Input⌋ does anybody know the way to the mines` | ignored by the gate | ✅ correctly ignored |
| `Kilo, please say hello` (prefix cleared) | NPC turn | ✅ |

So the mod's by-name gate is a **substring** match, not a prefix match — the name
can appear anywhere in the message, and mcmti's default `prefix` does not break
it. The third row is the useful one: without the companion name the message is
correctly ignored, so voice cannot accidentally trigger NPCs that weren't named.

**Run one companion for voice.** With two characters in the roster the
companions talk to each other (`kind=npc_dialogue`), and that chatter was
observed to **starve a player's turn**: a message with the companion name was
queued but never got its own bridge call, because the NPC's turns were spent
answering the other NPC. The same four messages worked perfectly with a single
companion. For responsive voice, keep `bridge/characters.json` to one character;
use multi-character rosters for demonstrating per-character models.

### Server-side voice (the other path)

`POST /v1/stt/transcribe` exists for a server-side alternative that would work
for **vanilla** clients: capture the microphone in a NeoForge addon, POST the
audio here, and inject the returned transcript. The mods themselves cannot
capture audio — PlayerEngine has no microphone code at all (no `TargetDataLine`
anywhere in the jar) and its own `/v1/stt/*` endpoints carry no audio, which is
why the addon is required.

It is **dormant**, because no free gateway model can actually transcribe:

| Model | Result |
|---|---|
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | Accepts the request but reports `audio_tokens: 0` in every accepted format — the audio is never ingested. |
| `thinkingmachines/inkling-small:free` | `429 Rate limit exceeded … Daily limit reached` |

So it needs a paid key. Cheapest usable options:

| Model | in / out per Mtok |
|---|---|
| `mistralai/voxtral-small-24b-2507` (default) | $0.10 / $0.30 — purpose-built audio model |
| `xiaomi/mimo-v2.6-flash` | $0.10 / $0.28 |
| `google/gemini-2.5-flash-lite` | $0.10 / $0.40 |

**Transcription accuracy is unmeasured** for any of these. Prefer `mcmti` unless
you specifically need vanilla clients.

#### Configuration

| Key | Default | Meaning |
|---|---|---|
| `stt_enabled` | `true` | Master switch for the server-side path. |
| `stt_model` | `mistralai/voxtral-small-24b-2507` | The gateway model used for transcription. |
| `stt_temperature` | `0.0` | Passed to the transcription call. |
| `stt_max_seconds` | `20` | Longest utterance accepted; longer audio is rejected. |
| `stt_prompt` | *(verbatim-transcription prompt)* | Override if a model needs different wording. |

A character may set its own `stt_model` in `bridge/characters.json` (see
docs/CHARACTERS.md).

#### The fail-open contract

A valid request **always** returns 200. Bad input, an oversized utterance, quota
exhaustion, a gateway error, or a model that heard nothing all return
`{"text": ""}`, which the addon treats as "nothing was said" and the NPC simply
ignores. This matters because the caller runs on Simple Voice Chat's packet
processing thread, where a thrown exception would disrupt voice chat for **every**
player, not just the speaker. The same contract the chat endpoint already honours.

## Characters

The NPC roster lives in `bridge/characters.json` (`{"characters":[{id, name, short_name, greeting, description, voice_ids, meta:{skin_url}}]}`) and hot-reloads without restarting the bridge — changes appear the next time the in-game H menu is opened.

Each character may override the global model, `temperature` and `max_tokens` (opt-in; unset means inherit). See **[CHARACTERS.md](CHARACTERS.md)** for the full table — and note that overrides placed in the `characters` array of `bridge/config.json` are ignored while `bridge/characters.json` exists (the bridge warns at startup when that happens). Context windows are read from the gateway catalog, not configured per character.

```bash
python3 bridge/characters.py list                              # table incl. IDs
python3 bridge/characters.py add --name "Bob the Miner" --short "BOB" --greeting "Hey there, name's Bob." --description "A gruff but friendly miner."
python3 bridge/characters.py show --name "Bob the Miner"
python3 bridge/characters.py remove --name "Bob the Miner"
```

The mod requires a non-empty `id` per character (it persists the companion's state; characters without one abort spawning with "Persisted companion state was unavailable or rejected"). The CLI auto-generates the id (slug of the short name, e.g. `bob`) and the bridge backfills missing ids at serve time without rewriting the file. Keep ids stable — renaming an id orphans the companion's stored state. Full guide: docs/CHARACTERS.md.

## Chat-call logging and routing

Every chat call is logged with what it turned out to be, which makes it easy to
see how a turn breaks down:

```
[INFO] p2k.bridge: chat status=ok kind=conversation char=kilo model=nvidia/nemotron-3.5-lightning:free 2845tok (1201in/1644out) 41081ms :: "preview"
```

`kind` is one of `conversation`, `command_feedback`, `greeting`, `event`,
`npc_dialogue`, `memory_extraction` or `unknown`. It is derived from the payload
the mod sends and affects **nothing functional today** — it is there so you can
tell how a turn splits into calls, and so a future routing policy can be a config
change. An `unknown` kind means the payload matched no known shape, which is the
signal to check that the mods have not changed their format.

`kind`, `character_id` and `model` are also in `/bridge/stats` →
`recent_requests[]`.

## Pre-flight diagnostic (recommended)
```bash
python3 bridge/doctor.py [--json] [--quiet]
```
Checks, in one run: python version, `.env`/key presence, TLS certs + SAN, the hosts entry, bridge liveness on 9800, the login shim on 4315, the TLS listener on 443 (with the CA), JVM trust per discovered java home (including launcher runtimes — with the exact `trust_jvm.sh` fix when missing), Kilo Gateway reachability + pinned-model presence, every model named by the roster, one real end-to-end chat call through the bridge, and the request quota. Exit 0 = all PASS/WARN, 1 = any FAIL. A green doctor run means the whole chain works on your machine.

## Troubleshooting
| Symptom | Likely cause & fix |
|---|---|
| `SSLHandshakeException` / TLS errors in the server log | CA not in the right JVM's cacerts — re-run `bash tls/trust_jvm.sh <java-home>` against the exact runtime used (server vs client; vanilla vs Prism runtimes differ; restart the JVM after import) |
| NPC stays silent, no chat reply | Check the bridge stdout for rate-limit or Kilo errors; `curl /bridge/stats` — look at `rate_limited` and `token_budget.lifetime_total`; if the cap is exhausted, wait for refill or raise `requests_per_hour` |
| Model unavailable / upstream 404 | Kilo catalog churn — verify the pinned model is still listed (`run.py --check`); pin `model` in bridge/config.json to another listed ID |
| "click link" login prompt still appears | The 4315 shim is not running or /etc/hosts wasn't updated — check listeners (`ss -ltnp \| grep -E '9800\|4315\|443'`) and restart the bridge |
| Address already in use on 9800 or 4315 | Another bridge instance is running — stop it first |
| Address already in use on 443 | Something else serves HTTPS locally — use the iptables redirect instead, or free the port |
| Mod hangs on login forever | DNS cache still points at the real platform — flush caches and restart both server and client |
| `curl https://api.player2.game/...` fails issuer verification | The CA isn't in the system store — run `bash bootstrap.sh` as root, or use `curl --cacert tls/certs/p2k_ca.crt` (the game only needs the JVM trust — Step 5) |
| Companion won't spawn (PersistentData error) | The selected character lacks an `id` — see the Characters section; add one via `characters.py` or let the bridge backfill it |
