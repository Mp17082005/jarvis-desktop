"""Live, bounded Realtime WebRTC probe; not a mocked test.
Run: env -u PYTHONPATH uv tool run --from playwright python tests/probe_webrtc.py
Only ephemeral credentials enter Chromium. No SDP or credentials are persisted.
"""
import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'probe-webrtc'
SESSION = {
    'type': 'realtime', 'model': 'gpt-realtime-2.1',
    'output_modalities': ['audio'],
    'instructions': 'You are a transport test. Speak only the short phrase requested, in English.',
    'max_output_tokens': 128,
    'audio': {'input': {'turn_detection': None}, 'output': {'voice': 'marin'}},
}
SETUP = r"""async () => {
 const p = window.probe = {events:[], sent:[], errors:[], transcripts:[], maxRms:0, chunks:[], tracks:[]};
 p.pc = new RTCPeerConnection();
 p.ctx = new AudioContext(); await p.ctx.resume();
 p.dest = p.ctx.createMediaStreamDestination();
 p.osc = p.ctx.createOscillator(); p.gain = p.ctx.createGain(); p.gain.gain.value=0;
 p.osc.connect(p.gain).connect(p.dest); p.osc.start();
 p.dest.stream.getTracks().forEach(t => p.pc.addTrack(t,p.dest.stream));
 p.pc.ontrack = e => {
  const stream = new MediaStream([e.track]); p.tracks.push({kind:e.track.kind});
  p.audio = new Audio(); p.audio.autoplay=true; p.audio.srcObject=stream;
  p.audio.play().catch(e => p.errors.push({playback:e.name}));
  p.source=p.ctx.createMediaStreamSource(stream); p.analyser=p.ctx.createAnalyser();
  p.source.connect(p.analyser); p.analyser.fftSize=2048;
  p.poll=setInterval(() => {const a=new Float32Array(2048);p.analyser.getFloatTimeDomainData(a);
   let s=0; for(const v of a)s+=v*v;p.maxRms=Math.max(p.maxRms,Math.sqrt(s/a.length));},20);
  p.recorder=new MediaRecorder(stream,{mimeType:'audio/webm;codecs=opus'});
  p.recorder.ondataavailable=e=>{if(e.data.size)p.chunks.push(e.data)};p.recorder.start(250);
 };
 p.dc=p.pc.createDataChannel('oai-events');
 p.dc.onmessage=e=>{
  const v=JSON.parse(e.data); const small={type:v.type,ms:Math.round(performance.now())};
  if(v.session)small.session={type:v.session.type,model:v.session.model,output_modalities:v.session.output_modalities,audio:v.session.audio};
  if(v.error){small.error={code:v.error.code,type:v.error.type,param:v.error.param};p.errors.push(small.error)}
  if(v.transcript){small.transcript=v.transcript;p.transcripts.push(v.transcript)}
  if(v.type==='response.function_call_arguments.done'){small.name=v.name;small.arguments=v.arguments;small.call_id=v.call_id;p.call=v;}
  if(v.response){small.response={status:v.response.status,status_details:v.response.status_details,usage:v.response.usage,
   output:(v.response.output||[]).map(x=>({type:x.type,name:x.name,call_id:x.call_id,arguments:x.arguments,
    content:(x.content||[]).map(c=>({type:c.type,transcript:c.transcript,text:c.text}))}))};}
  p.events.push(small);
 };
 p.send=v=>{p.sent.push(v);p.dc.send(JSON.stringify(v))};
 await p.pc.setLocalDescription(await p.pc.createOffer());
 await Promise.race([new Promise(resolve=>{if(p.pc.iceGatheringState==='complete')resolve();else p.pc.addEventListener('icegatheringstatechange',()=>{if(p.pc.iceGatheringState==='complete')resolve()})}),new Promise(r=>setTimeout(r,3000))]);
 return p.pc.localDescription.sdp;
}"""
FETCH = r"""async ({endpoint, token, offer}) => {
 try {
  const r=await fetch(endpoint,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/sdp'},body:offer,signal:AbortSignal.timeout(25000)});
  const text=await r.text();
  if(!r.ok){let err;try{err=JSON.parse(text).error}catch{};return {status:r.status,content_type:r.headers.get('content-type'),error:err?{code:err.code,type:err.type}:null};}
  await probe.pc.setRemoteDescription({type:'answer',sdp:text});
  return {status:r.status,content_type:r.headers.get('content-type'),answer_is_sdp:text.startsWith('v=0'),answer_has_audio:text.includes('m=audio')};
 }catch(e){return {error_name:e.name,error_message:e.message};}
}"""
EXERCISE = r"""async () => {
 const p=probe; const deadline=Date.now()+45000;
 const wait=async test=>{while(!test()){if(Date.now()>deadline)throw Error('event deadline');await new Promise(r=>setTimeout(r,50));}};
 try{
  await wait(()=>p.dc.readyState==='open'&&p.events.some(e=>e.type==='session.created'));
  p.send({type:'response.create',response:{output_modalities:['audio'],instructions:'Say exactly: Realtime audio connected.'}});
  await wait(()=>p.events.some(e=>e.type==='response.done'));
  await wait(()=>p.events.some(e=>e.type==='output_audio_buffer.stopped'));
  p.greetingRms=p.maxRms;
  p.send({type:'session.update',session:{type:'realtime',tools:[{type:'function',name:'echo_probe',description:'Return an explicit transport verification marker.',parameters:{type:'object',properties:{marker:{type:'string'}},required:['marker'],additionalProperties:false}}],tool_choice:'auto'}});
  await wait(()=>p.events.some(e=>e.type==='session.updated'));
  p.send({type:'response.create',response:{output_modalities:['audio'],tool_choice:{type:'function',name:'echo_probe'},instructions:'Call echo_probe with marker webrtc_verified. Do not speak yet.'}});
  await wait(()=>p.call&&p.events.filter(e=>e.type==='response.done').length>=2);
  const args=JSON.parse(p.call.arguments);
  if(p.call.name!=='echo_probe'||args.marker!=='webrtc_verified')throw Error('unexpected probe tool arguments');
  p.send({type:'conversation.item.create',item:{type:'function_call_output',call_id:p.call.call_id,output:JSON.stringify({marker:args.marker,verified:true})}});
  p.send({type:'response.create',response:{output_modalities:['audio'],tool_choice:'none',instructions:'Say exactly: Probe tool confirmed.'}});
  await wait(()=>p.events.filter(e=>e.type==='response.done').length>=3);
  await wait(()=>p.events.filter(e=>e.type==='output_audio_buffer.stopped').length>=2);
  await new Promise(r=>setTimeout(r,300));
 }catch(e){p.errors.push({exercise:e.message})}
 return true;
}"""
FINISH = r"""async () => {
 const p=probe; const stats=[];
 for(const s of (await p.pc.getStats()).values())if(['inbound-rtp','outbound-rtp'].includes(s.type)){
  const v={};for(const k of ['type','kind','bytesReceived','packetsReceived','bytesSent','packetsSent','totalAudioEnergy','totalSamplesReceived','audioLevel','totalSamplesDuration'])if(s[k]!==undefined)v[k]=s[k];stats.push(v);
 }
 if(p.recorder&&p.recorder.state!=='inactive')await new Promise(r=>{p.recorder.onstop=r;p.recorder.stop()});
 const blob=new Blob(p.chunks,{type:'audio/webm;codecs=opus'});
 const bytes=new Uint8Array(await blob.arrayBuffer());let binary='';for(const x of bytes)binary+=String.fromCharCode(x);
 const result={connection_state:p.pc.connectionState,ice_state:p.pc.iceConnectionState,data_channel:p.dc.readyState,
 audio_context:p.ctx.state,tracks:p.tracks,max_rms:p.maxRms,greeting_rms:p.greetingRms,rtp:stats,
 events:p.events,sent:p.sent,transcripts:p.transcripts,errors:p.errors,recording_bytes:bytes.length,recording_base64:btoa(binary)};
 clearInterval(p.poll);p.dest.stream.getTracks().forEach(t=>t.stop());p.osc.stop();p.dc.close();p.pc.close();
 if(p.audio){p.audio.pause();p.audio.srcObject=null;} await p.ctx.close();
 await new Promise(r=>setTimeout(r,200));
 result.closed={peer:p.pc.connectionState,data_channel:p.dc.readyState,audio_context:p.ctx.state};return result;
}"""


