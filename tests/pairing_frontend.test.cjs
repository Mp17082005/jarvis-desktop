'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '../static');
function client(file = 'connect.js') {
  assert.ok(fs.existsSync(path.join(root, file)), `${file} must implement device setup`);
  return require(path.join(root, file));
}

class Element {
  constructor() { this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; this.children = []; this.events = {}; }
  addEventListener(type, fn) { this.events[type] = fn; }
  async fire(type, detail=1) { const event={detail,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};await this.events[type]?.(event);return event; }
  replaceChildren(...nodes) { this.children = nodes; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
}
function harness(responder, origin = 'https://jarvis.example', hash = '') {
  const elements = new Map();
  const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); } };
  const requests = [], navigations = [], timers = [];
  const window = { location: { origin, hash, assign(url) { navigations.push(url); } }, AbortController,
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; }, clearTimeout() {},
    async fetch(url, init) { requests.push({ url, ...init }); const response = await responder(url, init); return { ok: (response.status || 200) < 400, status: response.status || 200, json: async () => response.body }; } };
  return { document, window, requests, navigations, timers, el: id => document.getElementById(id) };
}
const discovery = { installed: true, version: '1', paired: true, device_name: '<img src=x onerror=alert(1)>', target: 'http://127.0.0.1:8767/' };

test('paired detection never redirects on load and explicit connect opens only fixed loopback', async () => {
  const h = harness(async () => ({ body: discovery }));
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, 'http://127.0.0.1:8767/companion/discovery?origin=https%3A%2F%2Fjarvis.example');
  assert.equal(h.requests[0].credentials, 'omit');
  assert.equal(h.requests[0].redirect, 'error');
  assert.equal(h.timers[0].ms, 3000);
  assert.deepEqual(h.navigations, []);
  assert.equal(h.el('open-device').textContent, 'Open Jarvis on ' + discovery.device_name);
  await h.el('connect-device').fire('click');
  assert.deepEqual(h.navigations, [client().LOCAL]);
});

test('discovery rejects malformed booleans and every non-fixed target; errors never fall back to cloud', async () => {
  for (const body of [null, {}, { ...discovery, installed: 'true' }, { ...discovery, installed: false }, { ...discovery, paired: 'false' }, { ...discovery, version: 1 }, { ...discovery, device_name: {} }, ...['https://evil.example/', 'http://localhost:8767/', 'http://127.0.0.1:8767/evil', 'http://127.0.0.1:9999/', 'http://user@127.0.0.1:8767/'].map(target => ({ ...discovery, target }))]) {
    const h = harness(async () => ({ body })); await client().mountBrowser(h.document, h.window).ready;
    await h.el('connect-device').fire('click');
    assert.deepEqual(h.navigations, []); assert.equal(h.el('open-device').hidden, true);
    assert.match(h.el('detection-status').textContent, /not reachable or browser blocked/);
  }
  const blocked = harness(async () => { throw new Error('blocked'); });
  await client().mountBrowser(blocked.document, blocked.window).ready;
  assert.match(blocked.el('detection-status').textContent, /not reachable or browser blocked/);
  assert.equal(blocked.requests.length, 1);
  assert.equal(blocked.el('manual-pair').href, client().LOCAL + 'pair-device.html#origin=https%3A%2F%2Fjarvis.example');
  assert.equal(blocked.el('manual-pair').hidden, false);
  assert.equal(blocked.el('manual-pair').rel, 'noopener noreferrer');
});

test('unpaired companion exposes only explicit top-level local pairing link and origin fragment', async () => {
  const h = harness(async () => ({ body: { ...discovery, paired: false } }));
  await client().mountBrowser(h.document, h.window).ready;
  await h.el('connect-device').fire('click');
  assert.equal(h.el('pair-device').hidden, false);
  assert.equal(h.el('pair-device').href, client().LOCAL + 'pair-device.html#origin=https%3A%2F%2Fjarvis.example');
  assert.equal(h.el('pair-device').rel, 'noopener noreferrer');
  assert.deepEqual(h.navigations, []);
  assert.equal(h.el('open-device').hidden, true);
});

