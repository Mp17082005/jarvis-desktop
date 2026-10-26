'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = path.join(__dirname, '../static/app.js');

test('external results only expose explicit HTTPS URLs without embedded credentials', () => {
  const { safeExternalUrl } = client();
  assert.equal(safeExternalUrl('https://www.youtube.com/watch?v=abc'), 'https://www.youtube.com/watch?v=abc');
  for (const url of ['javascript:alert(1)', 'data:text/html,test', 'http://example.com', '//example.com', '/relative', 'https://user:pass@example.com', null]) {
    assert.equal(safeExternalUrl(url), null);
  }
});
test('reminders normalize supported list envelopes and reject malformed dates', () => {
  const { normalizeReminders, dueReminders } = client();
  const input = { reminders: [{ id: 'a', label: '<script>alarm</script>', due_at: '2026-01-01T00:00:00Z' }, { id: 'b', label: 'later', due_at: '2027-01-01T00:00:00Z' }, { id: 'c', label: 'bad', due_at: 'not a date' }] };
  const rows = normalizeReminders(input);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].label, '<script>alarm</script>');
  const seen = new Set();
  assert.equal(dueReminders(rows, seen, Date.parse('2026-06-01')).length, 1);
  assert.equal(dueReminders(rows, seen, Date.parse('2026-06-01')).length, 0);
  assert.equal(normalizeReminders(input.reminders).length, 2);
  assert.deepEqual(normalizeReminders({}), []);
});

const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.attributes = {}; this.hidden = false; this.disabled = false; this.value = ''; this.textContent = ''; this.checked = false; this.scrollHeight = 0; this.scrollTop = 0; this.dataset = {}; this.classList = { add() {}, remove() {}, toggle() {} }; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }
  removeEventListener() {}
  focus() { this.focused = true; }
  remove() { this.removed = true; }
  async fire(type, extra = {}) { for (const fn of this.events[type] || []) await fn({ preventDefault() {}, target: this, ...extra }); }
}
function allText(node) { return [node.textContent, ...node.children.map(allText)].join(' '); }
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
function harness(responder, options = {}) {
  const htmlPath = path.join(__dirname, '../static/legacy.html');
  assert.ok(fs.existsSync(htmlPath), 'HTML entry exists');
  const html = fs.readFileSync(htmlPath, 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const document = Object.assign(new Element('document'), { hidden: false, getElementById: id => elements.get(id), createElement: tag => new Element(tag) });
  const requests = [], storage = new Map();
  const window = {
    document, navigator: {}, isSecureContext: true, AbortController, FormData, Blob,
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 2, clearInterval() {},
    addEventListener() {}, removeEventListener() {},
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    fetch: async (url, init) => { requests.push({ url, ...init }); const reply = await responder(url, init); return { ok: reply.status === undefined || reply.status < 400, status: reply.status || 200, json: async () => reply.body }; },
    ...options
  };
  return { document, window, requests, elements, storage };
}

test('typed console authenticates with cookie credentials, posts once and renders replies as text', async () => {
  let loggedIn = false, release;
  const h = harness(async (url, init) => {
    if (url === '/api/status') return loggedIn ? { body: { llm_configured: true, model: 'test-model', stt: false, tools: ['time'] } } : { status: 401, body: { error: 'Unauthorized' } };
    if (url === '/api/login') { assert.deepEqual(JSON.parse(init.body), { token: 'entered-secret' }); loggedIn = true; return { body: { ok: true } }; }
    if (url === '/api/reminders') return { body: { reminders: [] } };
    if (url === '/api/chat') return new Promise(resolve => { release = () => resolve({ body: { text: '<img src=x onerror=alert(1)>', session_id: 'session-1' } }); });
    throw new Error('Unexpected endpoint: ' + url);
  });
  const app = client().mountBrowser(h.document, h.window);
  await app.ready;
  assert.equal(h.elements.get('login-panel').hidden, false);
  h.elements.get('token').value = 'entered-secret';
  await h.elements.get('login-form').fire('submit');
  assert.equal(h.elements.get('token').value, '');
  assert.equal(h.elements.get('login-panel').hidden, true);
  h.elements.get('command').value = 'hello';
  const pending = h.elements.get('composer').fire('submit');
  await Promise.resolve();
  await h.elements.get('composer').fire('submit');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
  release(); await pending;
  assert.match(allText(h.elements.get('messages')), /<img src=x onerror=alert\(1\)>/);
  assert.equal(h.storage.get('jarvis.session'), 'session-1');
  assert.equal([...h.storage.values()].includes('entered-secret'), false);
  assert.ok(h.requests.every(r => r.credentials === 'same-origin'));
  app.stop();
});

test('confirmation shows exact tool arguments, requires a button and only offers safe result links', async () => {
  const args = { path: '<script>unsafe</script>', enabled: false };
  const h = harness(async (url, init) => {
    if (url === '/api/status') return { body: { tools: [] } };
    if (url === '/api/reminders') return { body: { reminders: [] } };
    if (url === '/api/chat') return { body: { text: 'Review the request.', session_id: 's', confirmation: { id: 'c', tool: 'write_exact', arguments: args } } };
    if (url === '/api/confirm') { assert.deepEqual(JSON.parse(init.body), { id: 'c', approve: true, session_id: 's', language: 'en-IN' }); return { body: { text: 'Approved.', results: [{ url: 'https://example.com/music' }, { url: 'javascript:alert(1)' }] } }; }
  });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await app.sendText('do it');
  const messages = h.elements.get('messages');
  assert.match(allText(messages), /write_exact/);
  assert.ok(allText(messages).includes(JSON.stringify(args, null, 2)));
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
  const approve = descendants(messages).find(n => n.textContent === 'Approve action');
  assert.ok(approve, 'explicit approval button exists');
  await approve.fire('click');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 1);
  await approve.fire('click');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 1);
  const links = descendants(messages).filter(n => n.tagName === 'A');
  assert.equal(links.length, 1); assert.equal(links[0].href, 'https://example.com/music');
  assert.equal(links[0].rel, 'noopener noreferrer');
  app.stop();
});

