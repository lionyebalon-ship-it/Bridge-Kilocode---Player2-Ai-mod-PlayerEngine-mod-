// chat_path_probe.js — verify that a NORMAL CHAT MESSAGE drives an NPC turn.
//
// Why this matters: the Microphone Text Input mod (mcmti) does not send a
// playerengine:user_message packet. It runs Whisper locally and, in AUTO_SEND
// mode, sends the recognized text as an ordinary player chat message. So the
// question this probe answers is whether PlayerEngine's own chat path — the
// Architectury `ChatEvent$Received` hook in ConversationManager — drives the
// bridge call, exactly as typed chat does for a real player.
//
// If it does, voice works with no new code anywhere: speak -> Whisper -> chat
// message -> by-name gate -> POST /v1/chat/completions -> NPC reply.
//
// It probes three messages so the by-name gate is covered too:
//   1. a message WITHOUT the companion name  (should be ignored)
//   2. a message WITH the companion name     (should drive a turn)
//
// usage: node chat_path_probe.js <username> [watchSeconds]

const COMPANION_NAME = process.env.KILO_NAME || 'Kilo';
const username = process.argv[2] || 'P2KChat';
const watchSeconds = parseInt(process.argv[3] || '110', 10);

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
  log('chat: ' + t);
});

let step = 0;
const script = [
  // Phase 1: no companion name -- the by-name gate should ignore it.
  { label: 'NO-NAME (gate should ignore)', text: 'can anyone hear me' },
  // Phase 2: with the companion name -- this is what mcmti's AUTO_SEND produces.
  { label: 'WITH-NAME (should drive a turn)', text: 'Kilo, please say hello to me' },
];

bot.once('spawn', () => {
  log('spawn; companion should spawn automatically');
  setTimeout(function tick() {
    if (step >= script.length) { log('probe complete'); return; }
    const item = script[step];
    try { bot.chat(item.text); log('SENT chat: ' + JSON.stringify(item.text) + '   [' + item.label + ']'); }
    catch (e) { logErr('chat send failed: ' + e.message); }
    step++;
    setTimeout(tick, 45000);
  }, 6000);
});

setTimeout(() => { log('watchdog fired'); process.exit(0); }, watchSeconds * 1000);
