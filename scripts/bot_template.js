// bot_template.js — p2k-bridge test client (copied to <rig>/bot/bot.js by
// scripts/build_test_rig.sh).
//
// Connects a mineflayer bot to the local NeoForge server (127.0.0.1:25565),
// lets the Player2 companion auto-spawn, then sends a chat message to the
// PlayerEngine mod on its C2S plugin channel 'playerengine:user_message'.
// Afterwards it watches position + inventory (how the test detects
// task-completion side effects) until a watchdog fires.
//
// usage: node bot.js <Username> "<message>" [watchSeconds]
//   watchSeconds defaults to 170.
//
// env: KILO_NAME  companion name to address (default "Kilo" — the mod has a
//                 by-name chat gate; the message is prefixed if it lacks it)
//
// The C2S payload framing is the tricky part, proven against NeoForge 1.21.1 +
// Architectury: the mod reads a ByteBufCodecs.BYTE_ARRAY (varint length +
// bytes) whose CONTENT is another length-prefixed UTF-8 string. So the
// payload is varint(len(inner)) + inner, inner = varint(utf8ByteLength) + utf8.
// (A single-level wrapper fails with "Not enough bytes in buffer" — the
// receiver has already consumed the inner varint.)

const COMPANION_NAME = process.env.KILO_NAME || 'Kilo';
const username = process.argv[2];
const message = process.argv[3];
const watchSeconds = parseInt(process.argv[4] || '170', 10);

if (!username || !message) {
  console.error('usage: node bot.js <Username> "<message>" [watchSeconds]');
  process.exit(2);
}

function ts() {
  return new Date().toISOString();
}

function log() {
  console.log('[' + ts() + ']', ...arguments);
}

function logErr() {
  console.error('[' + ts() + ']', ...arguments);
}

function writeVarint(value) {
  const out = [];
  let v = value >>> 0; // treat as unsigned
  for (;;) {
    const byte = v & 0x7f;
    v >>>= 7;
    if (v !== 0) {
      out.push(byte | 0x80);
    } else {
      out.push(byte);
      break;
    }
  }
  return Buffer.from(out);
}

// Nested length-prefixed payload for the 'playerengine:user_message' channel.
function buildUserMessagePayload(text) {
  const msg = Buffer.from(text, 'utf8');
  const inner = Buffer.concat([writeVarint(msg.length), msg]);
  return Buffer.concat([writeVarint(inner.length), inner]);
}

// Find the raw protocol client. mineflayer 4.x moved it to bot._client; 3.x
// used bot.client. Returns null if neither exists (caller throws).
function findClient() {
  const candidates = [bot._client, bot.client, bot.client && bot.client.client];
  for (let i = 0; i < candidates.length; i++) {
    const client = candidates[i];
    if (client && typeof client.write === 'function') return client;
  }
  return null;
}

// Send a plugin message on the given channel; throws a clear error if no
// writer is found so the byte framing stays untouched.
function sendPluginMessage(channel, payload) {
  const client = findClient();
  if (!client) {
    throw new Error('no mineflayer protocol writer found (tried bot._client and bot.client)');
  }
  client.write('custom_payload', { channel: channel, data: payload });
}

// The routed message must always name the companion so PlayerEngine picks the
// right NPC (matches the "Kilo" character in bridge/config.json).
const outgoing = message.includes(COMPANION_NAME) ? message : (COMPANION_NAME + ' ' + message);
log('companion="' + COMPANION_NAME + '" outgoing message: ' + JSON.stringify(outgoing));
log('watchdog=' + watchSeconds + 's');

// --- connect ------------------------------------------------------------
let mineflayer;
try {
  mineflayer = require('mineflayer');
} catch (e) {
  logErr('cannot load mineflayer (run: npm install mineflayer in this dir). ' +
    (e && e.message ? e.message : e));
  process.exit(3);
}

const bot = mineflayer.createBot({
  host: '127.0.0.1',
  port: 25565,
  username: username,
  auth: 'offline',
  version: '1.21.1',
});