test('voice recognition stays live for exact stop during synthesis, gates echo and stops on denial', async () => {
  let recognition; const spoken = [];
  class Recognition {
    constructor() { recognition = this; this.starts = 0; this.aborts = 0; }
    start() { this.starts++; this.onstart?.(); }
    abort() { this.aborts++; this.onend?.(); }
    async hear(text) { await this.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: text } }] }); }
  }
  const h = harness(async url => {
    if (url === '/api/status') return { body: { tools: [] } };
    if (url === '/api/reminders') return { body: { reminders: [] } };
    return { body: { text: 'Reply', session_id: 's' } };
  }, { SpeechRecognition: Recognition, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } }, speechSynthesis: { cancel() {}, speak(utterance) { spoken.push(utterance); } } });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  assert.equal(recognition, undefined, 'no permission request before user gesture');
  await h.elements.get('mic-toggle').fire('click');
  assert.equal(recognition.continuous, true);
  await recognition.hear('private words');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 0);
  await recognition.hear('Jarvis');
  assert.equal(spoken[0].text, 'Hi');
  assert.equal(recognition.aborts, 0, 'recognition stays live during speech');
  await recognition.hear('Jarvis duplicate echo');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 0);
  await recognition.hear('stop talking');
  assert.equal(h.elements.get('stop-speaking').hidden, true, 'exact stop cancels playback locally');
  spoken[0].onend();
  await recognition.hear('turn on the lamp');
  assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/chat').body).text, 'turn on the lamp');
  recognition.onerror({ error: 'not-allowed' });
  assert.match(h.elements.get('mic-description').textContent, /denied/i);
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
  app.stop();
});

test('push-to-talk uploads only an explicit recording, releases tracks, and sends transcription', async () => {
  let recorder, stopped = 0;
  class Recorder {
    static isTypeSupported() { return true; }
    constructor() { recorder = this; this.state = 'inactive'; this.mimeType = 'audio/webm'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob(['audio']) }); this.onstop(); }
  }
  const h = harness(async (url, init) => {
    if (url === '/api/status') return { body: { stt: true, tools: [] } };
    if (url === '/api/reminders') return { body: { reminders: [] } };
    if (url === '/api/transcribe') { assert.ok(init.body instanceof FormData); assert.ok(init.body.get('audio')); return { body: { text: 'what time is it' } }; }
    if (url === '/api/chat') return { body: { text: 'A reply.' } };
  }, { MediaRecorder: Recorder, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped++; } }] }) } } });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click');
  assert.equal(recorder, undefined, 'enable does not record or upload');
  assert.equal(h.elements.get('record-toggle').hidden, false);
  await h.elements.get('record-toggle').fire('click');
  assert.equal(recorder.state, 'recording');
  await h.elements.get('record-toggle').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stopped, 1);
  assert.equal(h.requests.filter(r => r.url === '/api/transcribe').length, 1);
  assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/chat').body).text, 'what time is it');
  app.stop();
});

test('overdue reminders notify and speak once per mounted session without server delivery writes', async () => {
  const spoken = [];
  const h = harness(async url => url === '/api/status' ? { body: { tools: [] } } : { body: { reminders: [{ id: 'r', label: 'Take a break', due_at: '2020-01-01T00:00:00Z' }] } }, { speechSynthesis: { cancel() {}, speak(u) { spoken.push(u.text); } }, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } } });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('refresh-reminders').fire('click');
  assert.equal(spoken.filter(text => text.includes('Take a break')).length, 1);
  assert.equal(descendants(h.elements.get('messages')).filter(n => n.textContent === 'Reminder: Take a break').length, 1);
  assert.ok(h.requests.filter(r => r.url === '/api/reminders').every(r => r.method === 'GET'));
  app.stop();
});

test('authentication expiry stops the microphone and handles subsequent chat errors without losing typed input', async () => {
  let recognition, expired = false;
  class Recognition { constructor() { recognition = this; } start() { this.onstart(); } abort() { this.onend?.(); } }
  const h = harness(async url => {
    if (expired) return { status: 401, body: { detail: 'Login required' } };
    return { body: url === '/api/status' ? { tools: [] } : { reminders: [] } };
  }, { SpeechRecognition: Recognition });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click');
  assert.ok(recognition);
  expired = true;
  await h.elements.get('refresh-reminders').fire('click');
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
  assert.equal(h.elements.get('login-panel').hidden, false);
  app.stop();
});

test('missing physical microphone leaves typed chat functional', async () => {
  const h = harness(async url => ({ body: url === '/api/status' ? { stt: { available: true }, tools: [] } : url === '/api/chat' ? { text: 'typed reply' } : { reminders: [] } }), {
    MediaRecorder: class {}, navigator: { mediaDevices: { getUserMedia: async () => { const error = new Error('No device'); error.name = 'NotFoundError'; throw error; } } }
  });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click'); await h.elements.get('record-toggle').fire('click');
  assert.match(h.elements.get('mic-description').textContent, /No microphone/);
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
  await app.sendText('hello'); assert.match(allText(h.elements.get('messages')), /typed reply/);
  app.stop();
});

test('typed submissions are blocked during audio capture and hidden tabs discard recordings', async () => {
  let recorder, released = 0;
  class Recorder {
    constructor() { recorder = this; this.state = 'inactive'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob(['audio']) }); this.onstop(); }
  }
  const h = harness(async url => ({ body: url === '/api/status' ? { stt: true } : { reminders: [] } }), { MediaRecorder: Recorder, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { released++; } }] }) } } });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click'); await h.elements.get('record-toggle').fire('click');
  await app.sendText('do not send while recording');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 0);
  h.document.hidden = true; await h.document.fire('visibilitychange');
  assert.equal(released, 1); assert.equal(recorder.state, 'inactive');
  assert.equal(h.requests.filter(r => r.url === '/api/transcribe').length, 0);
  assert.match(h.elements.get('mic-description').textContent, /suspended/);
  app.stop();
});

test('push-to-talk speech playback returns to push-to-talk, never claims wake-word listening', async () => {
  let utterance;
  const h = harness(async url => ({ body: url === '/api/status' ? { stt: true } : url === '/api/chat' ? { text: 'hello' } : { reminders: [] } }), { MediaRecorder: class {}, navigator: { mediaDevices: { getUserMedia() {} } }, speechSynthesis: { cancel() {}, speak(value) { utterance = value; } }, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } } });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click'); await app.sendText('hello');
  utterance.onend();
  assert.equal(h.elements.get('mic-state').textContent, 'Push-to-talk ready');
  app.stop();
});

