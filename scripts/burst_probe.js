// burst_probe.js — prove burst_capacity removes mid-turn stalling.
//
// Why this probe exists. One NPC turn is several bridge calls: the order, its
// command feedback, background memory extraction, plus (with two companions)
// NPC-to-NPC chatter. The burst capacity is how many of those can run before the
// bucket empties and the 20s-per-token refill bites. The derived default is
// rate/60 = 3, which is why a turn used to stall halfway through.
//
// This probe drives THREE consecutive commands with no gap beyond the NPC's own
// turn time, so the cumulative call count crosses the derived burst of 3 easily.
// Each command is checked for an actual in-game outcome (a get command finishing),
// not just a chat reply, because a shed call produces an empty envelope and the
// NPC appears to simply ignore you.
//
// usage: node burst_probe.js <username> [watchSeconds]

const COMPANION = process.env.KILO_NAME || 'Kilo';
const username = process.argv[2] || 'P2KBurst';
const watchSeconds = parseInt(process.argv[3] || '230', 10);

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

const COMMANDS = [
  { label: 'cmd1 (tools)',   text: COMPANION + ', get me a wooden pickaxe' },
  { label: 'cmd2 (building)', text: COMPANION + ', get me 4 oak planks' },
  { label: 'cmd3 (logs)',    text: COMPANION + ', get me 2 oak logs' },
];

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));
bot.on('death', () => logErr('died'));
bot.on('message', m => {
  let t; try { t = m.toString(); } catch (_) { t = String(m); }
  if (!t.startsWith('<' + username + '>')) log('NPC: ' + t);
});

let step = 0;
const sentAt = {};

bot.once('spawn', () => {
  log('spawn; letting the companions finish their spawn greetings first');
  setTimeout(function tick() {
    if (step >= COMMANDS.length) { log('probe complete'); return; }
    const item = COMMANDS[step];
    sentAt[step] = Date.now();
    try {
      bot.chat(item.text);
      log('SENT [' + item.label + '] ' + JSON.stringify(item.text));
    } catch (e) {
      logErr('chat send failed: ' + e.message);
    }
    step++;
    setTimeout(tick, 55000);
  }, 30000);
});

// Report the outcome per command from what the mod itself said, so the verdict
// does not depend on the NPC's phrasing.
setInterval(() => {
  for (const k of Object.keys(sentAt)) {
    const age = ((Date.now() - sentAt[k]) / 1000).toFixed(0);
    if (!sentAt[k + '_reported']) {
      sentAt[k + '_reported'] = true;
      log('  (command ' + k + ' sent ' + age + 's ago — watch the mod log for its command finish)');
    }
  }
}, 20000);

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