def config():
    values = {}
    for line in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('\"\'')
    return values


def mint(base, key):
    req = urllib.request.Request(base + '/realtime/client_secrets',
        data=json.dumps({'session': SESSION}).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
            return data['value'], {'status': r.status, 'response_keys': list(data),
                'session_model': data.get('session', {}).get('model')}
    except urllib.error.HTTPError as e:
        return None, {'status': e.code, 'error': 'client secret request failed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', choices=['both', 'gateway', 'direct'], default='both')
    args = parser.parse_args()
    c = config(); base = c['JARVIS_LLM_BASE_URL'].rstrip('/'); key = c['JARVIS_LLM_API_KEY']
    OUT.mkdir(parents=True, exist_ok=True)
    destinations = [('gateway', base + '/realtime/calls'), ('direct', 'https://api.openai.com/v1/realtime/calls')]
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'session_request': {'session': SESSION},
        'input': 'Synthetic silent WebAudio track; no physical microphone used.', 'attempts': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
        report['browser_version'] = browser.version
        try:
            for name, endpoint in destinations:
                if args.destination != 'both' and args.destination != name:
                    continue
                token, meta = mint(base, key)
                attempt = {'name': name, 'endpoint': endpoint, 'mint': meta}
                report['attempts'].append(attempt)
                if not token:
                    continue
                page = browser.new_page()
                page.route('http://127.0.0.1:18766/', lambda route: route.fulfill(status=200, content_type='text/html', body='<!doctype html><title>Bounded WebRTC probe</title>'))
                page.goto('http://127.0.0.1:18766/')
                start = time.monotonic()
                try:
                    offer = page.evaluate(SETUP)
                    attempt['exchange'] = page.evaluate(FETCH, {'endpoint': endpoint, 'token': token, 'offer': offer})
                    if attempt['exchange'].get('answer_is_sdp'):
                        page.evaluate(EXERCISE)
                    result = page.evaluate(FINISH)
                    recording = base64.b64decode(result.pop('recording_base64'))
                    if recording:
                        (OUT / (name + '.webm')).write_bytes(recording)
                        result['recording_path'] = str(OUT / (name + '.webm'))
                    attempt['result'] = result
                    attempt['audio_verified'] = bool(result['max_rms'] > 0.001 and any(s.get('bytesReceived', 0) > 0 for s in result['rtp']) and result['transcripts'] and recording)
                    attempt['tool_verified'] = any('Probe tool confirmed' in t for t in result['transcripts'])
                except Exception as e:
                    attempt['exception_type'] = type(e).__name__
                finally:
                    attempt['duration_seconds'] = round(time.monotonic() - start, 2)
                    page.close()
                    token = None
                (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                print(json.dumps({k: v for k, v in attempt.items() if k != 'result'}, indent=2), flush=True)
        finally:
            browser.close()
    report['verified'] = any(a.get('audio_verified') and a.get('tool_verified') for a in report['attempts'])
    (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'verified': report['verified'], 'report': str(OUT / 'report.json')}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