test('Stop speaking clears playback state with microphone off', async () => {
  const h = harness(async url => ({ body: url === '/api/chat' ? { text: 'hello' } : url === '/api/status' ? {} : { reminders: [] } }), { speechSynthesis: { cancel() {}, speak() {} }, SpeechSynthesisUtterance: class {} });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await app.sendText('hello'); assert.equal(h.elements.get('mic-state').textContent, 'Speaking');
  await h.elements.get('stop-speaking').fire('click');
  assert.equal(h.elements.get('mic-state').textContent, 'Microphone off');
  app.stop();
});

test('console assets contain accessible local-only structure and responsive safeguards', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/legacy.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
  assert.match(html, /src="\/app.js"/);
  assert.match(html, /href="\/app.css"/);
  assert.match(html, /type="password"/);
  assert.match(html, /role="log"/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@media/);
  assert.doesNotMatch(html, /https?:\/\/|onclick=/);
  assert.doesNotMatch(fs.readFileSync(source, 'utf8'), /innerHTML|localStorage|location\.search/);
});

test('exact local controls parse without mistaking prose, and require wake or PTT except stop', () => {
  const { localVoiceControl } = client();
  for (const phrase of ['stop', 'Stop speaking!', 'stop talking', 'quiet', 'Jarvis, stop.']) assert.equal(localVoiceControl(phrase)?.type, 'stop');
  for (const phrase of ['please stop the music', 'do not stop', 'Jarvis stop the timer', 'we discussed Jarvis increase volume', 'increase volume']) assert.equal(localVoiceControl(phrase), null);
  assert.deepEqual(localVoiceControl('Jarvis increase volume'), { type: 'volume', delta: 10 });
  assert.deepEqual(localVoiceControl('decrease volume', { awake: true }), { type: 'volume', delta: -10 });
  assert.deepEqual(localVoiceControl('set volume to 42 percent', { explicit: true }), { type: 'volume', value: 42 });
  assert.deepEqual(localVoiceControl('Jarvis mute'), { type: 'mute', value: true });
  assert.deepEqual(localVoiceControl('Jarvis unmute'), { type: 'mute', value: false });
  assert.deepEqual(localVoiceControl('Jarvis talk mode'), { type: 'mode', value: true });
  assert.deepEqual(localVoiceControl('Jarvis text mode'), { type: 'mode', value: false });
});

async function voiceHarness(responder) {
  let recognition; const spoken = [], timers = new Map(); let timerId = 0, cancelled = 0;
  class Recognition {
    constructor() { recognition = this; this.aborts = 0; this.starts = 0; }
    start() { this.starts++; this.onstart?.(); }
    abort() { this.aborts++; this.onend?.(); }
    hear(text, final = true) { return this.onresult({ resultIndex: 0, results: [{ isFinal: final, 0: { transcript: text } }] }); }
  }
  const h = harness(async (url, init) => {
    if (url === '/api/status') return { body: { tools: [] } };
    if (url === '/api/reminders') return { body: { reminders: [] } };
    return responder ? responder(url, init) : { body: { text: 'A spoken reply.' } };
  }, { SpeechRecognition: Recognition, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    speechSynthesis: { cancel() { cancelled++; }, speak(u) { spoken.push(u); } },
    setTimeout(fn, ms) { timers.set(++timerId, { fn, ms }); return timerId; }, clearTimeout(id) { timers.delete(id); }
  });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click');
  return { ...h, app, recognition, spoken, timers, cancelled: () => cancelled };
}

test('stop during busy keeps recognition running and suppresses only pending response autoplay', async () => {
  let release;
  const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Result remains visible.' } }); }));
  const pending = h.app.sendText('work');
  assert.equal(h.recognition.aborts, 0);
  await h.recognition.hear('Jarvis send another request');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
  h.recognition.onend();
  const restart = [...h.timers.values()].find(t => t.ms === 500); assert.ok(restart); restart.fn();
  assert.equal(h.recognition.starts, 2);
  await h.recognition.hear('stop');
  release(); await pending;
  assert.equal(h.spoken.length, 0, 'late answer must not restart stopped speech');
  assert.match(allText(h.elements.get('messages')), /Result remains visible/);
  const next = h.app.sendText('next explicit request'); release(); await next;
  assert.equal(h.spoken.length, 1, 'next request can speak');
  h.app.stop();
});

test('voice volume adjusts locally and the visible slider applies clamped volume to replies', async () => {
  const h = await voiceHarness();
  assert.ok(h.elements.has('voice-volume'), 'visible volume range exists');
  assert.equal(h.elements.get('voice-volume').value, '80');
  assert.equal(h.elements.get('volume-value').textContent, '80%');
  await h.recognition.hear('Jarvis increase volume');
  assert.equal(h.elements.get('volume-value').textContent, '90%');
  await h.recognition.hear('Jarvis decrease volume');
  assert.equal(h.elements.get('volume-value').textContent, '80%');
  await h.recognition.hear('Jarvis set volume to 500 percent');
  assert.equal(h.elements.get('voice-volume').value, '100');
  await h.recognition.hear('Jarvis set volume to -50 percent');
  assert.equal(h.elements.get('voice-volume').value, '0');
  h.elements.get('voice-volume').value = '45'; await h.elements.get('voice-volume').fire('input');
  assert.equal(h.elements.get('volume-value').textContent, '45%');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 0);
  await h.app.sendText('hello');
  assert.equal(h.spoken.at(-1).volume, 0.45);
  h.app.stop();
});

test('active volume change resumes from last word boundary and ignores stale speech events', async () => {
  const h = await voiceHarness();
  await h.app.sendText('hello');
  const old = h.spoken.at(-1);
  old.onboundary?.({ charIndex: 9 });
  await h.recognition.hear('Jarvis increase volume');
  assert.equal(h.spoken.length, 2);
  assert.equal(h.spoken.at(-1).text, 'reply.');
  assert.equal(h.spoken.at(-1).volume, 0.9);
  old.onend(); old.onerror();
  assert.equal(h.elements.get('stop-speaking').hidden, false);
  assert.doesNotMatch(h.elements.get('request-status').textContent, /unavailable/);
  await h.recognition.hear('stop', false);
  assert.equal(h.elements.get('stop-speaking').hidden, false, 'interim stop ignored');
  await h.recognition.hear('please stop explaining');
  assert.equal(h.elements.get('stop-speaking').hidden, false, 'prose ignored during playback');
  await h.recognition.hear('stop');
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
  h.app.stop();
});