// --- event logging ------------------------------------------------------
bot.on('login', function () {
  log('event: login');
  attachS2CListener();
});
bot.on('error', function (err) { logErr('event: error:', err && err.message ? err.message : err); });
bot.on('kicked', function (reason) {
  logErr('event: kicked:', typeof reason === 'string' ? reason : JSON.stringify(reason));
});
bot.on('end', function (reason) { log('event: end:', reason); });

// Log ALL server chat messages (the NPC's spoken replies live here).
bot.on('message', function (jsonMsg) {
  let text;
  try {
    text = jsonMsg ? jsonMsg.toString() : '';
  } catch (_) {
    text = String(jsonMsg);
  }
  log('chat(message):', text);
});

// Log ALL S2C custom_payload packets on playerengine/chat channels. mineflayer
// does not deserialize these into named events, so we read them raw (utf8).
// Attached on 'login' because the protocol client object only reliably exists
// after the connection completes.
function attachS2CListener() {
  const client = findClient();
  if (!client || typeof client.on !== 'function') {
    logErr('no protocol client for S2C packet logging (orders still work)');
    return;
  }
  client.on('packet', function (data, meta) {
    const name = meta && meta.name;
    if (name !== 'custom_payload' && name !== 'plugin_message') return;
    let channel;
    try {
      channel = data && data.channel && data.channel.toString ? data.channel.toString() : String(data && data.channel);
    } catch (_) {
      channel = '?';
    }
    if (!(channel.indexOf('playerengine') !== -1 || channel.indexOf('chat') !== -1)) return;
    let raw = '';
    try {
      raw = data && data.data ? data.data.toString('utf8') : '';
    } catch (_) {
      raw = '<binary>';
    }
    log('S2C custom_payload channel=' + channel + ' raw=' + JSON.stringify(raw));
  });
  log('S2C packet listener attached');
}

// --- send on spawn (after companion auto-spawn) -------------------------
let spawned = false;
bot.on('spawn', function () {
  log('event: spawn — bot joined; ready');
  if (spawned) return;
  spawned = true;
  log('event: spawn — waiting 5s for companion auto-spawn, then sending order');
  setTimeout(function () {
    try {
      const payload = buildUserMessagePayload(outgoing);
      sendPluginMessage('playerengine:user_message', payload);
      log('sent user_message on playerengine:user_message (' + payload.length + ' bytes)');
    } catch (e) {
      logErr('failed to send user_message:', e && e.message ? e.message : e);
    }
  }, 5000);
});

// --- inventory + position polling (side-effect detection) ---------------

let prevInventory = null;

function inventorySnapshot() {
  try {
    const items = bot.inventory ? bot.inventory.items() : [];
    if (!items.length) return '';
    return items.map(function (i) { return i.name + ' x' + i.count; }).sort().join(', ');
  } catch (_) {
    return '';
  }
}

function positionSnapshot() {
  try {
    const p = bot.entity && bot.entity.position;
    if (!p) return 'unknown';
    return p.x.toFixed(1) + ', ' + p.y.toFixed(1) + ', ' + p.z.toFixed(1);
  } catch (_) {
    return 'unknown';
  }
}

function poll() {
  const inv = inventorySnapshot();
  const pos = positionSnapshot();
  log('poll: pos=[' + pos + '] inventory=[' + (inv || '(empty)') + ']');
  if (inv !== prevInventory) {
    if (prevInventory !== null) {
      log('INVENTORY CHANGE: "' + (prevInventory || '(empty)') + '" -> "' + (inv || '(empty)') + '"');
    }
    prevInventory = inv;
  }
}

setInterval(poll, 10000);

// --- watchdog -----------------------------------------------------------

function finalSummary() {
  log('final summary: pos=[' + positionSnapshot() + '] inventory=[' + (inventorySnapshot() || '(empty)') + ']');
}

setTimeout(function () {
  log('watchdog fired after ' + watchSeconds + 's — exiting');
  finalSummary();
  process.exit(0);
}, watchSeconds * 1000);

process.on('SIGINT', function () { finalSummary(); process.exit(0); });
process.on('SIGTERM', function () { finalSummary(); process.exit(0); });
