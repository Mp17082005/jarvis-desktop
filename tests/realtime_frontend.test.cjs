'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = path.join(__dirname, '../static/realtime.js');
const load = () => fs.existsSync(source) ? require(source) : {};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function controller(options = {}) {
  const sent = [], requests = [], messages = [];
  const {RealtimeController} = load();
  assert.equal(typeof RealtimeController, 'function', 'native event controller exists');
  const core = new RealtimeController({send: e => sent.push(e), request: async (url, body) => {requests.push({url, body}); return {text:'Verified result'};}, caption: (...args) => messages.push(args), transcriptTimeout:20, ...options});
  core.id = 'rtc-test';
  return {core, sent, requests, messages};
}
test('bridge waits for the linked actual transcript, deduplicates, and waits for response.done', async () => {
  const {core, sent, requests} = controller();
  core.receive({type:'input_audio_buffer.speech_started', item_id:'turn-a'});
  core.receive({type:'response.created', response:{id:'response-a'}});
  const call = {type:'response.function_call_arguments.done', response_id:'response-a', call_id:'call-a', name:'ask_jarvis', arguments:JSON.stringify({request:'MODEL PRIVATE LEAK'})};
  const pending = core.receive(call);
  assert.equal(requests.length, 0);
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'turn-a', transcript:'What is the public weather?'});
  await pending;
  await core.receive(call);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.user_text, 'What is the public weather?');
  assert.equal(requests[0].url, '/api/realtime/tool');
  assert.equal(sent.filter(e=>e.type==='response.create').length, 0);
  core.receive({type:'response.done', response:{id:'response-a'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length, 1);
  assert.equal(sent.find(e=>e.type==='conversation.item.create').item.type, 'function_call_output');
});
test('pending action details stay on screen and only a minimal review notice reaches the voice model', async () => {
  const data = {session_id:'private-chat', text:'Send to alice.bob@example.com. Say approve 1234.', confirmation:{id:'private-approval', tool:'send_email', arguments:{to:'alice.bob@example.com', body:'Exact private message'}, spoken_summary:'Send to alice.bob@example.com. Say approve 1234.', voice_phrase:'approve 1234'}};
  const {core, sent, messages} = controller({request:async()=>data});
  core.beginTurn('u'); core.turns.get('u').text='Send the requested message';
  core.receive({type:'response.created', response:{id:'tool-response'}});
  await core.receive({type:'response.function_call_arguments.done', response_id:'tool-response', call_id:'call', name:'ask_jarvis', arguments:'{}'});
  core.receive({type:'response.done', response:{id:'tool-response'}});
  const output = sent.find(e=>e.item?.type==='function_call_output');
  assert.deepEqual(JSON.parse(output.item.output), {pending_approval:true, message:'Action ready. Review the exact details on screen and use the buttons.'});
  const response = sent.find(e=>e.type==='response.create').response;
  assert.equal(response.tool_choice, 'none');
  assert.match(response.instructions, /action is ready.*review.*on screen/i);
  assert.doesNotMatch(JSON.stringify(sent), /voice_phrase|spoken_summary|1234|alice\.bob|Exact private message|private-chat|private-approval/);
  assert.deepEqual(core.pending.confirmation, data.confirmation);
  assert.ok(messages.some(m=>m[0]==='Server' && m[1]===data.text));
});
test('interrupt suppresses stale tool speech but preserves returned context; stop cancels late VAD response', async () => {
  let resolve;
  const {core, sent} = controller({request:()=>new Promise(r=>{resolve=r;})});
  core.receive({type:'input_audio_buffer.speech_started', item_id:'a'});
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'a', transcript:'Research weather'});
  core.receive({type:'response.created', response:{id:'r'}});
  const pending = core.receive({type:'response.function_call_arguments.done', response_id:'r', call_id:'c', name:'ask_jarvis', arguments:'{}'});
  await delay(0);
  assert.equal(typeof core.interrupt, 'function');
  core.interrupt(); resolve({text:'Late weather'}); await pending;
  core.receive({type:'response.done', response:{id:'r'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length, 0);
  assert.ok(sent.some(e=>e.type==='output_audio_buffer.clear'));
  assert.ok(sent.some(e=>e.item?.type==='function_call_output'));
  core.receive({type:'input_audio_buffer.speech_started', item_id:'stop'});
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'stop', transcript:'stop'});
  core.receive({type:'response.created', response:{id:'late-stop'}});
  assert.equal(sent.at(-1).type, 'output_audio_buffer.clear');
});
test('missing or unrelated transcript fails closed without a server request', async () => {
  const {core, sent, requests} = controller();
  core.receive({type:'input_audio_buffer.speech_started', item_id:'missing'});
  core.receive({type:'response.created', response:{id:'r'}});
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'other', transcript:'PRIVATE'});
  await core.receive({type:'response.function_call_arguments.done', response_id:'r', call_id:'c', name:'ask_jarvis', arguments:'{"request":"invented"}'});
  assert.equal(requests.length, 0);
  assert.match(sent.find(e=>e.item?.type==='function_call_output').item.output, /repeat/);
});
test('a newer question never receives the previous response tool call', async () => {
  const {core, requests} = controller();
  core.receive({type:'input_audio_buffer.speech_started', item_id:'a'});
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'a', transcript:'old'});
  core.receive({type:'response.created', response:{id:'r'}});
  core.receive({type:'input_audio_buffer.speech_started', item_id:'b'});
  core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'b', transcript:'new'});
  await core.receive({type:'response.function_call_arguments.done', response_id:'r', call_id:'c', name:'ask_jarvis', arguments:'{}'});
  assert.equal(requests.length, 0);
});
for (const changedRecipient of [false, true]) {
  test(`delayed playback echo never confirms${changedRecipient ? ' even after recipient punctuation changes' : ''}`, async () => {
    const statuses = [];
    const {core, requests, sent} = controller({status:text=>statuses.push(text)});
    const data = {session_id:'chat', confirmation:{id:'approval', spoken_summary:'Send $10.25 to alice.bob@example.com. Say approve 1234.', voice_phrase:'approve 1234', arguments:{to:'alice.bob@example.com', amount:'$10.25'}}};
    core.setConfirmation(data, core.epoch);
    core.receive({type:'response.created', response:{id:'summary'}});
    core.receive({type:'output_audio_buffer.started', response_id:'summary'});
    core.receive({type:'response.output_audio_transcript.done', response_id:'summary', transcript:changedRecipient ? data.confirmation.spoken_summary.replace('alice.bob', 'alice-bob') : data.confirmation.spoken_summary});
    core.receive({type:'output_audio_buffer.stopped', response_id:'summary'});
    // Playback has ended at the provider; delayed acoustic echo arrives later.
    core.receive({type:'input_audio_buffer.speech_started', item_id:'echo'});
    core.receive({type:'conversation.item.input_audio_transcription.completed', item_id:'echo', transcript:'approve 1234'});
    await delay(0);
    assert.equal(requests.filter(r=>r.url==='/api/confirm').length, 0);
    assert.equal(requests.length, 0);
    assert.equal(core.pending.armed, false);
    assert.deepEqual(core.pending.confirmation, data.confirmation);
    assert.equal(core.turns.get('echo').blocked, true);
    assert.ok(sent.some(e=>e.type==='output_audio_buffer.clear'));
    assert.match(statuses.at(-1), /review.*buttons/i);
  });
}
test('direct voice confirmation is rejected without submitting or consuming pending approval', async () => {
  for (const phrase of ['approve 1234', '', null, undefined]) {
    const {core, requests} = controller();
    const data = {session_id:'chat', confirmation:{id:'approval', voice_phrase:'approve 1234'}};
    core.setConfirmation(data, 0);
    await core.confirm(true, phrase);
    assert.equal(requests.length, 0);
    assert.equal(core.pending.submitting, false);
    assert.deepEqual(core.pending.confirmation, data.confirmation);
    // A fresh button-style invocation remains valid after a rejected voice call.
    await core.confirm(true);
    assert.deepEqual(requests, [{url:'/api/confirm', body:{id:'approval', session_id:'chat', approve:true}}]);
  }
});
test('changed numbers, interrupted summary, generic yes and model approvals never arm voice', async () => {
  for (const transcript of ['Send 1025 dollars.', 'Send 10.26 dollars.', '']) {
    const {core, requests} = controller();
    core.setConfirmation({session_id:'chat',confirmation:{id:'a', spoken_summary:'Send 10.25 dollars.',voice_phrase:'approve 1234'}}, 0);
    core.receive({type:'response.created',response:{id:'r'}});
    core.receive({type:'output_audio_buffer.started',response_id:'r'});
    core.receive({type:'response.output_audio_transcript.done',response_id:'r',transcript});
    core.receive({type:'output_audio_buffer.stopped',response_id:'r'});
    assert.equal(core.pending.armed,false);
    core.receive({type:'input_audio_buffer.speech_started',item_id:'u'});
    core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:'yes'});
    await delay(0);
    assert.equal(requests.length,0);
  }
});
test('native session requests real mic only on Start and closes every resource on End', async () => {
  const {VoiceSession} = load();
  assert.equal(typeof VoiceSession, 'function');
  const h = transportHarness();
  const s = new VoiceSession(h.options);
  assert.equal(h.mediaCalls.length, 0);
  await s.start('ta-IN');
  assert.deepEqual(h.mediaCalls[0], {audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true}, video:false});
  assert.deepEqual(h.order.slice(0,3), ['track', 'channel:oai-events', 'offer']);
  assert.equal(h.requests[0].url, '/api/realtime/connect');
  assert.equal(h.requests[0].timeout, 40000);
  assert.equal(h.requests[0].body.language,'ta-IN');
  assert.equal(h.peer.remote.sdp, 'answer-sdp');
  s.mute(true); assert.equal(h.track.enabled,false);
  await s.stop('Ended');
  assert.equal(h.track.stopped,true);
  assert.equal(h.peer.closed,true);
  assert.equal(h.audio.srcObject,null);
  assert.equal(h.requests.at(-1).url,'/api/realtime/end');
  assert.deepEqual(h.requests.at(-1).body,{id:'session'});
});
test('MAINT-01 configured expiry renews once with the same live microphone and a fresh peer', async () => {
  const h=renewalHarness();const s=new (load().VoiceSession)(h.options);
  await s.start('ta-IN');const oldCore=s.core,oldPeer=s.peer;
  h.clock.advance(600000);await delay(0);
  assert.equal(h.peers.length,1,'no forced ten-minute boundary');
  h.clock.advance(2700000);await delay(0);
  assert.equal(h.peers.length,2,'configured maintenance creates a fresh peer');
  assert.equal(h.mediaCalls.length,1,'renewal never reacquires microphone');
  assert.equal(oldPeer.closed,true);assert.equal(oldCore.closed,true);
  assert.equal(s.stream,h.stream);assert.equal(h.track.stopped,undefined);
  assert.equal(s.id,'session-2');assert.equal(s.running,true);
  assert.deepEqual(h.requests.map(r=>r.url),['/api/realtime/connect','/api/realtime/end','/api/realtime/connect']);
  assert.equal(h.requests.at(-1).body.language,'ta-IN');
  assert.equal(h.cleanups.length,1,'camera cleanup runs at renewal');
  assert.equal(h.events.length,0,'no private context or tools replayed');
  await s.stop();assert.equal(h.track.stopped,true);assert.equal(h.clock.jobs.size,0);
});
test('MAINT-02 expiry fails closed for opt-out, approvals, busy tools, hidden tab, muted or ended mic and lost transport', async()=>{
  for(const state of ['optout','pending','busy','speaking','capturing','playing','active','hidden','muted','ended','no-tracks','disconnected','channel','recovered']) {
    const h=renewalHarness();const s=new (load().VoiceSession)(h.options);await s.start();
    if(state==='optout')h.options.keepConversation=()=>false;
    if(['pending','busy','speaking','capturing','playing','active'].includes(state))s.core[state]=true;
    if(state==='hidden')h.options.visible=()=>false;
    if(state==='muted')s.mute(true);
    if(state==='ended')h.track.readyState='ended';
    if(state==='no-tracks')h.stream.getAudioTracks=()=>[];
    if(state==='disconnected')s.peer.connectionState='disconnected';
    if(state==='channel')s.dc.readyState='closed';
    if(state==='recovered'){s.peer.connectionState='disconnected';s.peer.onconnectionstatechange();s.peer.connectionState='connected';s.peer.onconnectionstatechange();s.mute(false);}
    h.clock.advance(3300000);await delay(0);
    assert.equal(h.peers.length,1,state);assert.equal(s.running,false,state);assert.equal(h.track.stopped,true,state);
    h.clock.advance(3300000);await delay(0);assert.equal(h.peers.length,1,state+' never retries');
  }
});
test('MAINT-03 End or visibility/consent loss during renewal cancels it, failed renewal never loops',async()=>{
  for(const state of ['end','hidden','optout','ended','failure']) {
    const h=renewalHarness();const s=new (load().VoiceSession)(h.options);await s.start();
    const request=h.options.request;let release;
    h.options.request=async(url,...args)=>{if(url.endsWith('/end') && !release)await new Promise(r=>{release=r;});if(state==='failure'&&url.endsWith('/connect'))throw Error('offline');return request(url,...args);};
    const renewing=s.maintain();await delay(0);
    assert.equal(s.running,true);assert.equal(h.track.stopped,undefined);
    if(state==='end')await s.stop();
    if(state==='hidden')h.options.visible=()=>false;
    if(state==='optout')h.options.keepConversation=()=>false;
    if(state==='ended')h.track.readyState='ended';
    release();await renewing;
    assert.equal(s.running,false,state);assert.equal(h.track.stopped,true,state);
    h.clock.advance(6600000);await delay(0);assert.equal(h.peers.length,state==='failure'?2:1,state);
  }
});
test('MAINT-04 visible renewal opt-in drives mounted maintenance and labels provider costs/context reset',async()=>{
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  assert.match(html, /id="keep-conversation"[^>]*type="checkbox"/);
  assert.doesNotMatch(html, /id="keep-conversation"[^>]*checked/);
  assert.match(html,/usage costs.*continue/i);assert.match(html,/context.*reset/i);
  assert.doesNotMatch(html,/at most 10 minutes/);
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
  try {
    assert.match(h.elements.get('session-clock').textContent,/session maintenance/i);
    h.track.readyState='live';h.elements.get('keep-conversation').checked=true;
    await h.app.session.maintain();assert.equal(h.mediaCalls.length,1);
    assert.equal(h.fetches.filter(r=>r.url==='/api/realtime/connect').length,2);
    h.elements.get('keep-conversation').checked=false;await h.app.session.maintain();assert.equal(h.app.session.running,false);
  } finally {await h.app.session.stop();}
});
test('MAINT-05 camera lifetime follows session deadline rather than a separate ten-minute cap',async()=>{
  const h=liveHarness();h.options.deadline=()=>3300000;
  await h.v.startLive();h.clock.advance(600000);assert.equal(h.v.live,true);
  h.clock.advance(2700000);assert.equal(h.v.live,false);assert.equal(h.track.stopped,true);
});
test('WAKE-01 prior Start and permission required; serial wake never duplicates starts',async()=>{
  const h=await handsfreeHarness();
  try {
    await h.wake();assert.equal(h.mediaCalls.length,0);
    await h.optin();await h.wake();assert.equal(h.mediaCalls.length,0);
    await h.elements.get('start-conversation').fire('click');await h.say('Stop Jarvis');
    h.setPermission('prompt');await h.wake();assert.equal(h.mediaCalls.length,1);
    assert.equal(h.elements.get('allow-handsfree').checked,false);
    h.setPermission('granted');await h.optin();await Promise.all([h.wake(),h.wake()]);
    assert.equal(h.mediaCalls.length,2);assert.equal(h.listening(),false);
    await h.app.pollWake();assert.equal(h.mediaCalls.length,2);
  } finally {await h.app.session.stop();}
});
test('WAKE-02 hidden, auth, network, End and pagehide revoke consent; late wake cannot restart',async()=>{
  for(const state of ['hidden','auth','network','end','pagehide']) {
    const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.optin();
    if(state==='network') {
      h.app.session.peer.connectionState='disconnected';h.app.session.peer.onconnectionstatechange();
    } else {
      await h.say('Stop Jarvis');
      const original=h.window.fetch;let release;
      h.window.fetch=async(url,opts)=>url==='/api/desktop/wake'?new Promise(r=>{release=()=>r({status:200,ok:true,json:async()=>({event:true})});}):original(url,opts);
      const polling=h.app.pollWake();await delay(0);
      if(state==='hidden'){h.document.hidden=true;h.document.listeners.visibilitychange();}
      if(state==='pagehide')h.window.listeners.pagehide();
      if(state==='end')await h.elements.get('end-conversation').fire('click');
      if(state==='auth'){h.window.fetch=async()=>({status:401,ok:false});await h.app.refresh();}
      release();await polling;
    }
    assert.equal(h.elements.get('allow-handsfree').checked,false,state);
    h.document.hidden=false;await h.wake();assert.equal(h.mediaCalls.length,1,state);
    await h.app.session.stop();
  }
});
test('WAKE-03 native listener pauses before manual Start and remains off while browser is muted',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();
  await h.optin();assert.equal(h.listening(),true);
  await h.elements.get('start-conversation').fire('click');assert.equal(h.listening(),false);
  await h.elements.get('mic-toggle').fire('click');assert.equal(h.tracks.at(-1).enabled,false);
  const count=h.controls.length, polls=h.wakeCalls();await h.wake();assert.equal(h.controls.length,count);assert.equal(h.wakeCalls(),polls,'muted browser never refreshes wake lease');assert.equal(h.listening(),false);
  await h.app.session.stop();await delay(0);assert.equal(h.controls.at(-1),false);
});
test('WAKE-04 failed native enable readback fails closed and disables cleanup',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();
  const original=h.window.fetch;
  h.window.fetch=async(url,opts)=>url==='/api/desktop/status'?{status:200,ok:true,json:async()=>({enabled:true,wake_available:true,wake_enabled:false})}:original(url,opts);
  await h.optin();assert.equal(h.elements.get('allow-handsfree').checked,false);
  assert.deepEqual(h.controls.slice(-2),[true,false]);
});
async function handsfreeHarness({host='127.0.0.1',desktop=true}={}) {
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');
  const original=h.window.fetch;let listening=false,permission='granted',event=false,wakeCalls=0;
  const controls=[],tracks=[],peers=[];let maxCaptures=0;
  const captures=()=>tracks.filter(t=>!t.stopped).length+Number(listening);
  const check=()=>{maxCaptures=Math.max(maxCaptures,captures());assert.ok(captures()<=1,'native and browser must never capture concurrently');};
  h.window.location={hostname:host};h.window.setInterval=()=>1;h.window.clearInterval=()=>{};
  h.window.navigator.permissions={query:async()=>({state:permission})};
  h.window.navigator.mediaDevices.getUserMedia=async c=>{
    h.mediaCalls.push(c);const track={enabled:true,readyState:'live',stop(){this.stopped=true;this.readyState='ended';}};tracks.push(track);check();
    return {getTracks:()=>[track],getAudioTracks:()=>[track]};
  };
  h.window.RTCPeerConnection=function(){const p=transportHarness().peer;peers.push(p);return p;};
  h.window.fetch=async(url,opts)=>{
    const response=data=>({status:200,ok:true,json:async()=>data});
    if(url==='/api/desktop/status')return response({enabled:desktop,wake_available:true,wake_enabled:listening});
    if(url==='/api/desktop/wake-control'){listening=JSON.parse(opts.body).enabled;controls.push(listening);check();return response({wake_enabled:listening});}
    if(url==='/api/desktop/wake'){wakeCalls++;const found=event&&listening;event=false;return response({event:found});}
    return original(url,opts);
  };
  await h.app.refresh();
  const optin=async()=>{h.elements.get('allow-handsfree').checked=true;await h.elements.get('allow-handsfree').fire('change');};
  const say=async text=>{const core=h.app.session.core;const id='say-'+core.epoch;core.receive({type:'input_audio_buffer.speech_started',item_id:id});core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:id,transcript:text});await delay(0);};
  return {...h,controls,tracks,peers,optin,say,listening:()=>listening,wakeCalls:()=>wakeCalls,maxCaptures:()=>maxCaptures,setPermission:v=>permission=v,wake:async()=>{event=true;await h.app.pollWake();}};
}
test('STANDBY-03 opt-in Start -> sleep -> wake -> spoken Stop -> wake has exactly one capture and no approval bypass',async()=>{
  const h=await handsfreeHarness();
  try {
    await h.optin();assert.equal(h.listening(),false,'checkbox alone never acquires initial mic');
    await h.elements.get('start-conversation').fire('click');assert.equal(h.app.session.running,true);
    await h.say('go to sleep');assert.equal(h.app.session.running,false);assert.equal(h.listening(),true);
    assert.equal(h.elements.get('allow-handsfree').checked,true);
    await h.wake();assert.equal(h.app.session.running,true);assert.equal(h.listening(),false);
    const camera={stop(){this.stopped=true;}};h.app.vision.stream={getTracks:()=>[camera]};
    h.app.session.core.setConfirmation({session_id:'s',confirmation:{id:'a'}},0);
    await h.say('Stop Jarvis');assert.equal(camera.stopped,true);assert.equal(h.listening(),true);
    await Promise.all([h.wake(),h.app.pollWake()]);assert.equal(h.app.session.running,true);
    assert.equal(h.mediaCalls.length,3);assert.equal(h.maxCaptures(),1);
    assert.equal(h.fetches.filter(r=>r.url==='/api/confirm').length,0);
    await h.say('stop listening completely');assert.equal(h.listening(),false);assert.equal(h.elements.get('allow-handsfree').checked,false);
    await h.wake();assert.equal(h.mediaCalls.length,3);
  } finally {await h.app.session.stop();}
});
test('STANDBY-01 exact sleep controls request standby, complete stop revokes, never submits approval', async()=>{
  for(const [text,standby] of [['Stop Jarvis',true],['go to sleep',true],['stop listening completely',false]]) {
    const ends=[];const {core,requests}=controller({end:(...args)=>ends.push(args)});
    core.setConfirmation({session_id:'s',confirmation:{id:'a'}},0);
    core.receive({type:'input_audio_buffer.speech_started',item_id:'u'});
    core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:text});
    assert.equal(ends.length,1,text);assert.equal(ends[0][1]?.standby,standby,text);
    assert.equal(requests.length,0);assert.equal(core.pending.armed,false);
  }
});
test('STANDBY-02 resume callback waits for acknowledged teardown and never runs after explicit End or failed close',async()=>{
  for(const outcome of ['ok','end','failure']) {
    const h=transportHarness();let release,resumes=0,revokes=0;
    Object.assign(h.options,{standbyAllowed:()=>true,standby:()=>{resumes++;assert.equal(h.track.stopped,true);assert.equal(h.peer.closed,true);},stopped:()=>revokes++});
    const s=new (load().VoiceSession)(h.options);await s.start();
    h.options.request=()=>new Promise((resolve,reject)=>{release=()=>outcome==='failure'?reject(Error('offline')):resolve({ok:true});});
    const closing=s.stop('Standby',false,{standby:true});
    assert.equal(resumes,0);assert.equal(h.track.stopped,true);
    if(outcome==='end')await s.stop();
    release();await closing;
    assert.equal(resumes,outcome==='ok'?1:0,outcome);
    assert.equal(revokes,outcome==='ok'?0:1,outcome);
  }
});
test('IDLE-01 desktop consent idles after five minutes, releases camera and mic before local wake',async()=>{
  const h=renewalHarness();let resumed=0;
  Object.assign(h.options,{standbyAllowed:()=>true,standby:()=>resumed++});
  const s=new (load().VoiceSession)(h.options);await s.start();
  h.clock.advance(299000);assert.equal(s.running,true);
  h.clock.advance(1000);await delay(0);
  assert.equal(s.running,false);assert.equal(h.track.stopped,true);assert.equal(h.cleanups.length,1);assert.equal(resumed,1);
  assert.equal(h.clock.jobs.size,0);assert.equal(h.peers.length,1);
});
test('IDLE-02 speech, typed input, tool activity and camera requests reset idle; passive frames do not',async()=>{
  for(const activity of ['speech','transcript','typed','tool','camera','image']) {
    const h=renewalHarness();h.options.standbyAllowed=()=>true;h.options.idleSeconds=()=>60;
    const s=new (load().VoiceSession)(h.options);await s.start();
    h.clock.advance(59000);
    if(activity==='speech')s.core.receive({type:'input_audio_buffer.speech_started',item_id:'u'});
    if(activity==='transcript'){s.core.beginTurn('u');s.core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:'Hello'});}
    if(activity==='typed')s.core.typed('Hello');
    if(activity==='tool'){s.core.beginTurn('u');s.core.turns.get('u').text='Weather';s.core.receive({type:'response.created',response:{id:'r'}});await s.core.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'c',name:'ask_jarvis',arguments:'{}'});}
    if(activity==='camera')s.core.typed('see me');
    if(activity==='image')s.core.image('data:image/jpeg;base64,/9j/AA==','Describe');
    s.core.speaking=false;s.core.capturing=false;s.core.active=null;s.core.queue=[];
    h.clock.advance(59000);assert.equal(s.running,true,activity);
    h.clock.advance(1000);await delay(0);assert.equal(s.running,false,activity);
  }
  const h=renewalHarness();h.options.standbyAllowed=()=>true;
  const s=new (load().VoiceSession)(h.options);await s.start();h.clock.advance(299000);
  s.core.liveFrame('data:image/jpeg;base64,/9j/AA==');h.clock.advance(1000);await delay(0);assert.equal(s.running,false);
});
test('IDLE-03 no idle standby without consent, during approvals/work or while muted',async()=>{
  for(const state of ['optout','pending','busy','speaking','capturing','playing','active','muted','hidden','disconnected']) {
    const h=renewalHarness();h.options.standbyAllowed=()=>state!=='optout';
    const s=new (load().VoiceSession)(h.options);await s.start();
    if(['pending','busy','speaking','capturing','playing','active'].includes(state))s.core[state]=true;
    if(state==='muted')s.mute(true);
    if(state==='hidden')h.options.visible=()=>false;
    if(state==='disconnected')s.peer.connectionState='disconnected';
    h.clock.advance(600000);assert.equal(s.running,true,state);await s.stop();
  }
});
test('IDLE-04 mounted timeout setting and camera interaction reset idle; nonlocal/disabled desktop never enables it',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.optin();
  try {
    const input=h.elements.get('handsfree-idle-seconds');assert.ok(input,'visible configurable timeout');
    input.value='60';let now=0;h.app.session.now=()=>now;h.app.session.noteActivity();
    const camera={stop(){this.stopped=true;}};
    h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[camera]});
    h.elements.get('camera-preview').play=async()=>{};
    now=59000;await h.elements.get('enable-camera').fire('click');
    now=60000;assert.equal(h.app.session.checkIdle(),false);
    now=119000;assert.equal(h.app.session.checkIdle(),true);await delay(0);assert.equal(h.listening(),true);
  } finally {await h.app.session.stop();}
  for(const config of [{host:'example.com'},{desktop:false}]) {
    const m=await handsfreeHarness(config);await m.elements.get('start-conversation').fire('click');await m.optin();
    m.app.session.lastActivity=0;assert.equal(m.app.session.checkIdle(),false);
    await m.say('Stop Jarvis');assert.equal(m.listening(),false);assert.equal(m.controls.length,0);await m.app.session.stop();
  }
});
test('WAKE-05 failed disable readback forbids both manual and wake Start; no retry loop',async()=>{
  for(const method of ['manual','wake']) {
    const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();await h.optin();
    const original=h.window.fetch;
    h.window.fetch=async(url,opts)=>url==='/api/desktop/status'?{status:200,ok:true,json:async()=>({enabled:true,wake_available:true,wake_enabled:true})}:original(url,opts);
    if(method==='manual')await h.elements.get('start-conversation').fire('click');else await h.wake();
    assert.equal(h.app.session.running,false);assert.equal(h.mediaCalls.length,1);assert.equal(h.elements.get('allow-handsfree').checked,false);
    await h.wake();assert.equal(h.mediaCalls.length,1);await h.app.session.stop();
  }
});
test('WAKE-06 late native enable/disable responses cannot revive after End or hidden',async()=>{
  for(const transition of ['enable','disable']) {
    const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();
    if(transition==='disable')await h.optin();
    const original=h.window.fetch;let release;
    h.window.fetch=async(url,opts)=>{if(!release&&url==='/api/desktop/wake-control'&&JSON.parse(opts.body).enabled===(transition==='enable'))await new Promise(r=>release=r);return original(url,opts);};
    const operation=transition==='enable'?h.optin():h.wake();await delay(0);
    h.document.hidden=true;h.document.listeners.visibilitychange();release();await operation;await delay(0);
    assert.equal(h.listening(),false);assert.equal(h.mediaCalls.length,1);assert.equal(h.elements.get('allow-handsfree').checked,false);
    assert.doesNotMatch(h.elements.get('connection-status').textContent,/say.*wake/i,'late enable cannot claim standby after privacy stop');
  }
});
test('WAKE-07 failed refresh or offline event revokes idle consent; late response cannot revive status',async()=>{
  for(const loss of ['refresh','offline']) {
    const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();await h.optin();
    if(loss==='refresh'){const original=h.window.fetch;h.window.fetch=async(url,opts)=>{if(url==='/api/realtime/status')throw Error('offline');return original(url,opts);};await h.app.refresh();}
    else {assert.equal(typeof h.window.listeners.offline,'function');h.window.listeners.offline();}
    await delay(0);assert.equal(h.elements.get('allow-handsfree').checked,false,loss);assert.equal(h.listening(),false,loss);
  }
});
test('WAKE-08 wake connection failure never retries or retains consent; missing mic states are explicit',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();await h.optin();
  h.window.navigator.mediaDevices.getUserMedia=async()=>{throw Object.assign(Error('device missing'),{name:'NotFoundError'});};
  await h.wake();assert.equal(h.app.session.running,false);assert.equal(h.elements.get('allow-handsfree').checked,false);assert.equal(h.listening(),false);
  assert.match(h.elements.get('connection-status').textContent,/microphone.*(?:found|unavailable|device)/i);
  await h.wake();assert.equal(h.mediaCalls.length,1);
  const t=transportHarness();t.stream.getAudioTracks=()=>[];
  const s=new (load().VoiceSession)(t.options);await assert.rejects(s.start(),/microphone/i);
  assert.equal(t.track.stopped,true);assert.equal(t.requests.length,0);
});
test('WAKE-09 optional desktop status failure revokes consent and missing mic explains unavailable',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();await h.optin();
  const original=h.window.fetch;
  h.window.fetch=async(url,opts)=>{if(url==='/api/desktop/status')throw Error('offline');return original(url,opts);};
  await h.app.refresh();assert.equal(h.elements.get('allow-handsfree').checked,false);await h.app.pollWake();
  const m=await handsfreeHarness();const orig=m.window.fetch;
  m.window.fetch=async(url,opts)=>url==='/api/desktop/status'?{status:200,ok:true,json:async()=>({enabled:true,wake_available:false,wake:{reason:'microphone_unavailable'}})}:orig(url,opts);
  await m.app.refresh();assert.match(m.elements.get('handsfree-status').textContent,/microphone.*unavailable/i);
});
test('WAKE-10 loss of wake availability never bypasses known desktop shutdown on manual Start',async()=>{
  const h=await handsfreeHarness();await h.elements.get('start-conversation').fire('click');await h.app.session.stop();await h.optin();
  const original=h.window.fetch;
  h.window.fetch=async(url,opts)=>url==='/api/desktop/status'?{status:200,ok:true,json:async()=>({enabled:true,wake_available:false,wake_enabled:true})}:original(url,opts);
  await h.app.refresh();await delay(0);
  await h.elements.get('start-conversation').fire('click');
  assert.equal(h.mediaCalls.length,1,'unavailable status is not proof of stopped capture');assert.equal(h.app.session.running,false);
});
test('IDLE-05 mute/unmute, playback volume and interrupt reset interaction timeout',async()=>{
  for(const interaction of ['mute','volume','interrupt']) {
    const h=renewalHarness();h.options.standbyAllowed=()=>true;h.options.idleSeconds=()=>60;
    const s=new (load().VoiceSession)(h.options);await s.start();h.clock.advance(59000);
    if(interaction==='mute'){s.mute(true);s.mute(false);}
    if(interaction==='volume')s.setVolume({value:20});
    if(interaction==='interrupt')s.core.interrupt();
    h.clock.advance(59000);assert.equal(s.running,true,interaction);
    h.clock.advance(1000);await delay(0);assert.equal(s.running,false,interaction);
  }
});
function renewalHarness() {
  const h=transportHarness(),clock=fakeClock(),peers=[],events=[],cleanups=[];
  h.track.readyState='live';let serial=0;
  Object.assign(h.options,{now:clock.now,setInterval:(fn,ms)=>{const token={};const run=()=>{fn();if(!token.cancelled)token.id=clock.setTimeout(run,ms);};token.id=clock.setTimeout(run,ms);return token;},clearInterval:token=>{if(token){token.cancelled=true;clock.clearTimeout(token.id);}},keepConversation:()=>true,visible:()=>true,cleanup:()=>cleanups.push(true),createPeer:()=>{const t=transportHarness();t.dc.send=data=>events.push(JSON.parse(data));peers.push(t.peer);return t.peer;},request:async(url,body,timeout)=>{h.requests.push({url,body,timeout});return url.endsWith('/connect')?{id:'session-'+(++serial),sdp:'answer-sdp',max_seconds:3300}:{ok:true};}});
  return {...h,clock,peers,events,cleanups};
}
function transportHarness() {
  const mediaCalls=[], order=[], requests=[];
  const track={enabled:true, stop(){this.stopped=true;}};
  const stream={getTracks:()=>[track], getAudioTracks:()=>[track]};
  const audio={volume:0.8, paused:false, srcObject:null, play:async()=>{}, pause(){this.paused=true;}};
  const dc={readyState:'open',send(){},close(){this.closed=true;}};
  const peer={iceGatheringState:'complete', connectionState:'connected', addTrack(){order.push('track');},createDataChannel(name){order.push('channel:'+name);return dc;},createOffer:async()=>{order.push('offer');return {type:'offer',sdp:'offer-sdp'};},setLocalDescription:async function(value){this.localDescription=value;},setRemoteDescription:async function(value){this.remote=value;}, close(){this.closed=true;}};
  const options={media:async c=>{mediaCalls.push(c);return stream;},createPeer:()=>peer,audio,request:async(url,body,timeout)=>{requests.push({url,body,timeout});return {id:'session',sdp:'answer-sdp',max_seconds:600};},status:()=>{}};
  return {mediaCalls,order,requests,track,stream,peer,audio,dc,options};
}
test('failed signalling and late microphone permission release all tracks',async()=>{
  const {VoiceSession}=load();
  let h=transportHarness(); h.options.request=async()=>{throw Error('no signal');};
  let s=new VoiceSession(h.options); await assert.rejects(s.start(),/no signal/);
  assert.equal(h.track.stopped,true);assert.equal(h.peer.closed,true);
  h=transportHarness();let resolve;h.options.media=()=>new Promise(r=>{resolve=r;});
  s=new VoiceSession(h.options);const starting=s.start();await s.stop();resolve(h.stream);await starting;
  assert.equal(h.track.stopped,true);assert.equal(h.requests.length,0);
});
test('typed turns use exact text and native tools cannot end or change volume from invented arguments', async()=>{
  const {core, requests, sent} = controller();
  assert.equal(typeof core.typed,'function');
  core.typed('Please look up the weather');
  const item = sent.find(e=>e.item?.role==='user');
  assert.equal(item.item.content[0].text,'Please look up the weather');
  core.receive({type:'response.created',response:{id:'typed-response'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'typed-response',call_id:'typed-call',name:'ask_jarvis',arguments:'{"request":"made up"}'});
  assert.equal(requests[0].body.user_text,'Please look up the weather');
  let ended=false,volume=null;
  const h=controller({end:()=>{ended=true;},volume:c=>{volume=c;}});
  h.core.typed('Tell me a joke'); h.core.receive({type:'response.created',response:{id:'r'}});
  await h.core.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'x',name:'end_conversation',arguments:'{}'});
  await h.core.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'v',name:'set_volume',arguments:'{"level":100}'});
  assert.equal(ended,false);assert.equal(volume,null);
});
test('default UI loads only native assets; legacy retains its original script',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  assert.match(html,/src="\/realtime.js"/);
  assert.doesNotMatch(html,/src="\/app.js"/);
  for(const id of ['start-conversation','end-conversation','mic-toggle','interrupt','voice-volume','voice-language','connection-status','login-form','token','enable-audio','captions','composer','command','confirmation-panel']) assert.ok(html.includes(`id="${id}"`),id);
  assert.match(fs.readFileSync(path.join(__dirname,'../static/legacy.html'),'utf8'),/src="\/app.js"/);
  const js=fs.readFileSync(source,'utf8');
  assert.doesNotMatch(js,/speechSynthesis|SpeechRecognition|localStorage|sessionStorage|innerHTML/);
});
test('own result response is not cancelled after a consumed approval turn', async()=>{
  const {core,sent}=controller(); core.beginTurn('u'); core.turns.get('u').blocked=true;
  core.queueResponse(core.epoch,'Report confirmed result');
  core.receive({type:'response.created',response:{id:'own'}});
  assert.equal(sent.filter(e=>e.type==='response.cancel').length,0);
});
test('changing a negative amount to positive never arms approval',()=>{
  const {core}=controller();core.setConfirmation({session_id:'s',confirmation:{id:'c',spoken_summary:'Set amount -10.',voice_phrase:'approve 1111'}},0);
  core.receive({type:'response.created',response:{id:'r'}});
  core.receive({type:'response.output_audio_transcript.done',response_id:'r',transcript:'Set amount 10.'});
  core.receive({type:'output_audio_buffer.stopped',response_id:'r'});
  assert.equal(core.pending.armed,false);
});
class Element {
  constructor(){this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.children=[];this.listeners={};this.classList={toggle(){},remove(){}};}
  addEventListener(name, fn){this.listeners[name]=fn;}
  setAttribute(name,value){this[name]=value;} append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
  async fire(name,detail=1){return this.listeners[name]?.({detail,preventDefault(){}});}
}
function mountedHarness(){
  const ids=[...fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  const elements=new Map(ids.map(id=>[id,new Element()]));
  const h=transportHarness();Object.assign(elements.get('remote-audio'),h.audio);
  const document={hidden:false,getElementById:id=>elements.get(id),createElement:()=>new Element(),listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
  let authorized=false;const fetches=[];
  const window={isSecureContext:true,navigator:{mediaDevices:{getUserMedia:h.options.media}},RTCPeerConnection:function(){return h.peer;},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},fetch:async(url,options)=>{
    fetches.push({url,options});if(url==='/api/login')authorized=true;
    const status=authorized?200:401;
    return {ok:status===200,status,json:async()=>url==='/api/realtime/status'?{enabled:true,model:'test',transport:'webrtc',max_seconds:600}:url==='/api/realtime/connect'?{id:'session',sdp:'answer-sdp',max_seconds:600}:{ok:true}};
  }};
  const app=load().mount(document,window);return {...h,elements,document,window,fetches,app};
}
test('native approval UI is button-only and a fresh click submits the exact visible pending action once', async () => {
  const h = mountedHarness();
  try {
    await delay(0);
    await h.elements.get('login-form').fire('submit');
    await h.elements.get('start-conversation').fire('click');
    const data = {session_id:'exact-chat', confirmation:{id:'exact-approval', tool:'send_email', arguments:{to:'alice.bob@example.com', body:'Keep punctuation: -10.25!'}, spoken_summary:'Exact action: alice.bob@example.com, -10.25!', voice_phrase:'approve 1234'}};
    h.app.session.core.setConfirmation(data, 0);
    assert.equal(h.elements.get('confirmation-panel').hidden, false);
    assert.equal(h.elements.get('confirmation-summary').textContent, data.confirmation.spoken_summary);
    assert.deepEqual(JSON.parse(h.elements.get('confirmation-arguments').textContent), {tool:data.confirmation.tool, arguments:data.confirmation.arguments});
    assert.match(h.elements.get('confirmation-state').textContent, /voice approval is disabled/i);
    assert.doesNotMatch(h.elements.get('confirmation-state').textContent, /say exactly|listen to the entire/i);
    const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    assert.match(html, /Voice approval is disabled/);
    assert.doesNotMatch(html, /Then say|Voice approval becomes available/);
    assert.match(html, /<details open><summary[^>]*>Exact tool/);
    assert.equal(h.fetches.filter(r=>r.url==='/api/confirm').length, 0);
    await h.elements.get('approve-action').fire('click');
    await delay(0);
    await h.elements.get('approve-action').fire('click');
    const confirmations = h.fetches.filter(r=>r.url==='/api/confirm');
    assert.equal(confirmations.length, 1);
    assert.deepEqual(JSON.parse(confirmations[0].options.body), {id:'exact-approval', session_id:'exact-chat', approve:true});
    assert.equal(h.elements.get('confirmation-panel').hidden, true);
  } finally {await h.app.session.stop();}
});
test('browser mount authenticates cookie-only, starts on click, and hidden-tab cleanup never restarts', async()=>{
  const h=mountedHarness(); await delay(0);
  assert.equal(h.mediaCalls.length,0);assert.equal(h.elements.get('login-panel').hidden,false);
  h.elements.get('token').value='user-entered-test';await h.elements.get('login-form').fire('submit');
  assert.equal(h.elements.get('token').value,'');assert.equal(h.elements.get('start-conversation').disabled,false);
  assert.equal(h.fetches.find(r=>r.url==='/api/login').options.credentials,'same-origin');
  await h.elements.get('start-conversation').fire('click');assert.equal(h.mediaCalls.length,1);
  h.document.hidden=true;h.document.listeners.visibilitychange();await delay(0);
  assert.equal(h.track.stopped,true);assert.equal(h.app.session.running,false);
  h.document.hidden=false;h.document.listeners.visibilitychange();assert.equal(h.mediaCalls.length,1);
  assert.ok(h.fetches.some(r=>r.url==='/api/realtime/end'&&r.options.keepalive));
});
test('a complete transcript without audio-start evidence cannot arm approval',()=>{
  const {core}=controller();core.setConfirmation({session_id:'s',confirmation:{id:'c',spoken_summary:'Send ten dollars.',voice_phrase:'approve 1234'}},0);
  core.receive({type:'response.created',response:{id:'r'}});
  core.receive({type:'response.output_audio_transcript.done',response_id:'r',transcript:'Send ten dollars.'});
  core.receive({type:'output_audio_buffer.stopped',response_id:'r'});
  assert.equal(core.pending.armed,false);
});
test('native local tools use only matched raw control text, never generated values',async()=>{
  let level=null;const {core}=controller({volume:c=>{level=c.value;}});
  core.beginTurn('u'); core.turns.get('u').text='set volume to 25 percent';
  core.receive({type:'response.created',response:{id:'r'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'v',name:'set_volume',arguments:'{"level":99}'});
  assert.equal(level,25);
});
test('blocked audio provides recovery and ending aborts in-flight requests',async()=>{
  const h=transportHarness();const blocked=[];h.options.blocked=v=>blocked.push(v);
  h.audio.play=async()=>{throw Error('NotAllowed');};
  const s=new (load().VoiceSession)(h.options);await s.start();await s.play();assert.equal(blocked.at(-1),true);
  h.audio.play=async()=>{};await s.play();assert.equal(blocked.at(-1),false);
  const signal=s.abort.signal;await s.stop();assert.equal(signal.aborted,true);
});
test('end during a pending tool never contaminates a new conversation captions',async()=>{
  let finish;const {core,messages,sent}=controller({request:()=>new Promise(r=>{finish=r;})});
  core.beginTurn('u');core.turns.get('u').text='weather';core.receive({type:'response.created',response:{id:'r'}});
  const call=core.receive({type:'response.function_call_arguments.done',response_id:'r',call_id:'c',name:'ask_jarvis',arguments:'{}'});await delay(0);
  core.closed=true;finish({text:'late result'});await call;
  assert.equal(messages.length,0);assert.equal(sent.length,0);
});
test('disconnection mutes and never automatically reenables microphone',async()=>{
  const h=transportHarness();const s=new (load().VoiceSession)(h.options);await s.start();
  h.peer.connectionState='disconnected';h.peer.onconnectionstatechange();assert.equal(h.track.enabled,false);assert.ok(s.grace);
  h.peer.connectionState='connected';h.peer.onconnectionstatechange();assert.equal(h.track.enabled,false);assert.equal(s.grace,null);
  await s.stop();
});
test('spoken Stop Jarvis ends locally and never reaches tools; full summary interruption remains button-only', async()=>{
  let ended=false;const {core,requests}=controller({end:()=>{ended=true;}});
  core.receive({type:'input_audio_buffer.speech_started',item_id:'end'});
  core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'end',transcript:'Stop Jarvis.'});
  assert.equal(ended,true);assert.equal(requests.length,0);
  const h=controller();h.core.setConfirmation({session_id:'s',confirmation:{id:'a',spoken_summary:'Send ten dollars.',voice_phrase:'approve 4321'}},0);
  h.core.receive({type:'response.created',response:{id:'r'}});
  h.core.receive({type:'output_audio_buffer.started',response_id:'r'});
  h.core.interrupt();
  h.core.receive({type:'response.output_audio_transcript.done',response_id:'r',transcript:'Send ten dollars.'});
  h.core.receive({type:'output_audio_buffer.stopped',response_id:'r'});
  assert.equal(h.core.pending.armed,false);
});
test('a typed approval phrase cannot approve, and button failure never automatically retries',async()=>{
  const {core,requests}=controller();core.setConfirmation({session_id:'s',confirmation:{id:'c',spoken_summary:'Send.',voice_phrase:'approve 1234'}},0);
  core.pending.armed=true;core.typed('approve 1234');await delay(0);assert.equal(requests.length,0);
  let calls=0;core.request=async()=>{calls++;throw Error('uncertain');};await core.confirm(true);await core.confirm(true);assert.equal(calls,1);assert.equal(core.pending.submitting,true);
});
test('explicit image turn sends one native snapshot and speaks without trusting visual tool arguments', async () => {
  const {core, sent, requests} = controller();
  assert.equal(typeof core.image, 'function');
  assert.equal(core.image('data:image/jpeg;base64,/9j/AA==', '  Read this label  '), true);
  const item = sent.find(e=>e.item?.role==='user').item;
  assert.deepEqual(item.content, [{type:'input_text', text:'  Read this label  '}, {type:'input_image', image_url:'data:image/jpeg;base64,/9j/AA=='}]);
  const response = sent.find(e=>e.type==='response.create').response;
  assert.deepEqual(response.output_modalities, ['audio']);
  assert.equal(response.tool_choice, 'none');
  assert.match(response.instructions, /not identify people/i);
  core.receive({type:'response.created',response:{id:'vision'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'vision',call_id:'visual-call',name:'ask_jarvis',arguments:'{"request":"Search private label content"}'});
  assert.equal(requests.length, 0, 'image-derived tools fail closed even if provider ignores tool_choice');
});
test('image sends fail closed for approval, malformed or oversized payloads and interrupt existing speech', () => {
  const {core, sent} = controller();
  const jpeg = 'data:image/jpeg;base64,/9j/AA==';
  core.setConfirmation({session_id:'s',confirmation:{id:'a'}}, 0);
  assert.equal(core.image(jpeg), false);
  assert.equal(sent.length, 0);
  core.pending = null;
  for (const bad of ['https://example.com/photo.jpg', 'data:image/svg+xml;base64,AAAA', 'data:image/jpeg;base64,!', 'data:image/jpeg;base64,' + 'A'.repeat(2*1024*1024)]) assert.equal(core.image(bad), false);
  assert.equal(core.image(jpeg, 'A'.repeat(8001)), false);
  assert.equal(sent.length, 0);
  core.receive({type:'response.created',response:{id:'old'}});
  assert.equal(core.image(jpeg), true);
  assert.equal(sent[0].type, 'response.cancel');
  assert.equal(sent.filter(e=>e.type==='response.create').length, 0);
  core.receive({type:'response.done',response:{id:'old'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length, 1);
  assert.equal(sent.find(e=>e.item?.role==='user').item.content[0].text, 'What do you see?');
});
test('uploaded images decode locally, resize to 1024 and release decoder/canvas without sending', async () => {
  const {prepareImage} = load();
  assert.equal(typeof prepareImage, 'function');
  const draws = []; let closed = false;
  const bitmap = {width:2400,height:1200,close(){closed=true;}};
  const canvas = {getContext:()=>({drawImage:(...args)=>draws.push(args)}),toDataURL:(mime,quality)=>{assert.equal(mime,'image/jpeg');assert.equal(quality,.8);return 'data:image/jpeg;base64,/9j/AA==';}};
  const win = {createImageBitmap:async file=>{assert.equal(file.type,'image/png');return bitmap;}};
  const data = await prepareImage({type:'image/png',size:1000}, {createElement:()=>canvas}, win);
  assert.equal(data, 'data:image/jpeg;base64,/9j/AA==');
  assert.deepEqual(draws[0].slice(1), [0,0,1024,512]);
  assert.equal(closed,true);
  assert.equal(canvas.width,0);assert.equal(canvas.height,0);
});
test('image preparation rejects unsafe formats, byte/pixel limits, corrupt decodes and oversized JPEGs', async () => {
  const {prepareImage} = load();let decodes=0,closed=0,canvases=0;
  const bitmap={width:5000,height:5000,close(){closed++;}};
  const win={createImageBitmap:async()=>{decodes++;return bitmap;}};
  const doc={createElement:()=>{canvases++;return {getContext:()=>({drawImage(){}}),toDataURL:()=> 'data:image/jpeg;base64,'+'A'.repeat(1600000)};}};
  for(const file of [{type:'image/svg+xml',size:1},{type:'image/gif',size:1},{type:'',size:1},{type:'image/png',size:8*1024*1024+1},{type:'image/jpeg',size:0}]) await assert.rejects(prepareImage(file,doc,win),/PNG|JPEG|WebP|8 MB/);
  assert.equal(decodes,0);
  await assert.rejects(prepareImage({type:'image/webp',size:20},doc,win),/20 megapixels/);
  assert.equal(closed,1);assert.equal(canvases,0);
  bitmap.width=0; await assert.rejects(prepareImage({type:'image/png',size:20},doc,win),/dimensions/);
  bitmap.width=100;bitmap.height=100;
  await assert.rejects(prepareImage({type:'image/jpeg',size:20},doc,win),/too large/i);
  await assert.rejects(prepareImage({type:'image/png',size:20},doc,{createImageBitmap:async()=>{throw Error('bad decode');}}),/decode|read/i);
});
test('camera is opt-in local preview only; sharing captures exactly one frame and stop releases it', async () => {
  const {VisionInput} = load();assert.equal(typeof VisionInput,'function');
  const h=visionHarness();const v=new VisionInput(h.options);
  assert.equal(h.mediaCalls.length,0);assert.equal(h.sent.length,0);
  await v.enableCamera();
  assert.equal(h.mediaCalls[0].audio,false);assert.ok(h.mediaCalls[0].video);
  assert.equal(h.preview.srcObject,h.stream);assert.equal(h.preview.muted,true);
  await delay(5);assert.equal(h.draws.length,0);assert.equal(h.sent.length,0);
  assert.equal(await v.share('Read this'),true);
  assert.equal(h.draws.length,1);assert.equal(h.sent.filter(e=>e.item?.role==='user').length,1);
  v.stop();assert.equal(h.track.stopped,true);assert.equal(h.preview.srcObject,null);assert.equal(v.imageData,null);
});
function visionHarness() {
  const h=controller();const mediaCalls=[],draws=[];
  const track={stop(){this.stopped=true;}};const stream={getTracks:()=>[track]};
  const preview={videoWidth:1280,videoHeight:720,play:async()=>{},pause(){}};
  const doc={hidden:false,createElement:()=>({getContext:()=>({drawImage:(...args)=>draws.push(args)}),toDataURL:()=> 'data:image/jpeg;base64,/9j/AA=='})};
  const win={navigator:{mediaDevices:{getUserMedia:async c=>{mediaCalls.push(c);return stream;}}},createImageBitmap:async()=>({width:100,height:100,close(){}})};
  const options={doc,win,preview,core:()=>h.core,ready:()=>true,status:()=>{}};
  return {...h,mediaCalls,draws,track,stream,preview,doc,win,options};
}
test('vision permission/decode generations cannot revive after stop; approval blocks capture and upload', async () => {
  const {VisionInput}=load();const h=visionHarness();const v=new VisionInput(h.options);
  let resolve;h.win.navigator.mediaDevices.getUserMedia=()=>new Promise(r=>{resolve=r;});
  const enabling=v.enableCamera();v.stop();resolve(h.stream);assert.equal(await enabling,false);
  assert.equal(h.track.stopped,true);assert.equal(h.preview.srcObject,null);
  let closed=false;h.win.createImageBitmap=()=>new Promise(r=>{resolve=r;});
  const selecting=v.select({type:'image/png',size:100});v.stop();resolve({width:100,height:100,close(){closed=true;}});
  assert.equal(await selecting,false);assert.equal(closed,true);assert.equal(v.imageData,null);
  h.core.setConfirmation({session_id:'s',confirmation:{id:'a'}},0);
  assert.equal(await v.share('Describe'),false);assert.equal(h.draws.length,1, 'only stale decode drew locally; sharing did not capture');
  assert.equal(await v.select({type:'image/png',size:100}),false);
  assert.equal(h.sent.length,0);
  h.core.pending=null;h.win.createImageBitmap=async()=>({width:100,height:100,close(){}});
  assert.equal(await v.select({type:'image/png',size:100}),true);
  assert.equal(h.sent.length,0);assert.ok(v.imageData);
  assert.equal(await v.share('Read'),true);assert.equal(h.sent.filter(e=>e.item?.role==='user').length,1);
  v.stop();assert.equal(v.imageData,null);
});
test('mounted camera UI discloses upload, sends only on Show, and ends tracks on hidden/pagehide/auth failure', async () => {
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  for(const id of ['enable-camera','stop-camera','show-jarvis','camera-preview','image-upload','vision-question','vision-status']) assert.ok(html.includes(`id="${id}"`),id);
  assert.match(html, /<video[^>]*muted[^>]*playsinline/);
  assert.match(html,/one (?:image|snapshot).*AI provider/i);
  assert.match(html,/not identify people or diagnose conditions/i);
  assert.match(html,/noise.*interrupt.*not.*reconnect/i);
  for (const ending of ['hidden','pagehide','auth','end']) {
    const h=mountedHarness(); await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
    const camera={stop(){this.stopped=true;}};const stream={getTracks:()=>[camera]};
    h.window.navigator.mediaDevices.getUserMedia=async c=>{assert.equal(c.audio,false);return stream;};
    Object.assign(h.elements.get('camera-preview'), {videoWidth:640,videoHeight:480,play:async()=>{},pause(){}});
    const events=[];h.dc.send=data=>events.push(JSON.parse(data));
    h.document.createElement=tag=>tag==='canvas'?{getContext:()=>({drawImage(){}}),toDataURL:()=> 'data:image/jpeg;base64,/9j/AA=='}:new Element();
    await h.elements.get('enable-camera').fire('click');
    assert.equal(h.elements.get('camera-preview').hidden,false);assert.equal(events.length,0);
    assert.equal(h.order.filter(x=>x==='track').length,1, 'only microphone added to peer');
    h.elements.get('vision-question').value='Describe this';await h.elements.get('show-jarvis').fire('click');
    assert.equal(events.filter(e=>e.item?.content?.some(c=>c.type==='input_image')).length,1);
    if(ending==='hidden'){h.document.hidden=true;h.document.listeners.visibilitychange();}
    if(ending==='pagehide')h.window.listeners.pagehide();
    if(ending==='end')await h.elements.get('end-conversation').fire('click');
    if(ending==='auth'){h.window.fetch=async()=>({status:401,ok:false});await h.app.refresh();}
    await delay(0);assert.equal(camera.stopped,true);assert.equal(h.app.vision.imageData,null);assert.equal(h.elements.get('camera-preview').srcObject,null);
    assert.equal(h.elements.get('vision-question').value,'');
  }
});
test('only exact camera voice requests capture once; pending approvals and camera off do not capture', async () => {
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
  let captures=0;h.app.vision.share=async()=>{captures++;return true;};
  const core=h.app.session.core;const sent=[];h.dc.send=data=>sent.push(JSON.parse(data));
  function say(text,id){core.receive({type:'input_audio_buffer.speech_started',item_id:id});core.receive({type:'response.created',response:{id:'r'+id}});core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:id,transcript:text});}
  say('Jarvis look at this','off');assert.equal(captures,0);
  h.app.vision.stream={getTracks:()=>[]};
  say('How is the weather?','weather');say('Look at this price online','unrelated');assert.equal(captures,0);
  for(const [i,text] of ['Jarvis look at this','look at this','what do you see'].entries()) {say(text,String(i));assert.equal(captures,i+1);assert.equal(core.turns.get(String(i)).blocked,true);}
  assert.ok(sent.some(e=>e.type==='response.cancel'));
  core.setConfirmation({session_id:'s',confirmation:{id:'a'}},core.epoch);say('look at this','pending');assert.equal(captures,3);
  await h.app.session.stop();
});
test('rejected image events cancel queued visual speech and offer typed fallback without fabrication', () => {
  const statuses=[];const {core,sent}=controller({status:t=>statuses.push(t)});
  core.image('data:image/jpeg;base64,/9j/AA==','Describe');
  const item=sent.find(e=>e.item?.role==='user');assert.ok(item.event_id);
  core.receive({type:'response.created',response:{id:'vision'}});
  core.receive({type:'error',error:{event_id:item.event_id,code:'invalid_value',message:'input_image unsupported'}});
  assert.match(statuses.at(-1),/image.*unavailable.*type.*describe/i);
  assert.equal(core.turns.get(core.turn).blocked,true);
  assert.equal(sent.at(-1).type,'output_audio_buffer.clear');
  core.receive({type:'response.done',response:{id:'vision'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length,1);
  core.typed('Describe a kettle');assert.equal(sent.filter(e=>e.item?.role==='user').length,2);
});
test('session preference checkboxes have responsive accessible styling',()=>{
  const css=fs.readFileSync(path.join(__dirname,'../static/realtime.css'),'utf8');
  assert.match(css,/\.session-preference\s*\{[^}]*max-width:100%/);
  assert.match(css,/\.session-preference input\s*\{[^}]*width:auto/);
});
test('vision previews and upload controls have bounded responsive styling', () => {
  const css=fs.readFileSync(path.join(__dirname,'../static/realtime.css'),'utf8');
  assert.match(css,/\.vision-preview\s*\{[^}]*max-width:100%/);
  assert.match(css,/\.vision-panel[^}]*input\[type=file\][^}]*max-width:100%/);
});
test('image decoder has a clear unsupported-browser error rather than a raw API failure', async () => {
  await assert.rejects(load().prepareImage({type:'image/png',size:12},{},{}),/browser.*image.*supported browser/i);
});
test('LIVE-01 passive frames preserve audio turn/playback and prune only accepted context', () => {
  const {core,sent,messages}=controller();
  core.beginTurn('audio');core.turns.get('audio').text='Hello';core.playing=true;
  const epoch=core.epoch;
  assert.equal(typeof core.liveFrame,'function');
  const jpeg='data:image/jpeg;base64,/9j/AA==';
  for(let i=0;i<3;i++) {
    assert.equal(core.liveFrame(jpeg),true);
    const event=sent.filter(e=>e.item?.content?.some(c=>c.type==='input_image')).at(-1);
    assert.equal(event.item.role,'user');assert.match(event.item.content[0].text,/untrusted data.*Do not respond unless user asks/);
    assert.equal(core.liveFrame(jpeg),false,'one frame awaiting acceptance');
    core.receive({type:'conversation.item.created',item:event.item});
  }
  assert.equal(core.epoch,epoch);assert.equal(core.turn,'audio');assert.equal(core.playing,true);
  assert.equal(messages.length,0);assert.equal(sent.filter(e=>e.type==='response.create'||e.type==='response.cancel'||e.type==='output_audio_buffer.clear').length,0);
  assert.equal(sent.filter(e=>e.type==='conversation.item.delete').length,1);
  assert.equal(core.liveItems.length,2);
});
function fakeClock() {
  let now=0, serial=0;const jobs=new Map();
  return {now:()=>now, setTimeout(fn,ms){const id=++serial;jobs.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>jobs.delete(id),
    advance(ms){const end=now+ms;while(true){const next=[...jobs].sort((a,b)=>a[1].at-b[1].at)[0];if(!next||next[1].at>end)break;now=next[1].at;jobs.delete(next[0]);next[1].fn();}now=end;},jobs};
}
function liveHarness() {
  const h=visionHarness(),clock=fakeClock();h.preview.readyState=2;
  Object.assign(h.options,{now:clock.now,setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,canSend:()=>true});
  const v=new (load().VisionInput)(h.options);
  const accept=()=>{const event=h.sent.filter(e=>e.item?.content?.some(c=>c.type==='input_image')).at(-1);if(event)h.core.receive({type:'conversation.item.created',item:event.item});};
  return {...h,clock,v,accept};
}
test('LIVE-02 opt-in loop uses 640 JPEG every five seconds without a queued backlog', async()=>{
  const h=liveHarness();h.clock.advance(20000);assert.equal(h.sent.length,0);
  assert.equal(typeof h.v.startLive,'function');assert.equal(await h.v.startLive(),true);
  assert.equal(h.sent.length,1);assert.deepEqual(h.draws[0].slice(1),[0,0,640,360]);
  h.accept();h.clock.advance(4999);assert.equal(h.sent.length,1);h.clock.advance(1);assert.equal(h.sent.length,2);
  h.clock.advance(20000);assert.equal(h.sent.length,2,'unacknowledged frame holds next frames');
  h.accept();h.clock.advance(5000);assert.equal(h.draws.length,3);
  h.v.stop();h.clock.advance(20000);assert.equal(h.draws.length,3);assert.equal(h.track.stopped,true);assert.equal(h.clock.jobs.size,0);
});
test('LIVE-03 loop skips unsafe capture states and terminates on lost session or deadline', async()=>{
  for(const state of ['pending','busy','speaking','capturing','buffered','notready','visionbusy']) {
    const h=liveHarness();await h.v.startLive();h.accept();
    if(state==='pending')h.core.pending={};
    if(state==='busy')h.core.busy=1;
    if(state==='speaking')h.core.receive({type:'input_audio_buffer.speech_started',item_id:'speech'});
    if(state==='capturing')h.core.receive({type:'input_audio_buffer.speech_started',item_id:'speech'});
    if(state==='buffered')h.options.canSend=()=>false;
    if(state==='notready')h.preview.readyState=0;
    if(state==='visionbusy')h.v.busy=true;
    h.clock.advance(20000);assert.equal(h.draws.length,1,state);
    h.core.pending=null;h.core.busy=0;h.v.busy=false;h.options.canSend=()=>true;h.preview.readyState=2;
    h.core.receive({type:'input_audio_buffer.speech_stopped'});
    if(state==='speaking'||state==='capturing') {
      h.clock.advance(5000);assert.equal(h.draws.length,1,'wait for final transcript');
      h.core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'speech',transcript:'Hello'});
    }
    h.clock.advance(5000);assert.equal(h.draws.length,2,state+' resumes');h.v.stop();
  }
  for(const end of ['hidden','closed','disconnected','deadline']) {
    const h=liveHarness();await h.v.startLive();h.accept();
    if(end==='hidden')h.doc.hidden=true;
    if(end==='closed')h.core.closed=true;
    if(end==='disconnected')h.options.ready=()=>false;
    h.clock.advance(end==='deadline'?3300000:5000);
    assert.equal(h.v.live,false,end);assert.equal(h.track.stopped,true,end);assert.equal(h.clock.jobs.size,0,end);
  }
});
test('LIVE-04 exact final user controls start watching, stop through approvals, and live queries do not recapture', async()=>{
  let starts=0,stops=0,captures=0;
  const {core,sent,requests}=controller({startWatching:()=>{starts++;},stopWatching:()=>{stops++;},liveEnabled:()=>true,cameraEnabled:()=>true,capture:()=>{captures++;}});
  const say=(text,id)=>{core.receive({type:'input_audio_buffer.speech_started',item_id:id});core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:id,transcript:text});};
  core.receive({type:'input_audio_buffer.speech_started',item_id:'partial'});
  core.receive({type:'conversation.item.input_audio_transcription.delta',item_id:'partial',delta:'see me'});
  core.receive({type:'response.output_audio_transcript.done',transcript:'see me'});assert.equal(starts,0);
  for(const [i,text] of ['Jarvis see me','see me','start watching','Jarvis start watching'].entries()) {
    say(text,'start'+i);assert.equal(starts,i+1);assert.equal(core.turns.get('start'+i).blocked,true);
    core.receive({type:'response.created',response:{id:'late'+i}});assert.equal(sent.at(-2).type,'response.cancel');
  }
  for(const text of ['please see me','see me tomorrow','start watching the news'])say(text,text);
  assert.equal(starts,4);say('what do you see','question');assert.equal(captures,0);assert.equal(core.turns.get('question').blocked,false);
  core.setConfirmation({session_id:'s',confirmation:{id:'a'}},core.epoch);say('see me','blocked');assert.equal(starts,4);
  for(const text of ['stop watching','Jarvis stop watching','camera off'])say(text,text);
  assert.equal(stops,3);assert.ok(core.pending);assert.equal(requests.length,0);
});
test('LIVE-05 mounted opt-in notice and buttons share quietly with a separate live indicator', async()=>{
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  for(const id of ['start-live-view','stop-live-view','live-view-notice','live-view-status'])assert.ok(html.includes(`id="${id}"`),id);
  assert.match(html,/frame every 5 seconds.*AI provider/i);assert.match(html,/changes between frames can be missed/i);
  assert.match(html,/Stop watching.*(?:shuts|turns).*camera/i);assert.match(html,/processing.*cost/i);
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
  const clock=fakeClock();Object.assign(h.app.vision,{now:clock.now,schedule:clock.setTimeout,cancelTimer:clock.clearTimeout});
  const camera={stop(){this.stopped=true;}};h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[camera]});
  Object.assign(h.elements.get('camera-preview'),{videoWidth:1280,videoHeight:720,readyState:2,play:async()=>{},pause(){}});
  h.document.createElement=tag=>tag==='canvas'?{getContext:()=>({drawImage(){}}),toDataURL:()=> 'data:image/jpeg;base64,/9j/AA=='}:new Element();
  const events=[];h.dc.bufferedAmount=0;h.dc.send=data=>events.push(JSON.parse(data));
  try {
    assert.equal(h.elements.get('start-live-view').disabled,false);
    await h.elements.get('start-live-view').fire('click');assert.equal(h.app.vision.live,true);
    assert.equal(events.length,1);assert.equal(events[0].item.content[1].type,'input_image');
    assert.match(h.elements.get('live-view-status').textContent,/1 frame.*last sent/i);
    assert.equal(h.elements.get('stop-live-view').disabled,false);assert.equal(h.elements.get('start-live-view').disabled,false);assert.equal(h.elements.get('start-live-view')['aria-pressed'],'true');
    await h.elements.get('stop-live-view').fire('click');assert.equal(camera.stopped,true);assert.equal(h.app.vision.live,false);
    assert.match(h.elements.get('live-view-status').textContent,/off/i);assert.equal(clock.jobs.size,0);
    const core=h.app.session.core;core.receive({type:'input_audio_buffer.speech_started',item_id:'voice'});
    core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'voice',transcript:'Jarvis see me'});
    await delay(0);assert.equal(h.app.vision.live,true,'voice is wired to live camera');
  } finally {await h.app.session.stop();}
});
test('LIVE-06 transport backpressure and disconnected peer prevent frames before capture and send', async()=>{
  const h=transportHarness();h.dc.bufferedAmount=512*1024;const events=[];h.dc.send=data=>events.push(JSON.parse(data));
  const s=new (load().VoiceSession)(h.options);await s.start();
  try {
    assert.equal(typeof s.canSendFrame,'function');assert.equal(s.canSendFrame(),false);
    assert.equal(s.core.liveFrame('data:image/jpeg;base64,/9j/AA=='),false);assert.equal(events.length,0);
    h.dc.bufferedAmount=0;assert.equal(s.canSendFrame(),true);
    h.dc.readyState='closing';assert.equal(s.canSendFrame(),false);h.dc.readyState='open';
    h.peer.connectionState='disconnected';assert.equal(s.canSendFrame(),false);h.peer.connectionState='connected';
    assert.equal(s.core.liveFrame('data:image/jpeg;base64,/9j/AA=='),true);
    assert.equal(events.length,1);
  } finally {await s.stop();}
  for(const state of ['closed','pending','busy','speaking','capturing']) {
    const {core,sent}=controller();core[state]=true;
    assert.equal(core.liveFrame('data:image/jpeg;base64,/9j/AA=='),false,state);assert.equal(sent.length,0);
  }
  const {core}=controller({send:()=>false});assert.equal(core.liveFrame('data:image/jpeg;base64,/9j/AA=='),false);assert.equal(core.liveInFlight,null);
});
test('LIVE-07 permission races, denial and network loss shut watching down without automatic restart', async()=>{
  for(const phase of ['permission','play','resolved']) {
    const h=liveHarness();let resolve;
    if(phase==='permission')h.win.navigator.mediaDevices.getUserMedia=()=>new Promise(r=>{resolve=r;});
    if(phase==='play')h.preview.play=()=>new Promise(r=>{resolve=r;});
    if(phase==='resolved')h.v.enableCamera=async()=>{h.v.stream=h.stream;h.v.stop();return true;};
    const start=h.v.startLive();
    if(phase!=='permission')await Promise.resolve();
    h.v.stop();resolve?.(phase==='permission'?h.stream:undefined);
    assert.equal(await start,false,phase);assert.equal(h.v.live,false);assert.equal(h.track.stopped,true);assert.equal(h.clock.jobs.size,0);
  }
  const h=liveHarness(),statuses=[];h.v.status=t=>statuses.push(t);
  h.win.navigator.mediaDevices.getUserMedia=async()=>{throw Object.assign(Error('gesture'),{name:'NotAllowedError'});};
  assert.equal(await h.v.startLive(),false);assert.match(statuses.at(-1),/tap.*Start live view/i);
  const t=transportHarness();let cleanups=0;t.options.cleanup=()=>cleanups++;
  const s=new (load().VoiceSession)(t.options);await s.start();
  t.peer.connectionState='disconnected';t.peer.onconnectionstatechange();assert.equal(cleanups,1,'immediate camera teardown, not grace timer');
  t.peer.connectionState='connected';t.peer.onconnectionstatechange();assert.equal(cleanups,1);await s.stop();
});
test('LIVE-08 passive frames cannot initiate tools and later audio forwards only raw user text', async()=>{
  let starts=0;const {core,requests,sent}=controller({startWatching:()=>starts++,liveEnabled:()=>true});
  core.beginTurn('old');core.turns.get('old').text='old weather';
  core.liveFrame('data:image/jpeg;base64,/9j/AA==');
  core.receive({type:'response.created',response:{id:'autonomous'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'autonomous',call_id:'bad',name:'ask_jarvis',arguments:'{"request":"image text"}'});
  assert.equal(requests.length,0);
  core.receive({type:'input_audio_buffer.speech_started',item_id:'fresh'});
  core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'fresh',transcript:' Weather in Delhi? '});
  core.receive({type:'response.created',response:{id:'fresh-r'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'fresh-r',call_id:'good',name:'ask_jarvis',arguments:'{"request":"private image description"}'});
  assert.equal(requests.length,1);assert.equal(requests[0].body.user_text,' Weather in Delhi? ');
  assert.deepEqual(requests[0].body.arguments,{request:' Weather in Delhi? '});
  core.receive({type:'input_audio_buffer.speech_started',item_id:'query'});
  core.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'query',transcript:'what do you see'});
  core.receive({type:'response.created',response:{id:'query-r'}});
  await core.receive({type:'response.function_call_arguments.done',response_id:'query-r',call_id:'query-call',name:'ask_jarvis',arguments:'{}'});
  await core.receive({type:'response.function_call_arguments.done',response_id:'query-r',call_id:'start-call',name:'start_watching',arguments:'{}'});
  assert.equal(requests.length,1);assert.equal(starts,0);
});
test('LIVE-09 rejected frames and failed pruning stop sharing; missing deletes are benign', async()=>{
  const h=liveHarness(),statuses=[];h.v.status=t=>statuses.push(t);h.core.stopWatching=()=>h.v.stop();h.core.status=t=>statuses.push(t);
  await h.v.startLive();const first=h.sent[0];
  h.core.receive({type:'error',error:{event_id:first.event_id,code:'invalid_value',message:'image unsupported'}});
  assert.equal(h.v.live,false);assert.equal(h.track.stopped,true);assert.equal(h.core.liveInFlight,null);assert.match(statuses.at(-1),/rejected|unavailable/i);
  const c=controller({status:t=>statuses.push(t)});
  for(let i=0;i<3;i++){c.core.liveFrame('data:image/jpeg;base64,/9j/AA==');const frame=c.sent.filter(e=>e.item?.role==='user').at(-1);c.core.receive({type:'conversation.item.created',item:frame.item});}
  const deletion=c.sent.find(e=>e.type==='conversation.item.delete');assert.ok(deletion.event_id);
  const count=statuses.length;c.core.receive({type:'error',error:{event_id:deletion.event_id,code:'item_not_found',message:'Item missing'}});assert.equal(statuses.length,count);
  let stopped=0;c.core.stopWatching=()=>stopped++;
  c.core.liveFrame('data:image/jpeg;base64,/9j/AA==');c.core.receive({type:'conversation.item.created',item:c.sent.filter(e=>e.item?.role==='user').at(-1).item});
  const badDelete=c.sent.filter(e=>e.type==='conversation.item.delete').at(-1);
  c.core.receive({type:'error',error:{event_id:badDelete.event_id,code:'server_error',message:'Cannot delete'}});
  assert.equal(stopped,1);assert.match(statuses.at(-1),/context|sharing stopped/i);
});
test('LIVE-10 failed capture/send shuts camera down and pending deletes bound provider context', async()=>{
  for(const failure of ['draw','send']) {
    const h=liveHarness();
    if(failure==='draw')h.doc.createElement=()=>({getContext:()=>({drawImage(){throw Error('camera lost');}})});
    else h.core.send=()=>{throw Error('channel closed');};
    assert.equal(await h.v.startLive(),false);assert.equal(h.v.live,false);assert.equal(h.track.stopped,true);assert.equal(h.clock.jobs.size,0);
  }
  const {core,sent}=controller();const jpeg='data:image/jpeg;base64,/9j/AA==';
  for(let i=0;i<3;i++){core.liveFrame(jpeg);core.receive({type:'conversation.item.created',item:sent.filter(e=>e.item?.role==='user').at(-1).item});}
  assert.equal(core.liveFrame(jpeg),false,'wait for deletion acknowledgement rather than grow context');
  core.receive({type:'conversation.item.deleted',item_id:sent.find(e=>e.type==='conversation.item.delete').item_id});assert.equal(core.liveFrame(jpeg),true);
});
test('LIVE-11 live lifecycle cleanup covers hidden/pagehide/auth/end/channel and reconnect never resumes', async()=>{
  for(const end of ['hidden','pagehide','auth','end','channel','network']) {
    const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
    const clock=fakeClock();Object.assign(h.app.vision,{now:clock.now,schedule:clock.setTimeout,cancelTimer:clock.clearTimeout});
    const camera={stop(){this.stopped=true;}};h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[camera]});
    Object.assign(h.elements.get('camera-preview'),{readyState:0,play:async()=>{},pause(){}});
    await h.elements.get('start-live-view').fire('click');assert.equal(h.app.vision.live,true);
    if(end==='hidden'){h.document.hidden=true;h.document.listeners.visibilitychange();}
    if(end==='pagehide')h.window.listeners.pagehide();
    if(end==='auth'){h.window.fetch=async()=>({status:401,ok:false});await h.app.refresh();}
    if(end==='end')await h.app.session.stop();
    if(end==='channel')h.dc.onclose();
    if(end==='network'){h.peer.connectionState='disconnected';h.peer.onconnectionstatechange();}
    assert.equal(h.app.vision.live,false,end);assert.equal(camera.stopped,true,end);assert.equal(clock.jobs.size,0,end);
    h.document.hidden=false;h.document.listeners.visibilitychange();h.peer.connectionState='connected';h.peer.onconnectionstatechange();clock.advance(20000);
    assert.equal(h.app.vision.live,false,end);await h.app.session.stop();
  }
  const css=fs.readFileSync(path.join(__dirname,'../static/realtime.css'),'utf8');
  assert.match(css,/\.live-view-status\s*\{[^}]*border/);
});
test('LIVE-12 stale start cannot attach to replacement camera and disconnected sessions cannot start watching', async()=>{
  const h=liveHarness();let resolve;
  h.v.enableCamera=()=>new Promise(r=>{resolve=r;});
  const starting=h.v.startLive();h.v.stop();h.v.stream=h.stream;resolve(true);
  assert.equal(await starting,false);assert.equal(h.v.live,false);assert.equal(h.clock.jobs.size,0);
  const m=mountedHarness();await delay(0);await m.elements.get('login-form').fire('submit');await m.elements.get('start-conversation').fire('click');
  m.peer.connectionState='disconnected';m.peer.onconnectionstatechange();
  assert.equal(await m.app.vision.startLive(),false);assert.equal(m.mediaCalls.length,1);await m.app.session.stop();
});
test('LIVE-13 duplicate final transcripts do not restart a stopped watch or repeat a local control', ()=>{
  let starts=0;const {core}=controller({startWatching:()=>starts++});
  core.receive({type:'input_audio_buffer.speech_started',item_id:'start'});
  const final={type:'conversation.item.input_audio_transcription.completed',item_id:'start',transcript:'see me'};
  core.receive(final);assert.equal(starts,1);core.receive(final);assert.equal(starts,1);
});
test('UI camera and live buttons reflect resources and second click stops them', async()=>{
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
  const track={stop(){this.stopped=true;}};
  h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[track]});
  Object.assign(h.elements.get('camera-preview'),{readyState:0,play:async()=>{},pause(){}});
  try {
    for(const id of ['enable-camera','start-live-view']) {
      const button=h.elements.get(id);assert.equal(button['aria-pressed'],'false');
      await button.fire('click');assert.equal(button['aria-pressed'],'true');assert.equal(button.disabled,false);
      await button.fire('click');assert.equal(button['aria-pressed'],'false');assert.equal(h.app.vision.stream,null);assert.equal(track.stopped,true);
    }
  } finally {await h.app.session.stop();}
});
test('UI doubleclick trailing event cannot end the session just started', async()=>{
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');
  try {
    await h.elements.get('start-conversation').fire('click',1);
    await h.elements.get('end-conversation').fire('click',2);
    assert.equal(h.app.session.running,true);
    assert.equal(h.fetches.filter(r=>r.url==='/api/realtime/connect').length,1);
    await h.elements.get('end-conversation').fire('click',1);
    assert.equal(h.app.session.running,false);
  } finally {await h.app.session.stop();}
});
test('UI compact operate surface has purpose hints and disclosed settings', ()=>{
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  assert.match(html,/<h1[^>]*>Jarvis<\/h1>/);
  assert.match(html,/<details[^>]*id="settings-panel"/);
  assert.ok(html.indexOf('id="enable-camera"') < html.indexOf('id="confirmation-panel"'));
  for(const tag of html.matchAll(/<(?:button|a|summary)\b[^>]*>/g)) assert.match(tag[0],/title="[^"]+"/,tag[0]);
  for(const id of ['execution-target','controls-help']) assert.ok(html.includes('id="'+id+'"'));
  const css=fs.readFileSync(path.join(__dirname,'../static/realtime.css'),'utf8');
  assert.match(css,/\[aria-pressed="true"\]/);
});
test('UI denied or cancelled camera permission never selects toggles; pending approval keeps Stop available', async()=>{
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
  const camera=h.elements.get('enable-camera'),live=h.elements.get('start-live-view');
  try {
    h.window.navigator.mediaDevices.getUserMedia=async()=>{throw Error('denied');};
    await camera.fire('click');assert.equal(camera['aria-pressed'],'false');assert.equal(live['aria-pressed'],'false');
    let resolve;const track={stop(){this.stopped=true;}};
    h.window.navigator.mediaDevices.getUserMedia=()=>new Promise(r=>resolve=r);
    const pending=camera.fire('click');assert.equal(camera['aria-pressed'],'false');assert.equal(h.elements.get('stop-camera').disabled,false);
    await h.elements.get('stop-camera').fire('click');resolve({getTracks:()=>[track]});await pending;
    assert.equal(track.stopped,true);assert.equal(camera['aria-pressed'],'false');
    h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[track]});Object.assign(h.elements.get('camera-preview'),{play:async()=>{},pause(){}});
    await camera.fire('click');h.app.session.core.pending={};h.app.vision.changed();assert.equal(camera.disabled,false);assert.equal(live.disabled,true);
    await camera.fire('click');assert.equal(h.app.vision.stream,null);
  } finally {h.app.session.core.pending=null;await h.app.session.stop();}
});
test('UI target reflects verified local desktop and mute explicitly names its state', async()=>{
  const h=await handsfreeHarness();
  try {
    assert.match(h.elements.get('execution-target').textContent,/local.*desktop/i);
    await h.elements.get('start-conversation').fire('click');await h.elements.get('mic-toggle').fire('click');
    assert.equal(h.elements.get('mic-toggle').textContent,'Mute: On');assert.equal(h.elements.get('mic-toggle')['aria-pressed'],'true');
    await h.app.session.stop();assert.equal(h.elements.get('mic-toggle').textContent,'Mute: Off');
    h.window.location.hostname='public.example';await h.app.refresh();assert.match(h.elements.get('execution-target').textContent,/server.*not this device/i);
  } finally {await h.app.session.stop();}
});
test('UI focus exposes purpose and trailing doubleclick cannot start or approve', async()=>{
  const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');
  try {
    h.document.listeners.focusin?.({target:{closest:()=>({title:'Start the microphone explicitly.'})}});
    assert.equal(h.elements.get('controls-help').textContent,'Start the microphone explicitly.');
    await h.elements.get('start-conversation').fire('click',2);assert.equal(h.mediaCalls.length,0);
    await h.elements.get('start-conversation').fire('click',1);
    h.app.session.core.setConfirmation({session_id:'x',confirmation:{id:'x',tool:'send_email',arguments:{},spoken_summary:'review'}},0);
    await h.elements.get('approve-action').fire('click',2);await delay(0);assert.equal(h.fetches.filter(r=>r.url==='/api/confirm').length,0);
  } finally {await h.app.session.stop();}
});
test('UI camera hardware ending clears actual selected state',async()=>{
 const h=mountedHarness();await delay(0);await h.elements.get('login-form').fire('submit');await h.elements.get('start-conversation').fire('click');
 let ended;const track={stop(){},addEventListener(name,fn){if(name==='ended')ended=fn;}};
 h.window.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[track]});Object.assign(h.elements.get('camera-preview'),{play:async()=>{},pause(){}});
 try {await h.elements.get('enable-camera').fire('click');assert.equal(typeof ended,'function');ended();assert.equal(h.elements.get('enable-camera')['aria-pressed'],'false');assert.equal(h.app.vision.stream,null);}finally{await h.app.session.stop();}
});
test('exact local controls never match ordinary requests', () => {
  const {localControl} = load();
  assert.equal(typeof localControl, 'function', 'native frontend local controls exist');
  assert.deepEqual(localControl('Stop Jarvis!'), {type:'end',standby:true});
  assert.deepEqual(localControl('stop'), {type:'interrupt'});
  assert.deepEqual(localControl('रुको'), {type:'interrupt'});
  assert.deepEqual(localControl('Jarvis set volume to 42 percent'), {type:'volume', value:42});
  assert.equal(localControl('please stop the server'), null);
  assert.equal(localControl('send volume to 42 percent to Bob'), null);
});