test('mute cancels playback without disabling talk mode and unmute restores last nonzero volume', async () => {
  const h = await voiceHarness();
  await h.app.sendText('hello');
  await h.recognition.hear('Jarvis mute');
  assert.equal(h.elements.get('voice-volume').value, '0');
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  assert.equal(h.elements.get('speak-replies').checked, true);
  await h.app.sendText('silent answer');
  assert.equal(h.spoken.length, 1, 'muted replies stay text instead of silent speech blocking commands');
  await h.recognition.hear('Jarvis unmute');
  assert.equal(h.elements.get('voice-volume').value, '80');
  await h.recognition.hear('Jarvis set volume to 35 percent');
  await h.recognition.hear('Jarvis mute'); await h.recognition.hear('Jarvis unmute');
  assert.equal(h.elements.get('voice-volume').value, '35');
  await h.app.sendText('audible answer');
  assert.equal(h.spoken.at(-1).volume, 0.35);
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 3);
  h.app.stop();
});

test('voice talk and text mode switches are local and text mode keeps mic but cancels pending autoplay', async () => {
  let release;
  const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Answer.' } }); }));
  const pending = h.app.sendText('question');
  await h.recognition.hear('Jarvis text mode');
  assert.equal(h.elements.get('speak-replies').checked, false);
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'true');
  await h.recognition.hear('Jarvis talk mode');
  assert.equal(h.elements.get('speak-replies').checked, true);
  release(); await pending;
  assert.equal(h.spoken.length, 0, 'reenabling talk must not revive a stopped pending reply');
  const next = h.app.sendText('next'); release(); await next;
  assert.equal(h.spoken.length, 1);
  await h.recognition.hear('Jarvis text mode');
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  assert.equal(h.recognition.aborts, 0);
  h.elements.get('speak-replies').checked = true; await h.elements.get('speak-replies').fire('change');
  const third = h.app.sendText('third'); release(); await third;
  h.elements.get('speak-replies').checked = false; await h.elements.get('speak-replies').fire('change');
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 3);
  assert.match(fs.readFileSync(path.join(__dirname, '../static/legacy.html'), 'utf8'), /Talk mode · speak replies/);
  h.app.stop();
});

test('Test voice is an explicit local preview even in text mode without enabling the mic', async () => {
  const spoken = [];
  const h = harness(async url => ({ body: url === '/api/status' ? {} : { reminders: [] } }), { speechSynthesis: { cancel() {}, speak(u) { spoken.push(u); } }, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } } });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  assert.ok(h.elements.has('test-voice'));
  assert.equal(spoken.length, 0);
  await h.elements.get('test-voice').fire('click');
  assert.equal(spoken.length, 1);
  assert.equal(spoken[0].volume, 0.8);
  assert.match(spoken[0].text, /hello/i);
  assert.equal(h.elements.get('speak-replies').checked, false);
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  app.stop();
});

test('push-to-talk transcripts use local controls without forwarding them to chat', async () => {
  let recorder, transcript = 'text mode';
  class Recorder {
    constructor() { recorder = this; this.state = 'inactive'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob(['audio']) }); this.onstop(); }
  }
  const h = harness(async url => ({ body: url === '/api/status' ? { stt: true } : url === '/api/transcribe' ? { text: transcript } : { reminders: [] } }), {
    MediaRecorder: Recorder, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } },
    SpeechSynthesisUtterance: class {}, speechSynthesis: { cancel() {}, speak() {} }
  });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('mic-toggle').fire('click');
  for (const phrase of ['text mode', 'increase volume', 'stop']) {
    transcript = phrase;
    await h.elements.get('record-toggle').fire('click');
    await h.elements.get('record-toggle').fire('click');
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(h.elements.get('speak-replies').checked, false);
  assert.equal(h.elements.get('voice-volume').value, '90');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 0);
  assert.equal(h.requests.filter(r => r.url === '/api/transcribe').length, 3);
  app.stop();
});

test('unsupported or failed synthesis is visibly reported and cannot falsely enable talk mode', async () => {
  const h = await voiceHarness();
  delete h.window.SpeechSynthesisUtterance;
  await h.recognition.hear('Jarvis talk mode');
  assert.equal(h.elements.get('speak-replies').checked, false);
  assert.match(h.elements.get('voice-output-status').textContent, /unavailable/i);
  await h.elements.get('test-voice').fire('click');
  assert.match(h.elements.get('voice-output-status').textContent, /unavailable/i);
  h.window.SpeechSynthesisUtterance = class {};
  h.window.speechSynthesis.speak = () => { throw new Error('blocked'); };
  await h.elements.get('test-voice').fire('click');
  assert.match(h.elements.get('voice-output-status').textContent, /unavailable/i);
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  h.app.stop();
  const noTts = harness(async url => ({ body: url === '/api/status' ? {} : { reminders: [] } }));
  const app = client().mountBrowser(noTts.document, noTts.window); await app.ready;
  assert.match(noTts.elements.get('voice-output-status').textContent, /unavailable/i);
  app.stop();
});

test('voice help explains setup, exact commands and limitations with responsive volume controls', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/legacy.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
  for (const phrase of ['Help', 'Enable mic', 'Allow', 'Jarvis increase volume', 'Jarvis decrease volume', 'Jarvis set volume to 50 percent', 'Jarvis mute', 'Jarvis unmute', 'Jarvis talk mode', 'Jarvis text mode', 'stop speaking', 'stop talking', 'quiet', 'headphones', 'system volume', 'final', 'Tap Stop speaking']) assert.ok(html.includes(phrase), phrase);
  assert.match(css, /\.voice-settings\s*\{[^}]*flex-wrap:wrap/);
  assert.match(css, /#voice-volume\s*\{[^}]*min-width:0/);
});

