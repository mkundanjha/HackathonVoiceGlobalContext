// Browser WAV framing, playback gating and cleanup; no microphone/network needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
let sends = [], stopped = 0, closedAudio = 0;
class AudioContext {
  constructor() { this.sampleRate = 16000; }
  close() { closedAudio++; return Promise.resolve(); }
}
const env = {window: {AudioContext}, WebSocket: {OPEN: 1},
  btoa: text => Buffer.from(text, 'binary').toString('base64'),
  atob: text => Buffer.from(text, 'base64').toString('binary'),
  URL, console, setTimeout};
vm.createContext(env);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/hosted_voice.js'), 'utf8'), env);
const encoded = env.encodeHostedWav([new Float32Array(48000).fill(0.5)], 48000);
const wav = Buffer.from(encoded, 'base64');
assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
assert.equal(wav.readUInt32LE(24), 16000);
assert.equal(wav.readUInt16LE(22), 1);
assert.equal(wav.readUInt32LE(40), 32000);
let phase = '';
const call = new env.HostedVoice({onPhase: value => { phase = value; }});
call.socket = {readyState: 1, send: message => sends.push(JSON.parse(message)), close() {}};
call.microphone = {getTracks: () => [{stop() { stopped++; }}]};
call.capture(new Float32Array(1024).fill(0.2));
assert.equal(sends.length, 0); // Opening is not finished yet.
call.waiting = false;
call.playing = true;
call.capture(new Float32Array(1024).fill(0.2));
assert.equal(sends.length, 0); // Do not transcribe the agent's own audio.
call.playing = false;
for (let i = 0; i < 6; i++) call.capture(new Float32Array(1024).fill(0.2));
for (let i = 0; i < 12; i++) call.capture(new Float32Array(1024));
assert.equal(sends.length, 1);
assert.equal(sends[0].type, 'audio');
assert.equal(call.waiting, true);
assert.match(phase, /preparing reply/);
call.close(); call.close();
assert.equal(sends.at(-1).type, 'stop');
assert.equal(stopped, 1);
assert.equal(closedAudio, 1);
console.log('Passed hosted WAV encoding, echo/playback gating, utterance framing and idempotent hangup.');
