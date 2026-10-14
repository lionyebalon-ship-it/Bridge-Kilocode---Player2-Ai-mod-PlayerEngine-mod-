#!/usr/bin/env node
// test_bot_framing.js — regression test for the playerengine:user_message wire
// framing in scripts/bot_template.js.
//
// Why this exists: the payload is a triple-nested varint frame and a 2-level
// frame is silently rejected by the mod ("Not enough bytes in buffer, expected
// 75, but got 31"). The test asserts the exact byte structure and that a
// decoder mirroring the Java stack (Architectury byte-array codec, then the
// mod's FriendlyByteBuf.readUtf) recovers the message. It reads the real
// bot_template.js so the tested code cannot drift from the shipped one.
//
// usage: node test_bot_framing.js        (run from the repo root or scripts/)

const fs = require('fs');
const path = require('path');

const failures = [];
let checks = 0;

function check(label, cond, detail) {
  checks++;
  if (cond) {
    console.log('  ok   ' + label);
  } else {
    console.log('  FAIL ' + label + (detail ? '  (' + detail + ')' : ''));
    failures.push(label);
  }
}

const TEMPLATE = path.join(__dirname, 'bot_template.js');
if (!fs.existsSync(TEMPLATE)) {
  console.error('cannot find bot_template.js at ' + TEMPLATE);
  process.exit(2);
}
const src = fs.readFileSync(TEMPLATE, 'utf8');

// --- extract the framing helpers from the real template (no duplication) ----
// The template references `Bot`/mineflayer at load time, so only the two pure
// helpers are lifted out of the source text.
function extractFunction(name, text) {
  const start = text.indexOf('function ' + name);
  if (start === -1) throw new Error(name + '() not found in bot_template.js');
  // find the end of the function body by brace matching
  let depth = 0, i = text.indexOf('{', start);
  for (; i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}') { depth--; if (depth === 0) break; }
  }
  return text.slice(start, i + 1);
}

const harness = new Function(
  extractFunction('writeVarint', src) + '\n' +
  extractFunction('buildUserMessagePayload', src) + '\n' +
  'return { writeVarint: writeVarint, buildUserMessagePayload: buildUserMessagePayload };'
)();
const writeVarint = harness.writeVarint;
const buildUserMessagePayload = harness.buildUserMessagePayload;

// --- a decoder mirroring the Java receive path -------------------------------
// Level 1: Architectury's byte-array codec (NetworkAggregator) strips the outer
//          varint length and hands the remaining bytes to the channel handler.
// Level 2: the mod's FriendlyByteBuf.readUtf() reads its own varint length.
function decodeWire(buf) {
  // read varint
  function readVarint(b, offset) {
    let result = 0, shift = 0, pos = offset.ref;
    for (;;) {
      if (pos >= b.length) throw new Error('varint out of bounds');
      const byte = b[pos++];
      result |= (byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) break;
      shift += 7;
    }
    offset.ref = pos;
    return result >>> 0;
  }
  const o1 = { ref: 0 };
  const outerLen = readVarint(buf, o1);
  if (o1.ref + outerLen !== buf.length) {
    throw new Error('outer varint length mismatch: ' + outerLen + ' vs ' + buf.length);
  }
  // level 2: Architectury's byte array — a second varint + payload
  const o2 = { ref: o1.ref };
  const byteArrayLen = readVarint(buf, o2);
  const innerStart = o2.ref;
  if (innerStart + byteArrayLen > buf.length) {
    throw new Error('byte-array frame overruns the buffer');
  }
  // level 3: the mod's readUtf
  const o3 = { ref: innerStart };
  const utf8Len = readVarint(buf, o3);
  if (o3.ref + utf8Len > innerStart + byteArrayLen) {
    throw new Error('utf8 frame overruns its byte-array frame');
  }
  return {
    outerLen: outerLen,
    byteArrayLen: byteArrayLen,
    utf8Len: utf8Len,
    text: buf.slice(o3.ref, o3.ref + utf8Len).toString('utf8'),
    consumed: buf.length,
  };
}

const MESSAGE = 'Kilo, can you get me 3 oak logs?';

console.log('bot_template.js framing');
const wire = buildUserMessagePayload(MESSAGE);
check('payload is a Buffer', Buffer.isBuffer(wire));
check('payload is non-empty', wire.length > 0);

let decoded;
try {
  decoded = decodeWire(wire);
} catch (exc) {
  // A frame the Java receive path rejects -> report as a failed check, not a crash.
  check('frame decodes through the Java receive path', false, exc.message);
  console.log('');
  console.log(failures.length + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
check('outer varint spans the whole payload', decoded.outerLen === MESSAGE.length + 2,
      'outerLen=' + decoded.outerLen + ' expected=' + (MESSAGE.length + 2));
check('byte-array frame is exactly one byte longer than the utf8 frame',
      decoded.byteArrayLen === decoded.utf8Len + 1,
      'byteArrayLen=' + decoded.byteArrayLen + ' utf8Len=' + decoded.utf8Len);
check('utf8 frame length equals the message byte length',
      decoded.utf8Len === Buffer.byteLength(MESSAGE, 'utf8'));
check('round-trip recovers the message', decoded.text === MESSAGE, repr(decoded.text));
check('no trailing bytes', decoded.consumed === wire.length);

// the exact wire bytes for the canonical test message (pins the shape).
// "Kilo, can you get me 3 oak logs?" is 32 bytes:
//   varint(34) outer -> varint(33) byte-array -> varint(32) utf8 -> text
const EXPECTED = Buffer.concat([
  Buffer.from([0x22]), // varint(34) outer
  Buffer.from([0x21]), // varint(33) byte array
  Buffer.from([0x20]), // varint(32) utf8
  Buffer.from(MESSAGE, 'utf8'),
]);
check('exact wire bytes for the canonical message', wire.equals(EXPECTED),
      'got hex=' + wire.toString('hex'));

// multi-byte utf8 must use byte lengths, not char lengths
const UNI = 'Kilo, gö the logs ✓';
let u;
try {
  u = decodeWire(buildUserMessagePayload(UNI));
} catch (exc) {
  check('multibyte utf8 frame decodes', false, exc.message);
  u = null;
}
if (u) {
  check('multibyte utf8 uses byte lengths', u.utf8Len === Buffer.byteLength(UNI, 'utf8'));
  check('multibyte utf8 round-trips', u.text === UNI);
}

// long messages need multi-byte varints
const LONG = 'Kilo, ' + 'x'.repeat(200) + ' logs?';
let l;
try {
  l = decodeWire(buildUserMessagePayload(LONG));
} catch (exc) {
  check('long message frame decodes', false, exc.message);
  l = null;
}
if (l) {
  check('long message utf8 frame uses a multi-byte varint',
        Buffer.byteLength(LONG, 'utf8') >= 128 && l.utf8Len === Buffer.byteLength(LONG, 'utf8'));
  check('long message round-trips', l.text === LONG);
}

function repr(s) { return JSON.stringify(s); }

console.log('');
if (failures.length) {
  console.log(failures.length + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
console.log('all ' + checks + ' framing checks passed');