test('sleeping ordinary prose mentioning Jarvis is not a prefixed request', () => {
  const { wakeDecision } = client();
  assert.equal(wakeDecision('we discussed Jarvis increase volume', 0, 1000).type, 'ignore');
  assert.deepEqual(wakeDecision('Jarvis, explain why trains stop', false), { type: 'command', text: 'explain why trains stop', conversationActive: true });
});

test('mic status remains controls-only through recognition restarts and resets after stop', async () => {
  const h = await voiceHarness();
  await h.app.sendText('hello');
  h.recognition.onstart();
  assert.match(h.elements.get('mic-description').textContent, /local.*controls/i);
  await h.recognition.hear('stop');
  assert.equal(h.elements.get('mic-state').textContent, 'Waiting for “Jarvis”');
  assert.doesNotMatch(h.elements.get('mic-description').textContent, /paused/i);
  h.app.stop();
});

test('Stop mic and hidden-tab cleanup suppress late pending autoplay without automatic reenable', async () => {
  for (const action of ['stop-mic', 'hidden']) {
    let release;
    const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Late text' } }); }));
    const pending = h.app.sendText('work');
    if (action === 'stop-mic') await h.elements.get('mic-toggle').fire('click');
    else { h.document.hidden = true; await h.document.fire('visibilitychange'); h.document.hidden = false; await h.document.fire('visibilitychange'); }
    release(); await pending;
    assert.equal(h.spoken.length, 0, action);
    assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
    assert.match(allText(h.elements.get('messages')), /Late text/);
    const count = h.requests.length; await h.recognition.hear('Jarvis do something');
    assert.equal(h.requests.length, count);
    h.app.stop();
  }
});

test('Stop speaking button is available while waiting and suppresses late response voice', async () => {
  let release;
  const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Late answer.' } }); }));
  const pending = h.app.sendText('question');
  assert.equal(h.elements.get('stop-speaking').hidden, false);
  await h.elements.get('stop-speaking').fire('click');
  release(); await pending;
  assert.equal(h.spoken.length, 0);
  assert.equal(h.elements.get('stop-speaking').hidden, true);
  h.app.stop();
});

test('volume change during Test voice preserves text mode and resumes only remaining preview', async () => {
  const h = await voiceHarness();
  await h.recognition.hear('Jarvis text mode');
  await h.elements.get('test-voice').fire('click');
  h.spoken.at(-1).onboundary({ charIndex: 7 });
  h.elements.get('voice-volume').value = '60'; await h.elements.get('voice-volume').fire('input');
  assert.equal(h.spoken.length, 2);
  assert.equal(h.spoken.at(-1).text, 'Your voice preview is ready.');
  assert.equal(h.spoken.at(-1).volume, 0.6);
  assert.equal(h.elements.get('speak-replies').checked, false);
  h.app.stop();
});

test('voice safeguards cover awake controls, no-position fallback, and button-only approval', async () => {
  const h = await voiceHarness(() => ({ body: { text: 'Review this action.', confirmation: { id: 'approval-1', tool: 'write', arguments: { path: 'x' } } } }));
  await h.recognition.hear('increase volume');
  assert.equal(h.elements.get('voice-volume').value, '80', 'sleeping bare control ignored');
  await h.recognition.hear('Jarvis');
  await h.recognition.hear('increase volume');
  assert.equal(h.elements.get('voice-volume').value, '90', 'awake bare control accepted');
  assert.equal(h.spoken.length, 1, 'no boundary means do not replay from start');
  assert.match(h.elements.get('request-status').textContent, /did not report word position/);
  await h.app.sendText('request an action');
  await h.recognition.hear('Jarvis approve');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1, 'echo cannot submit another request');
  assert.ok(descendants(h.elements.get('messages')).find(n => n.textContent === 'Approve action'));
  h.app.stop();
});

test('language selection updates recognition, loaded voices, speech and request hints', async () => {
  const h = await voiceHarness();
  assert.equal(h.recognition.lang, 'en-IN');
  const select = h.elements.get('voice-language');
  assert.ok(select);
  select.value = 'hi-IN'; await select.fire('change');
  assert.equal(h.recognition.lang, 'hi-IN');
  h.window.speechSynthesis.getVoices = () => [{ lang: 'hi-IN', name: 'Hindi voice' }];
  h.window.speechSynthesis.onvoiceschanged?.();
  await h.app.sendText('नमस्ते');
  assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/chat').body).language, 'hi-IN');
  assert.equal(h.spoken.at(-1).lang, 'hi-IN');
  assert.equal(h.spoken.at(-1).voice.name, 'Hindi voice');
  await h.recognition.hear('stop');
  await h.recognition.hear('Jarvis change language to Telugu');
  assert.equal(select.value, 'te-IN');
  assert.equal(h.recognition.lang, 'te-IN');
  await h.recognition.hear('జార్విస్ భాష హిందీ');
  assert.equal(select.value, 'hi-IN');
  select.value = 'custom'; await select.fire('change');
  const custom = h.elements.get('voice-language-custom');
  custom.value = 'pt-BR'; await custom.fire('change');
  await h.elements.get('test-voice').fire('click');
  assert.equal(h.spoken.at(-1).lang, 'pt-BR');
  custom.value = 'not a tag'; await custom.fire('change');
  assert.equal(h.recognition.lang, 'pt-BR', 'invalid tag does not replace valid language');
  h.app.stop();
});

test('localized exact stops and language commands avoid fuzzy interpretation', () => {
  const { localVoiceControl } = client();
  for (const text of ['रुको', 'बोलना बंद करो', 'ఆపు', 'మాట్లాడటం ఆపు']) assert.equal(localVoiceControl(text)?.type, 'stop');
  for (const text of ['जार्विस सुनना बंद करो', 'జార్విస్ వినడం ఆపు']) assert.equal(localVoiceControl(text)?.type, 'end');
  assert.deepEqual(localVoiceControl('जार्विस भाषा हिंदी'), { type: 'language', value: 'hi-IN' });
  assert.equal(localVoiceControl('we should change language to Hindi', { awake: true }), null);
});

