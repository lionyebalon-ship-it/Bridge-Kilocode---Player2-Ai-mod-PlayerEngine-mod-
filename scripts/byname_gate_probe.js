// byname_gate_probe.js — determine the exact by-name gate semantics.
//
// Answers three things the docs cannot, because they live in the mod's
// CallByNameChatFilter and only a run can show them:
//
//   1. Is matching case-insensitive? Whisper emits sentence case ("kilo, ..."),
//      so this decides whether voice works at all for a lowercase name.
//   2. Does a name inside a bigger word match? "kilometer" contains "kilo" —
//      if that matches, unrelated chat would trigger the NPC.
//   3. Are other messages ignored, and what does the mod log say when they are?
//
// usage: node byname_gate_probe.js <username> [watchSeconds]

const NAME = process.env.KILO_NAME || 'Kilo';
const SHORT = process.env.KILO_SHORT || 'KILO';
const username = process.argv[2] || 'P2KGate';
const watchSeconds = parseInt(process.argv[3] || '150', 10);

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

// Order matters: each case is independent, and a turn takes ~15-25s, so the
// spacing below keeps one case from bleeding into the next.
const CASES = [
  { label: 'lowercase name',              expect: true,  text: 'hello ' + NAME.toLowerCase() + ' please say hello' },
  { label: 'UPPERCASE short_name',        expect: true,  text: 'hello ' + SHORT.toUpperCase() + ' please say hello' },
  { label: 'name inside a bigger word',   expect: false, text: 'a kilometer is one thousand meters, right' },
  { label: 'unrelated message',           expect: false, text: 'hello everyone, is anybody online tonight' },
  { label: 'name as the only word',       expect: true,  text: NAME },
  { label: 'name at the very end',        expect: true,  text: 'can you hear me ' + NAME.toLowerCase() },
];

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));
bot.on('message', m => {
  let t; try { t = m.toString(); } catch (_) { t = String(m); }
  // Only NPC replies matter; skip the raw echo of our own message.
  if (!t.startsWith('<' + username + '>')) log('NPC: ' + t);
});

let step = 0;
bot.once('spawn', () => {
  log('spawn; probing the by-name gate');
  setTimeout(function tick() {
    if (step >= CASES.length) { log('probe complete'); return; }
    const item = CASES[step];
    try {
      bot.chat(item.text);
      log('SENT [' + item.label + '] ' + JSON.stringify(item.text) +
          '   -> expect ' + (item.expect ? 'NPC TURN' : 'IGNORED'));
    } catch (e) {
      logErr('chat send failed: ' + e.message);
    }
    step++;
    setTimeout(tick, 22000);
  }, 6000);
});

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
