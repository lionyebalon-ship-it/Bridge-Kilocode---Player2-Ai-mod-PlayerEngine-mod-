// multitarget_probe.js — which companions does a by-name message reach?
//
// Reads the MOD's own routing logs rather than the NPC replies, because with
// two companions NPC-to-NPC chatter can starve a turn and a missing reply would
// be ambiguous. The mod logs, per message:
//
//   Passing msg btw <NAME> <-> <user>       <- considered
//   queue for UUID=... name=<NAME> adding event=UserMessage(...)   <- actually queued
//
// "queued" is the answer: it is that companion's turn to act.
//
// usage: node multitarget_probe.js <username> [watchSeconds]

const username = process.argv[2] || 'P2KMulti';
const watchSeconds = parseInt(process.argv[3] || '95', 10);

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

// The three cases that answer the question:
//   name one    -> exactly that one is queued
//   name other  -> exactly that one is queued
//   name both   -> BOTH queued
const CASES = [
  { label: 'names ONE (Kilo)',     text: 'Kilo, please get me 3 oak logs' },
  { label: 'names OTHER (Blaze)',  text: 'Blaze, please get me 3 oak logs' },
  { label: 'names BOTH',           text: 'Kilo and Blaze, please get me 3 oak logs' },
];

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));

let step = 0;
bot.once('spawn', () => {
  log('spawn; driving the targeting cases');
  setTimeout(function tick() {
    if (step >= CASES.length) { log('probe complete'); return; }
    const item = CASES[step];
    try { bot.chat(item.text); log('SENT [' + item.label + '] ' + JSON.stringify(item.text)); }
    catch (e) { logErr('chat send failed: ' + e.message); }
    step++;
    setTimeout(tick, 28000);
  }, 6000);
});

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