test('voice approval waits for summary end, binds exact current phrase and submits only once', async () => {
  let release;
  const confirmation = { id: 'c1', tool: 'send', arguments: { to: 'friend@example.com', body: 'Hello' }, spoken_summary: 'Send Hello to friend@example.com.', voice_phrase: 'approve 4821' };
  const h = await voiceHarness((url, init) => url === '/api/chat' ? { body: { text: 'Permission required.', session_id: 's1', confirmation } } : new Promise(resolve => { release = () => resolve({ body: { text: 'Sent.' } }); }));
  await h.recognition.hear('Jarvis send my message');
  assert.match(h.spoken.at(-1).text, /Send Hello to friend@example.com/);
  assert.match(h.spoken.at(-1).text, /approve 4821/);
  await h.recognition.hear('approve 4821');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0, 'own summary echo ignored');
  h.spoken.at(-1).onend();
  assert.match(h.elements.get('request-status').textContent, /approve 4821/);
  for (const text of ['yes', 'हाँ', 'అవును', 'approve 1111', 'please approve 4821', 'approve 4821 now']) await h.recognition.hear(text);
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1, 'invalid approvals are not new chat');
  const pending = h.recognition.hear('approve 4821');
  await h.recognition.hear('approve 4821');
  const calls = h.requests.filter(r => r.url === '/api/confirm');
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].body), { id: 'c1', approve: true, session_id: 's1', language: 'en-IN', voice_phrase: 'approve 4821' });
  release(); await pending; h.spoken.at(-1).onend();
  await h.recognition.hear('approve 4821');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 1);
  h.app.stop();
});

test('stopping summary never arms approval and stale phrase cannot approve replacement', async () => {
  let count = 0;
  const h = await voiceHarness(url => url === '/api/chat' ? { body: { confirmation: { id: 'c' + (++count), spoken_summary: 'Write exact file contents.', voice_phrase: count === 1 ? 'approve 1234' : 'approve 5678', tool: 'write', arguments: { path: 'a', content: 'b' } } } } : { body: { text: 'Done' } });
  await h.recognition.hear('Jarvis write a file');
  const interrupted = h.spoken.at(-1);
  await h.recognition.hear('stop'); interrupted.onend();
  await h.recognition.hear('approve 1234');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0, 'full summary must finish');
  await h.app.sendText('replace that action'); h.spoken.at(-1).onend();
  await h.recognition.hear('approve 1234');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
  await h.recognition.hear('approve 5678');
  assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/confirm').body).id, 'c2');
  h.app.stop();
});

test('localized denials cancel only current pending action after playback, legacy confirmations stay button-only', async () => {
  for (const denial of ['cancel action', 'no', 'नहीं', 'వద్దు']) {
    const h = await voiceHarness(url => url === '/api/chat' ? { body: { confirmation: { id: 'c', spoken_summary: 'Send this message.', voice_phrase: 'approve 4821' } } } : { body: { text: 'Cancelled' } });
    await h.recognition.hear('Jarvis send');
    await h.recognition.hear(denial);
    assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
    await h.recognition.hear('stop'); await h.recognition.hear(denial);
    assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/confirm').body).approve, false);
    h.app.stop();
  }
  const h = await voiceHarness(() => ({ body: { text: 'Review', confirmation: { id: 'old', tool: 'write', arguments: {} } } }));
  await h.recognition.hear('Jarvis write'); h.spoken.at(-1).onend();
  await h.recognition.hear('yes'); await h.recognition.hear('approve');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
  h.app.stop();
});

test('failed summary speech cannot enable voice approval; public answers speak prose not result URLs', async () => {
  const h = await voiceHarness(() => ({ body: { confirmation: { id: 'c', spoken_summary: 'Delete the file.', voice_phrase: 'approve 4821' } } }));
  await h.recognition.hear('Jarvis delete'); h.spoken.at(-1).onerror();
  await h.recognition.hear('approve 4821');
  assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
  h.app.stop();
  const publicReply = await voiceHarness(() => ({ body: { text: 'The answer is available in this article: https://example.com/source', results: [{ url: 'https://example.com/source' }] } }));
  await publicReply.recognition.hear('Jarvis research this');
  assert.match(publicReply.spoken.at(-1).text, /The answer/);
  assert.doesNotMatch(publicReply.spoken.at(-1).text, /https:\/\//);
  assert.match(allText(publicReply.elements.get('messages')), /https:\/\/example.com/);
  publicReply.app.stop();
});

test('help explains persistent conversation, scoped approvals and browser language limitations', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/legacy.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
  for (const text of ['stop Jarvis', 'Jarvis stop listening', 'spoken summary', 'exact phrase', 'हाँ', 'అవును', 'नहीं', 'వద్దు', 'not universal', 'Whisper']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /within 20 seconds|voice is not approval|awake window/);
  assert.match(css, /#voice-language/);
});

test('speech errors and watchdog completion never arm approval via stale end events', async () => {
  for (const reason of ['error', 'timeout']) {
    const h = await voiceHarness(() => ({ body: { confirmation: { id: 'c', spoken_summary: 'Send exact message.', voice_phrase: 'approve 1234' } } }));
    await h.recognition.hear('Jarvis send');
    const utterance = h.spoken.at(-1);
    if (reason === 'error') utterance.onerror();
    else [...h.timers.values()].find(t => t.ms === 120000).fn();
    utterance.onend(); await h.recognition.hear('approve 1234');
    assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0, reason);
    h.app.stop();
  }
});

test('changing language waits for asynchronous recognition shutdown before restarting', async () => {
  const h = await voiceHarness();
  let shuttingDown = false;
  const start = h.recognition.start.bind(h.recognition);
  h.recognition.abort = () => { shuttingDown = true; };
  h.recognition.start = () => { if (shuttingDown) throw new Error('recognition already started'); start(); };
  h.elements.get('voice-language').value = 'hi-IN'; await h.elements.get('voice-language').fire('change');
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'true');
  shuttingDown = false; h.recognition.onend();
  [...h.timers.values()].find(t => t.ms === 500).fn();
  assert.equal(h.recognition.starts, 2);
  assert.equal(h.recognition.lang, 'hi-IN');
  h.app.stop();
});

