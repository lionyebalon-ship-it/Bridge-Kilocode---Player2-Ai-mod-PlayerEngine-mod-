// endpoint_audit_probe.js — provoke every endpoint the mods know about.
//
// The bridge answers part of the mods' surface; until recently it logged nothing
// for the rest. This probe drives the features most likely to make the mods reach
// for an endpoint the bridge does not serve:
//
//   - spawn + greetings          (session, joule, profile endpoints)
//   - typed and by-name chat     (chat, memory)
//   - a build/schematic request  (schematics endpoints)
//   - a memory probe             (memory retrieval)
//   - a disconnect/reconnect     (login, session)
//
// The verdict is read from the BRIDGE, not from the NPC: every unserved request
// is now a WARNING naming its path, and /bridge/stats carries the running total.
//
// usage: node endpoint_audit_probe.js <username> [watchSeconds]

const COMPANION = process.env.KILO_NAME || 'Kilo';
const username = process.argv[2] || 'P2KAudit';
const watchSeconds = parseInt(process.argv[3] || '215', 10);

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));
bot.on('message', m => {
  let t; try { t = m.toString(); } catch (_) { t = String(m); }
  if (!t.startsWith('<' + username + '>')) log('NPC: ' + t);
});

// Each step is a different feature surface. The build step matters most: it is
// the one most likely to hit an endpoint the bridge has never served.
const STEPS = [
  { label: 'greeting-zone chat',  text: COMPANION + ', how are you today?' },
  { label: 'by-name command',     text: COMPANION + ', get me 1 oak log' },
  { label: 'BUILD (schematics)',  text: COMPANION + ', build me a small house' },
  { label: 'memory probe',        text: COMPANION + ', what did I ask you for earlier?' },
  { label: 'no-name chat',        text: 'just talking to myself here' },
];

let step = 0;
bot.once('spawn', () => {
  log('spawn; letting companions finish their spawn greetings first');
  setTimeout(function tick() {
    if (step >= STEPS.length) {
      log('chat phase complete — leaving so the mod runs memory extraction');
      return;
    }
    const item = STEPS[step];
    try {
      bot.chat(item.text);
      log('SENT [' + item.label + '] ' + JSON.stringify(item.text));
    } catch (e) {
      logErr('chat send failed: ' + e.message);
    }
    step++;
    setTimeout(tick, 32000);
  }, 30000);
});

// A disconnect/reconnect provokes the login and session endpoints, which are
// only reached on a fresh connection.
setTimeout(() => {
  log('disconnecting to provoke login/session endpoints on rejoin');
  try { bot.quit(); } catch (_) {}
}, (watchSeconds - 25) * 1000);

setTimeout(() => {
  log('reconnecting');
  try {
    const b2 = mineflayer.createBot({
      host: '127.0.0.1', port: 25565, username: username + 'B',
      auth: 'offline', version: '1.21.1',
    });
    b2.on('message', m => log('NPC2: ' + m.toString()));
    setTimeout(() => process.exit(0), 15000);
  } catch (e) {
    logErr('reconnect failed: ' + e.message);
    process.exit(0);
  }
}, (watchSeconds - 8) * 1000);

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
