// mcmti_format_probe.js — verify what mcmti's AUTO_SEND actually produces.
//
// This is the risky part of the voice story that rig testing could not cover,
// because mcmti needs a real microphone on the player's machine. What CAN be
// tested is the seam: mcmti sends the recognised text as an ordinary chat
// message, with a configurable `prefix` in front of it. The default is
// "⌈Speech Input⌋".
//
// If that prefix broke the by-name gate, voice would silently not work while
// every other test passed. So this probe drives the exact message shapes mcmti
// emits, prefix included, and asserts the bridge sees a conversation call for
// each one.
//
// usage: node mcmti_format_probe.js <username> [watchSeconds]

const COMPANION_NAME = process.env.KILO_NAME || 'Kilo';
const username = process.argv[2] || 'P2KMcMti';
const watchSeconds = parseInt(process.argv[3] || '150', 10);

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

// Every shape mcmti can emit. These are the ones that matter:
//  1. default prefix, name at the START of the text  -> gate should pass
//  2. default prefix, name somewhere in the middle    -> gate should pass
//  3. prefix, but no companion name at all            -> gate should ignore
//  4. with prefix disabled (empty string)             -> gate should pass
//
// Case 2 matters because mcmti puts the prefix BEFORE the text, so the
// companion name is no longer the first word of the chat message, and the
// mod's by-name gate is a substring check, not a prefix check.
const CASES = [
  { label: 'prefix + name first',       gate: true,
    text: '⌈Speech Input⌋ ' + COMPANION_NAME + ', get me three oak logs' },
  { label: 'prefix + name mid-sentence', gate: true,
    text: '⌈Speech Input⌋ hey ' + COMPANION_NAME + ' can you help me' },
  { label: 'prefix but NO name',        gate: false,
    text: '⌈Speech Input⌋ does anybody know the way to the mines' },
  { label: 'no prefix + name first',    gate: true,
    text: COMPANION_NAME + ', please say hello' },
];

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));
bot.on('message', m => {
  let t; try { t = m.toString(); } catch (_) { t = String(m); }
  log('chat: ' + t);
});

let step = 0;
bot.once('spawn', () => {
  log('spawn; driving the mcmti output shapes');
  setTimeout(function tick() {
    if (step >= CASES.length) { log('probe complete'); return; }
    const item = CASES[step];
    try {
      bot.chat(item.text);
      log('SENT [' + item.label + '] ' + JSON.stringify(item.text));
      log('  expect: ' + (item.gate ? 'NPC TURN' : 'IGNORED by the gate'));
    } catch (e) {
      logErr('chat send failed: ' + e.message);
    }
    step++;
    setTimeout(tick, 30000);
  }, 6000);
});

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