test('pairing page on public origin disables login and never contacts local or cloud APIs', async () => {
  const h = harness(async () => { throw new Error('must not fetch'); }, 'https://jarvis.example', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  await h.el('local-login').fire('submit'); await h.el('approve-site').fire('click');
  assert.equal(h.el('local-token').disabled, true);
  assert.equal(h.el('login-submit').disabled, true);
  assert.equal(h.el('approve-site').disabled, true);
  assert.match(h.el('pair-status').textContent, /local companion/);
  assert.deepEqual(h.requests, []);
});

test('local pairing authenticates with cookie credentials but does not automatically approve', async () => {
  let loggedIn = false;
  const h = harness(async (url, init) => {
    if (url === '/api/login') { assert.deepEqual(JSON.parse(init.body), { token: 'test-only-password' }); loggedIn = true; return { body: { ok: true } }; }
    assert.equal(url, '/api/pairing');
    return loggedIn ? { body: { origins: [], device_name: 'My PC' } } : { status: 401 };
  }, 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  assert.equal(h.el('approve-site').disabled, true);
  assert.equal(h.el('local-login').hidden, false);
  h.el('local-token').value = 'test-only-password'; await h.el('local-login').fire('submit');
  assert.equal(h.el('local-token').value, '');
  assert.equal(h.el('requested-origin').textContent, 'https://jarvis.example');
  assert.equal(h.el('approve-site').disabled, false);
  assert.equal(h.el('local-login').hidden, true);
  assert.deepEqual(h.requests.map(r => r.url), ['/api/pairing', '/api/login', '/api/pairing']);
  assert.ok(h.requests.every(r => r.credentials === 'same-origin' && r.redirect === 'error'));
  assert.deepEqual(h.navigations, []);
});

test('explicit approval and revoke require successful exact-origin readback and never navigate automatically', async () => {
  let origins = [];
  const h = harness(async (url, init) => {
    if (url === '/api/pairing/approve') { assert.deepEqual(JSON.parse(init.body), { origin: 'https://jarvis.example' }); origins = ['https://jarvis.example']; }
    if (url === '/api/pairing/revoke') { assert.deepEqual(JSON.parse(init.body), { origin: 'https://jarvis.example' }); origins = []; }
    return { body: { origins, device_name: '<img src=x>' } };
  }, 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  assert.equal(h.requests.length, 1);
  await h.el('approve-site').fire('click');
  assert.deepEqual(h.requests.map(r => r.url), ['/api/pairing', '/api/pairing/approve', '/api/pairing']);
  assert.match(h.el('pair-status').textContent, /Approval verified/);
  assert.equal(h.el('local-open').hidden, false);
  assert.equal(h.el('local-open').href, client().LOCAL);
  assert.equal(h.el('return-site').href, 'https://jarvis.example/connect.html');
  const row = h.el('approved-origins').children[0];
  assert.equal(row.children[0].textContent, 'https://jarvis.example');
  await row.children[1].fire('click');
  assert.deepEqual(h.requests.slice(-2).map(r => r.url), ['/api/pairing/revoke', '/api/pairing']);
  assert.match(h.el('pair-status').textContent, /Revocation verified/);
  assert.deepEqual(h.navigations, []);
});

test('approval readback mismatch and failed mutation are not reported successful or retried', async () => {
  for (const status of [200, 403, 500]) {
    const h = harness(async url => ({ status: url.endsWith('/approve') ? status : 200, body: { origins: [], device_name: 'PC' } }), 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
    await client('pair-device.js').mountBrowser(h.document, h.window).ready;
    await h.el('approve-site').fire('click');
    assert.doesNotMatch(h.el('pair-status').textContent, /Approval verified/);
    assert.match(h.el('pair-status').textContent, /not verified|failed/);
    assert.equal(h.el('local-open').hidden, true);
    assert.equal(h.requests.filter(r => r.url.endsWith('/approve')).length, 1);
  }
});

test('invalid requested origins and malformed authenticated responses fail closed', async () => {
  for (const hash of ['#origin=javascript%3Aalert(1)', '#origin=https%3A%2F%2Fx.example%2Fpath', '#origin=https%3A%2F%2Fuser%3Apass%40x.example', '#origin=https%3A%2F%2Fx.example&origin=https%3A%2F%2Fy.example', '#origin=https%3A%2F%2Fx.example&token=secret']) {
    const h = harness(async () => ({ body: { origins: [], device_name: 'PC' } }), 'http://127.0.0.1:8767', hash);
    await client('pair-device.js').mountBrowser(h.document, h.window).ready;
    await h.el('approve-site').fire('click');
    assert.equal(h.el('approve-site').disabled, true); assert.equal(h.el('return-site').hidden, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
  }
  for (const body of [null, {}, { origins: 'https://x.example', device_name: 'PC' }, { origins: ['javascript:alert(1)'], device_name: 'PC' }]) {
    const h = harness(async () => ({ body }), 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
    await client('pair-device.js').mountBrowser(h.document, h.window).ready;
    assert.equal(h.el('approve-site').disabled, true);
  }
});

test('setup pages expose accessible controls, fixed download, truthful install steps and root entry panel', () => {
  for (const file of ['connect.html', 'pair-device.html', 'connect.css']) assert.ok(fs.existsSync(path.join(root, file)), file + ' exists');
  const connect = fs.readFileSync(path.join(root, 'connect.html'), 'utf8');
  for (const id of ['connect-device', 'open-device', 'pair-device', 'detection-status', 'site-origin']) assert.match(connect, new RegExp('id="' + id + '"'));
  assert.match(connect, /href="\/downloads\/Jarvis-Windows-Companion.zip"/);
  assert.match(connect, /install_companion\.ps1/); assert.match(connect, /start_companion\.cmd/);
  assert.match(connect, /cannot install/i); assert.match(connect, /browser-only/i);
  const pair = fs.readFileSync(path.join(root, 'pair-device.html'), 'utf8');
  for (const id of ['local-login', 'local-token', 'login-submit', 'approve-site', 'requested-origin', 'approved-origins', 'no-origins', 'refresh-pairing', 'pair-status', 'local-open', 'return-site', 'local-device']) assert.match(pair, new RegExp('id="' + id + '"'));
  assert.match(pair, /id="local-token"[^>]*type="password"[^>]*disabled/);
  assert.match(pair, /only permits detection and navigation/i);
  assert.match(pair, /cloud API key is not copied/i);
  const index = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  assert.match(index, /href="\/connect.html"[^>]*>Use this Windows device/);
  for (const file of ['connect.js', 'pair-device.js']) assert.doesNotMatch(fs.readFileSync(path.join(root, file), 'utf8'), /innerHTML|localStorage|sessionStorage|console\./);
});

test('discovery retries hide stale pairing links and unsupported origins never request', async () => {
  let pass = 0;
  const h = harness(async () => { if (pass++) throw new Error('blocked'); return { body: { ...discovery, paired: false } }; });
  await client().mountBrowser(h.document, h.window).ready;
  await h.el('connect-device').fire('click');
  assert.equal(h.el('pair-device').hidden, true);
  const invalid = harness(async () => { throw new Error('no'); }, 'http://public.example');
  await client().mountBrowser(invalid.document, invalid.window).ready;
  assert.match(invalid.el('detection-status').textContent, /HTTPS/);
  assert.equal(invalid.el('connect-device').disabled, true);
  assert.deepEqual(invalid.requests, []);
});

test('discovery timeout aborts at three seconds without navigation, retries, or other endpoints', async () => {
  const h = harness(async (url, init) => new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted')))));
  const app = client().mountBrowser(h.document, h.window);
  assert.equal(h.timers[0].ms, 3000); h.timers[0].fn(); await app.ready;
  assert.equal(h.requests[0].signal.aborted, true);
  assert.match(h.el('detection-status').textContent, /not reachable or browser blocked/);
  assert.deepEqual(h.navigations, []); assert.equal(h.requests.length, 1);
});

test('pending approval is single-flight; success waits for readback rather than POST acknowledgement', async () => {
  let releasePost, releaseReadback, reads = 0;
  const h = harness(async url => {
    if (url.endsWith('/approve')) return new Promise(resolve => { releasePost = () => resolve({ body: { ok: true } }); });
    if (++reads > 1) return new Promise(resolve => { releaseReadback = () => resolve({ body: { origins: ['https://jarvis.example'], device_name: 'PC' } }); });
    return { body: { origins: [], device_name: 'PC' } };
  }, 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  const pending = h.el('approve-site').fire('click'); await h.el('approve-site').fire('click');
  assert.equal(h.requests.filter(r => r.url.endsWith('/approve')).length, 1);
  releasePost(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.el('local-open').hidden, true); assert.doesNotMatch(h.el('pair-status').textContent, /Approval verified/);
  releaseReadback(); await pending; assert.match(h.el('pair-status').textContent, /Approval verified/);
});

test('failed revoke readback cannot claim revocation and blocks stale retry until refresh', async () => {
  const h = harness(async () => ({ body: { origins: ['https://jarvis.example'], device_name: 'PC' } }), 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  const button = h.el('approved-origins').children[0].children[1];
  await button.fire('click'); await button.fire('click');
  assert.equal(h.requests.filter(r => r.url.endsWith('/revoke')).length, 1);
  assert.match(h.el('pair-status').textContent, /not verified/);
  assert.doesNotMatch(h.el('pair-status').textContent, /Revocation verified/);
  assert.equal(h.el('local-open').hidden, true);
});

test('failed local login clears password and never enables approval', async () => {
  const h = harness(async () => ({ status: 401 }), 'http://127.0.0.1:8767', '#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(h.document, h.window).ready;
  h.el('local-token').value = 'test-secret'; await h.el('local-login').fire('submit');
  assert.equal(h.el('local-token').value, ''); assert.equal(h.el('approve-site').disabled, true);
  assert.equal(h.el('local-login').hidden, false);
  assert.doesNotMatch(h.el('pair-status').textContent, /test-secret/);
  assert.ok(h.requests.every(r => !r.url.includes('test-secret')));
});

test('setup controls explain purposes and installer starts from extracted folder',()=>{
  for(const file of ['connect.html','pair-device.html']) {
    const html=fs.readFileSync(path.join(root,file),'utf8');
    for(const tag of html.matchAll(/<(?:button|a|summary)\b[^>]*>/g)) assert.match(tag[0],/title="[^"]+"/,tag[0]);
  }
  const html=fs.readFileSync(path.join(root,'connect.html'),'utf8');
  assert.match(html,/Install-Jarvis\.cmd/);assert.match(html,/File Explorer/);assert.match(html,/address bar/);assert.match(html,/\.env/);
  assert.match(html,/<details/);assert.doesNotMatch(html,/Downloads\\\\jarvis/);
});
test('dynamic revoke explains its purpose without toggle semantics',async()=>{
  const h=harness(async()=>({body:{origins:['https://jarvis.example'],device_name:'PC'}}),'http://127.0.0.1:8767');
  await client('pair-device.js').mountBrowser(h.document,h.window).ready;
  const button=h.el('approved-origins').children[0].children[1];assert.match(button.title,/Revoke.*https:\/\/jarvis.example/);assert.equal(button['aria-pressed'],undefined);
});
test('doubleclick download and approve ignores trailing event, not a custom doubleclick action',async()=>{
  const h=harness(async()=>({body:discovery}));await client().mountBrowser(h.document,h.window).ready;
  assert.equal((await h.el('download-companion').fire('click',1)).defaultPrevented,false);
  assert.equal((await h.el('download-companion').fire('click',2)).defaultPrevented,true);
  const p=harness(async()=>({body:{origins:[],device_name:'PC'}}),'http://127.0.0.1:8767','#origin=https%3A%2F%2Fjarvis.example');
  await client('pair-device.js').mountBrowser(p.document,p.window).ready;
  await p.el('approve-site').fire('click',2);assert.equal(p.requests.length,1);
});
test('site origin accepts only exact HTTPS or local HTTP origins without credentials or URL components', () => {
  const { safeOrigin } = client();
  for (const origin of ['https://jarvis.example', 'https://jarvis.example:8443', 'http://localhost:8000', 'http://127.0.0.1:8767', 'http://[::1]:8000']) assert.equal(safeOrigin(origin), origin);
  for (const origin of [null, '', 'null', 'http://evil.example', 'javascript:alert(1)', '//evil.example', 'https://user:pass@evil.example', 'https://example.com/path', 'https://example.com/', 'https://example.com?', 'https://example.com#', 'https://example.com?q=x', ' https://example.com', 'https://example.com\n', 'https://example.com\\@evil.example', 'http://127.1:8767', 'http://localhost.evil:8767']) assert.equal(safeOrigin(origin), null, String(origin));
});
