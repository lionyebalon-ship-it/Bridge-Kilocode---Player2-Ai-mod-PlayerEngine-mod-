// prefix_shape_probe.js — Phase 0 of the two-companion fix.
//
// Hypothesis: the mention parser only recognises a LEADING address. When mcmti's
// `prefix` sits in front of the companion name, the name is no longer leading, so
// the message falls into the multi-target/broadcast path instead of resolving to
// one companion -- and the multi-target path is where the turn gets lost.
//
// The signal to read is deliberately chosen NOT to depend on the NPC choosing to
// act, because that is what we are trying to measure. The mod strips the
// addressed companion's name when it resolves to a SINGLE target and leaves it in
// place when it resolves to several. So:
//
//   queue log shows message='get me 3 oak logs'            -> single target  (good)
//   queue log shows message='Kilo, get me 3 oak logs'      -> multi target   (bad)
//
// Three shapes, same wording, nothing but the text differs:
//   A  "Kilo, get me 3 oak logs"                 name first, no prefix
//   B  "⌈Speech Input⌋ Kilo, get me 3 oak logs"  prefix then name  <- suspect
//   C  "⌈Speech Input⌋ hey Kilo, get me 3..."    prefix, name mid
//
// NOTE what this probe actually established: B and C take the multi-target path
// (name left in place) and still get a turn and execute. The prefix does NOT
// break the by-name gate. The reason a voice command could be lost was the
// request quota: two companions drained the burst and the player's call was
// shed. That is fixed in the bridge (see reserve_tokens_for_players), so this
// probe is now a guard against regressing the gate itself.
//
// usage: node prefix_shape_probe.js <username> [watchSeconds]

const username = process.argv[2] || 'P2KShape';
const watchSeconds = parseInt(process.argv[3] || '130', 10);
const PREFIX = '\u2308Speech Input\u230f';

function log() { console.log('[' + new Date().toISOString() + ']', ...arguments); }
function logErr() { console.error('[' + new Date().toISOString() + ']', ...arguments); }

let mineflayer;
try { mineflayer = require('mineflayer'); }
catch (e) { logErr('cannot load mineflayer: ' + (e && e.message)); process.exit(3); }

const CASES = [
  { label: 'A name-first, no prefix',  good: true,
    text: 'Kilo, get me 3 oak logs' },
  { label: 'B prefix THEN name',       good: false,
    text: PREFIX + ' Kilo, get me 3 oak logs' },
  { label: 'C prefix, name mid-text',  good: true,
    text: PREFIX + ' hey Kilo, get me 3 oak logs' },
];

const bot = mineflayer.createBot({
  host: '127.0.0.1', port: 25565, username, auth: 'offline', version: '1.21.1',
});

bot.on('error', e => logErr('bot error: ' + (e && e.message)));
bot.on('kicked', r => logErr('kicked: ' + r));
bot.on('message', m => {
  let t; try { t = m.toString(); } catch (_) { t = String(m); }
  if (!t.startsWith('<' + username + '>')) log('NPC: ' + t);
});

let step = 0;
bot.once('spawn', () => {
  log('spawn; waiting out the companions\' spawn greetings before probing');
  // The spawn greetings are themselves LLM turns for BOTH companions; probing
  // during them would confound the result. Give them time to finish first.
  setTimeout(function tick() {
    if (step >= CASES.length) { log('probe complete'); return; }
    const item = CASES[step];
    try {
      bot.chat(item.text);
      log('SENT [' + item.label + '] ' + JSON.stringify(item.text));
      log('   expect: ' + (item.good ? 'SINGLE target (name stripped)' : 'MULTI target (bug)'));
    } catch (e) {
      logErr('chat send failed: ' + e.message);
    }
    step++;
    setTimeout(tick, 30000);
  }, 25000);
});

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