test('spokenText removes emoji clusters and markdown without corrupting prices or Indic text', () => {
  const { spokenText } = client();
  assert.equal(typeof spokenText, 'function');
  assert.equal(spokenText('## **आज का भाव** 👨‍👩‍👧‍👦 🇮🇳 👍🏽\n- ₹14,608 22K हिंदी\n* [Source](https://example.com/gold) 1️⃣ #️⃣ *️⃣\n```text\nनमस्ते `दोस्त`\n```\nhttps://example.com/raw'), 'आज का भाव ₹14,608 22K हिंदी Source नमस्ते दोस्त');
  assert.equal(spokenText('123 0 9 ₹14,608 22K हिन्दी क्‍ष 1.5 -2 50%'), '123 0 9 ₹14,608 22K हिन्दी क्‍ष 1.5 -2 50%');
});

test('reply speech is clean while every exact approval summary remains verbatim', async () => {
  let confirmation;
  const h = await voiceHarness(() => ({ body: { text: '**Gold** 🇮🇳 ₹14,608 22K [Source](https://example.com)', ...(confirmation ? { confirmation } : {}) } }));
  await h.app.sendText('gold');
  assert.equal(h.spoken.at(-1).text, 'Gold ₹14,608 22K Source');
  for (const voice_phrase of ['approve 1234', undefined]) {
    confirmation = { id: 'c', spoken_summary: 'Send **hello** 👨‍👩‍👧‍👦 to https://example.com with ₹14,608.', voice_phrase };
    await h.app.sendText('send');
    assert.ok(h.spoken.at(-1).text.startsWith(confirmation.spoken_summary), 'including button-only summary');
  }
  assert.match(allText(h.elements.get('messages')), /🇮🇳/);
  h.app.stop();
});

test('spokenText strips unpaired asterisks rather than saying asterisk', () => {
  const { spokenText } = client();
  assert.equal(spokenText('* * price **₹14,608**'), 'price ₹14,608');
  assert.equal(spokenText('Price * ₹14,608 ** 22K *'), 'Price ₹14,608 22K');
});

test('anchored wake greetings and practical aliases answer once locally without chat', async () => {
  const { wakeDecision } = client();
  for (const phrase of ['Hey Jarvis', 'Hi Jarves', 'Hello Jervis', 'Jarvis hi', 'Jarvis hello!', 'Jarvis hey', 'Hi, जार्विस', 'hello జార్విస్', 'hey ஜார்விஸ்']) {
    assert.equal(wakeDecision(phrase).type, 'wake', phrase);
    const h = await voiceHarness();
    await h.recognition.hear(phrase); await h.recognition.hear(phrase);
    assert.equal(h.spoken.length, 1, phrase);
    h.spoken[0].onend(); await h.recognition.hear(phrase);
    assert.equal(h.spoken.length, 1, 'duplicate final does not repeat greeting');
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
    await h.recognition.hear('what time is it');
    assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
    h.app.stop();
  }
  for (const text of ['they said hey Jarvis', 'Hey Jarvison', 'hello service']) assert.equal(wakeDecision(text).type, 'ignore');
  assert.equal(wakeDecision('Hey Jervis, tell me the time').text, 'tell me the time');
});

test('Start conversation is an explicit microphone gesture and followups need no wake', async () => {
  let recognition; const spoken = [];
  class Recognition {
    constructor() { recognition = this; }
    start() { this.onstart(); }
    abort() { this.onend?.(); }
    hear(text) { return this.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: text } }] }); }
  }
  const h = harness(async url => ({ body: url === '/api/status' ? {} : url === '/api/chat' ? { text: 'Answer' } : { reminders: [] } }), {
    SpeechRecognition: Recognition, SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } }, speechSynthesis: { cancel() {}, speak(u) { spoken.push(u); } }
  });
  h.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  assert.ok(h.elements.has('start-conversation'), 'prominent start button exists');
  assert.equal(recognition, undefined);
  await h.elements.get('start-conversation').fire('click');
  assert.ok(recognition);
  assert.equal(spoken.length, 1);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  await h.elements.get('start-conversation').fire('click');
  assert.equal(spoken.length, 1, 'double click never repeats greeting');
  spoken[0].onend();
  await recognition.hear('tell me the time');
  assert.equal(JSON.parse(h.requests.find(r => r.url === '/api/chat').body).text, 'tell me the time');
  await recognition.hear('stop Jarvis');
  await recognition.hear('private words');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
  await h.elements.get('mic-toggle').fire('click');
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'false');
  app.stop();
});

test('interim and sleeping transcripts display locally with no actions or transmission', async () => {
  const h = await voiceHarness();
  assert.ok(h.elements.has('heard-transcript'), 'last heard display exists');
  assert.equal(h.recognition.interimResults, true);
  const heard = h.elements.get('heard-transcript');
  await h.recognition.hear('private sleeping words', false);
  assert.equal(heard.textContent, 'private sleeping words');
  assert.equal(heard.dataset.final, 'false');
  await h.recognition.hear('Jarvis hi', false);
  assert.equal(h.spoken.length, 0);
  await h.recognition.hear('private final words');
  assert.equal(heard.textContent, 'private final words');
  assert.equal(heard.dataset.final, 'true');
  assert.match(h.elements.get('request-status').textContent, /wake|Start conversation/i);
  await h.recognition.hear('Jarvis increase volume', false);
  assert.equal(h.elements.get('voice-volume').value, '80');
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  h.app.stop();
});

test('mic status never claims listening before start or between restarts and start resets after stop', async () => {
  let recognition;
  class Recognition { constructor() { recognition = this; } start() {} abort() { this.onend?.(); } }
  const h = harness(async url => ({ body: url === '/api/status' ? {} : { reminders: [] } }), { SpeechRecognition: Recognition });
  const app = client().mountBrowser(h.document, h.window); await app.ready;
  await h.elements.get('start-conversation').fire('click');
  await h.elements.get('refresh-status').fire('click');
  assert.match(h.elements.get('mic-state').textContent, /Starting microphone/);
  assert.doesNotMatch(h.elements.get('mic-description').textContent, /listening/i);
  recognition.onstart();
  assert.match(h.elements.get('mic-state').textContent, /Active/);
  recognition.onend();
  assert.match(h.elements.get('mic-state').textContent, /Starting microphone/);
  await h.elements.get('mic-toggle').fire('click');
  assert.equal(h.elements.get('start-conversation').disabled, false);
  app.stop();
});

