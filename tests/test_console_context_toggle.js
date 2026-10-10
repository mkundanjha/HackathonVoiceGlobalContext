// Context-file inspection must not affect the conversation or mix role histories.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../app/static/console.html'), 'utf8');
const initial = html.match(/const state = .*?;/)[0];
const code = html.slice(html.indexOf('function setContextRole('), html.indexOf('function renderContext()'));
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function setup() {
  const elements = {}, urls = [], renders = [];
  const buttons = ['buyer', 'seller'].map(role => ({dataset: {contextRole: role},
    classList: {toggle() {}}, setAttribute(name, value) { this[name] = value; }}));
  const env = {
    selectedGlid: '123', glid: () => env.selectedGlid,
    $: id => elements[id] ||= {value: '', textContent: '', options: [],
      classList: {remove() {}}, set innerHTML(v) {
        this.html = v;
        this.options = [...v.matchAll(/value="([^"]*)"/g)].map(m => ({value: m[1]}));
      }},
    document: {querySelectorAll: () => buttons},
    fetch: async url => { urls.push(url); return {ok: true, json: async () => url.includes('/history?')
      ? [{version: url.includes('role=buyer') ? 4 : 9}]
      : {path: url.includes('role=buyer') ? 'buyer.md' : 'seller.md'}}; },
    renderContext() { renders.push(vm.runInContext('state.ctx.path', env)); },
  };
  vm.createContext(env);
  vm.runInContext(initial + '\n' + code, env);
  return {env, urls, buttons, renders, state: vm.runInContext('state', env)};
}
async function main() {
  {
    const {env, urls, buttons, state} = setup();
    state.chatSid = 'seller-session'; state.pc = {connected: true};
    await env.setContextRole('buyer');
    assert.equal(state.role, 'seller');
    assert.equal(state.chatSid, 'seller-session');
    assert.equal(state.pc.connected, true);
    assert.equal(state.ctx.path, 'buyer.md');
    assert(urls.every(url => url.includes('role=buyer')));
    assert.equal(state.history[0].version, 4);
    assert.equal(buttons[0]['aria-pressed'], 'true');
    env.$('compare').value = '4';
    await env.setContextRole('seller');
    assert.equal(state.ctx.path, 'seller.md');
    assert.equal(state.history[0].version, 9);
    assert.equal(env.$('compare').value, '');
  }
  {
    const {env, state, renders} = setup();
    const old = deferred();
    env.fetch = async url => url.includes('role=buyer') ? old.promise :
      {ok: true, json: async () => url.includes('/history?') ? [] : {path: 'seller.md'}};
    const pending = env.setContextRole('buyer');
    await env.setContextRole('seller');
    old.resolve({ok: true, json: async () => ({path: 'stale buyer response'})});
    await pending;
    assert.equal(state.ctx.path, 'seller.md');
    assert.deepEqual(renders, ['seller.md']);
  }
  {
    const {env, state, renders} = setup();
    const old = deferred(); env.fetch = () => old.promise;
    const pending = env.refreshContext();
    env.selectedGlid = '456';
    old.resolve({ok: true, json: async () => ({path: 'old user file'})});
    await pending;
    assert.equal(state.ctx, null);
    assert.equal(renders.length, 0);
  }
  console.log('Passed 3 context-role isolation and stale-response scenarios.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
