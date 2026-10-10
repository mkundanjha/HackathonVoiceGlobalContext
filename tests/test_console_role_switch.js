// Offline interaction checks for role switching; run with node.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../app/static/console.html'), 'utf8');
const chat = html.slice(html.indexOf('// ---------- Chat ----------'), html.indexOf('// ---------- Context file ----------'));
const selectionStart = html.indexOf('function userChanged()');
const selection = html.slice(selectionStart, html.indexOf("document.querySelectorAll('#roleSeg button')", selectionStart));
const voice = html.slice(html.indexOf('async function startCall()'), html.indexOf('function iceGathered(pc)'));
const initialState = html.match(/const state = .*?;/)[0];

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}

function setup() {
  const elements = {}, calls = [];
  const env = {
    glid: () => '123',
    $: id => elements[id] ||= {value: '', textContent: '', disabled: false,
      classList: {add() {}, remove() {}}, focus() {}},
    api: async (url, data) => { calls.push({url, data}); return {session_id: `${data.role}-session`}; },
    setStatus() {}, note() {}, endCall() {}, resetFlow() {}, resetDemo() {},
    refreshContext() {}, setContextRole() {}, loadMemory() {}, BUILD: [], CONVERSE: [],
  };
  vm.createContext(env);
  vm.runInContext(initialState + '\n' + chat + '\n' + selection + '\n' + voice, env);
  return {env, calls, state: vm.runInContext('state', env), elements};
}

async function main() {
  {
    const {env, state, calls} = setup();
    const selectionGeneration = state.selectionGeneration;
    await env.$('btnChat').onclick();
    assert.equal(state.selectionGeneration, selectionGeneration); // Chat actions must not cancel voice setup.
    assert.equal(state.chatSid, 'seller-session');
    state.role = 'buyer'; env.userChanged();
    assert.equal(state.chatSid, null);
    assert(calls.some(c => c.url === '/api/chat/end' && c.data.session_id === 'seller-session'));
    await env.$('btnChat').onclick();
    assert.equal(state.chatSid, 'buyer-session');
    assert.equal(calls.at(-1).data.role, 'buyer');
  }
  {
    const {env, state, calls} = setup();
    const old = deferred();
    env.api = async (url, data) => {
      calls.push({url, data});
      if (url === '/api/chat/start' && data.role === 'seller') return old.promise;
      return {session_id: 'buyer-session'};
    };
    const pending = env.$('btnChat').onclick();
    state.role = 'buyer'; env.userChanged();
    await env.$('btnChat').onclick();
    old.resolve({session_id: 'late-seller-session'});
    await pending;
    assert.equal(state.chatSid, 'buyer-session');
    assert(calls.some(c => c.url === '/api/chat/end' && c.data.session_id === 'late-seller-session'));
    assert.equal(env.$('btnChat').textContent, 'End chat');
  }
  {
    const {env, state, calls} = setup();
    const status = deferred();
    env.agentStatus = () => status.promise;
    const pending = env.startCall();
    state.role = 'buyer'; env.userChanged();
    status.resolve({running: true, ready: true, glid: '123', role: 'seller'});
    await pending;
    assert(!calls.some(c => c.url === '/api/voice/start'));
    assert.equal(state.pc, null);
  }
  {
    const {env, state} = setup();
    const microphone = deferred();
    let stopped = false;
    env.agentStatus = async () => ({running: true, ready: true, glid: '123', role: 'seller'});
    env.setAgentPill = () => {};
    env.navigator = {mediaDevices: {getUserMedia: () => microphone.promise}};
    const pending = env.startCall();
    await new Promise(resolve => setImmediate(resolve));
    state.role = 'buyer'; env.userChanged();
    microphone.resolve({getTracks: () => [{stop: () => { stopped = true; }}]});
    await pending;
    assert(stopped);
    assert.equal(state.pc, null);
  }
  console.log('Passed 4 chat/voice role-switch scenarios.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
