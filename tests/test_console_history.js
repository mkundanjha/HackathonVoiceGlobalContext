// Offline checks for History form visibility and submitted event payloads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../app/static/console.html'), 'utf8');
const fields = html.slice(html.indexOf('function updateDemoFields()'), html.indexOf("$('demoType').onchange"));
const submit = html.slice(html.indexOf("$('demoForm').onsubmit"), html.indexOf("$('btnDemoRetry').onclick"));
const elements = {};
const env = {
  $: id => elements[id] ||= {value: '', hidden: false, disabled: false, required: false},
  glid: () => '624518', state: {role: 'seller'}, demo: {pending: false},
  demoKey: () => '624518:seller', crypto: {randomUUID: () => 'test-event'},
  resetFlow() {}, demoBusy() {}, setNode() {}, HISTORY: [], refreshDemoStatus: async () => {},
  api: async (url, payload) => { env.payload = payload; },
};
vm.createContext(env);
vm.runInContext(fields + '\n' + submit, env);
async function main() {
  for (const [kind, visible] of [
    ['enquiry.created', ['Quantity', 'Unit', 'Product', 'City']],
    ['lead.available', ['Product', 'City']],
    ['ticket.opened', ['TicketId', 'Issue']],
    ['ticket.closed', ['TicketId']],
  ]) {
    env.$('demoType').value = kind;
    env.updateDemoFields();
    for (const name of ['Quantity', 'Unit', 'Product', 'City', 'TicketId', 'Issue']) {
      const shown = visible.includes(name);
      assert.equal(env.$('demo' + name + 'Field').hidden, !shown);
      assert.equal(env.$('demo' + name).disabled, !shown);
      assert.equal(env.$('demo' + name).required, shown);
    }
    Object.assign(env.$('demoProduct'), {value: 'Bags'});
    Object.assign(env.$('demoCity'), {value: 'Delhi'});
    Object.assign(env.$('demoQuantity'), {value: '500'});
    Object.assign(env.$('demoUnit'), {value: 'pieces'});
    Object.assign(env.$('demoTicketId'), {value: '111'});
    Object.assign(env.$('demoIssue'), {value: 'App login fails'});
    await env.$('demoForm').onsubmit({preventDefault() {}});
    const payload = env.payload;
    assert.equal(payload.activity_type, kind);
    if (kind.startsWith('ticket.')) {
      assert.equal(payload.ticket_id, '111');
      for (const key of ['product', 'city', 'quantity', 'unit']) assert(!(key in payload));
      assert.equal('issue' in payload, kind === 'ticket.opened');
    } else {
      assert.equal(payload.product, 'Bags');
      assert.equal('quantity' in payload, kind === 'enquiry.created');
      assert(!('ticket_id' in payload));
    }
  }
  console.log('Passed all 4 History activity form scenarios.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