test('research chat waits four minutes while other APIs retain ninety-second timeout and stop suppression', async () => {
  let release;
  const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Research complete' } }); }));
  const pending = h.app.sendText('research');
  assert.ok([...h.timers.values()].some(t => t.ms === 240000), 'chat gets research time budget');
  assert.equal([...h.timers.values()].some(t => t.ms === 90000), false);
  assert.equal(h.elements.get('mic-state').textContent, 'Thinking');
  await h.recognition.hear('stop'); release(); await pending;
  assert.equal(h.spoken.length, 0);
  assert.match(allText(h.elements.get('messages')), /Research complete/);
  const recorded = [];
  h.window.setTimeout = (fn, ms) => { recorded.push(ms); return 1; };
  await h.elements.get('refresh-status').fire('click');
  assert.ok(recorded.includes(90000)); assert.ok(!recorded.includes(240000));
  h.app.stop();
});

test('new ordinary speech retires only legacy public research prompts without approving them', async () => {
  for (const tool of ['public_research', 'send_email']) {
    let chats = 0;
    const h = await voiceHarness(() => ({ body: ++chats === 1 ? { confirmation: { id: 'legacy', tool, spoken_summary: 'Research old query.', voice_phrase: 'approve 4821' } } : { text: 'Fresh result' } }));
    await h.recognition.hear('Jarvis research');
    const oldApprove = descendants(h.elements.get('messages')).find(n => n.textContent === 'Approve action');
    h.spoken.at(-1).onend();
    for (const text of ['approve 9999', 'Hey Jarvis approve 4821', 'yes']) await h.recognition.hear(text);
    assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1, 'approval-like speech never becomes fresh research');
    await h.recognition.hear('what is the gold price today');
    assert.equal(h.requests.filter(r => r.url === '/api/chat').length, tool === 'public_research' ? 2 : 1);
    if (tool === 'public_research') {
      assert.equal(JSON.parse(h.requests.filter(r => r.url === '/api/chat')[1].body).text, 'what is the gold price today');
      assert.equal(oldApprove.disabled, true);
      await oldApprove.fire('click');
      await h.recognition.hear('approve 4821');
    }
    assert.equal(h.requests.filter(r => r.url === '/api/confirm').length, 0);
    h.app.stop();
  }
});

test('simple layout exposes start instructions and tucks technical settings into closed details', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/legacy.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
  assert.match(html, /Start conversation → speak → Stop Jarvis/);
  assert.match(html, /<details[^>]*id="system-details"[^>]*>/);
  assert.match(html, /<details[^>]*id="voice-options"[^>]*>/);
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/);
  assert.match(css, /#mic-state\s*\{[^}]*font-size/);
  assert.match(css, /\.heard-line\s*\{[^}]*overflow-wrap:anywhere/);
});

test('Stop mic during greeting leaves Start usable and speech never claims pending mic is listening', async () => {
  const h = await voiceHarness();
  await h.elements.get('start-conversation').fire('click');
  await h.elements.get('mic-toggle').fire('click');
  assert.equal(h.elements.get('start-conversation').disabled, false);
  await h.elements.get('start-conversation').fire('click');
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'true');
  h.app.stop();
  let recognition;
  const pending = harness(async url => ({ body: url === '/api/status' ? {} : url === '/api/chat' ? { text: 'Answer' } : { reminders: [] } }), {
    SpeechRecognition: class { constructor() { recognition = this; } start() {} abort() {} },
    SpeechSynthesisUtterance: class {}, speechSynthesis: { cancel() {}, speak() {} }
  });
  pending.elements.get('speak-replies').checked = true;
  const app = client().mountBrowser(pending.document, pending.window); await app.ready;
  await pending.elements.get('mic-toggle').fire('click');
  await app.sendText('question');
  assert.ok(recognition);
  assert.doesNotMatch(pending.elements.get('mic-description').textContent, /Listening/i);
  app.stop();
});

function client() {
  assert.ok(fs.existsSync(source), 'browser client exists');
  return require(source);
}

test('wake gate uses explicit continuous state and practical native-script aliases', () => {
  const { wakeDecision } = client();
  assert.deepEqual(wakeDecision('ordinary private conversation', false), { type: 'ignore', conversationActive: false });
  for (const wake of ['Jarvis!', 'जार्विस', 'जारविस', 'జార్విస్', 'ஜார்விஸ்']) assert.deepEqual(wakeDecision(wake, false), { type: 'wake', conversationActive: true });
  assert.deepEqual(wakeDecision('what time is it?', true), { type: 'command', text: 'what time is it?', conversationActive: true });
  assert.equal(wakeDecision('Jarvison is here', false).type, 'ignore');
  assert.equal(wakeDecision('', true).type, 'ignore');
});

test('one wake supports two followups, bare stop preserves conversation, stop Jarvis ends it', async () => {
  const h = await voiceHarness();
  await h.recognition.hear('Jarvis'); h.spoken.at(-1).onend();
  for (const text of ['first question', 'second question']) { await h.recognition.hear(text); h.spoken.at(-1).onend(); }
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 2);
  await h.recognition.hear('third question'); await h.recognition.hear('stop');
  assert.equal(h.elements.get('mic-state').textContent, 'Active · listening to you');
  await h.recognition.hear('fourth question');
  await h.recognition.hear('stop Jarvis');
  assert.equal(h.elements.get('mic-state').textContent, 'Waiting for “Jarvis”');
  assert.equal(h.elements.get('mic-toggle').getAttribute('aria-pressed'), 'true');
  await h.recognition.hear('private conversation');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 4);
  await h.recognition.hear('Jarvis hello again'); h.spoken.at(-1).onend();
  await h.recognition.hear('Jarvis stop listening'); await h.recognition.hear('private words');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 5);
  h.app.stop();
});

test('ending conversation during work keeps result text but suppresses speech', async () => {
  let release;
  const h = await voiceHarness(() => new Promise(resolve => { release = () => resolve({ body: { text: 'Visible result' } }); }));
  const pending = h.recognition.hear('Jarvis work');
  await h.recognition.hear('stop Jarvis'); release(); await pending;
  assert.equal(h.spoken.length, 0);
  assert.match(allText(h.elements.get('messages')), /Visible result/);
  await h.recognition.hear('unrelated private words');
  assert.equal(h.requests.filter(r => r.url === '/api/chat').length, 1);
  h.app.stop();
});
