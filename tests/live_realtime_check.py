"""Actual deployed realtime UI + real prerecorded speech + provider audio."""
import base64,json,os,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
BASE=os.getenv('JARVIS_TEST_URL','http://127.0.0.1:8765')
c={}
for line in (ROOT/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1);c[k]=v.strip().strip("'\"")
AUDIO=base64.b64encode((ROOT/'data/realtime-input-question.mp3').read_bytes()).decode()
INIT="""(() => {
 window.voiceTest={events:[],errors:[],tools:[],ends:[]};
 const originalFetch=window.fetch.bind(window);
 window.fetch=async (...args)=>{const r=await originalFetch(...args);const url=String(args[0]);if(url.includes('/api/realtime/tool')){const d=await r.clone().json();voiceTest.tools.push({status:r.status,text:d.text,results:d.results});}if(url.includes('/api/realtime/end'))voiceTest.ends.push(await r.clone().json());return r;};
 navigator.mediaDevices.getUserMedia=async ()=>{const ctx=new AudioContext();await ctx.resume();const dest=ctx.createMediaStreamDestination();const osc=ctx.createOscillator();const gain=ctx.createGain();gain.gain.value=0;osc.connect(gain).connect(dest);osc.start();voiceTest.ctx=ctx;voiceTest.dest=dest;voiceTest.osc=osc;return dest.stream;};
 const Original=window.RTCPeerConnection;
 window.RTCPeerConnection=class extends Original {
  constructor(...args){super(...args);voiceTest.peer=this;this.addEventListener('track',e=>{voiceTest.remote=new MediaStream([e.track]);voiceTest.chunks=[];voiceTest.recorder=new MediaRecorder(voiceTest.remote);voiceTest.recorder.ondataavailable=e=>{if(e.data.size)voiceTest.chunks.push(e.data);};voiceTest.recorder.start(200);});}
  createDataChannel(...args){const dc=super.createDataChannel(...args);voiceTest.dc=dc;dc.addEventListener('message',e=>{const v=JSON.parse(e.data);voiceTest.events.push({type:v.type,transcript:v.transcript,name:v.name,code:v.error?.code});});return dc;}
 };
 window.playQuestion=async encoded=>{const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));const buffer=await voiceTest.ctx.decodeAudioData(bytes.buffer);const src=voiceTest.ctx.createBufferSource();src.buffer=buffer;src.connect(voiceTest.dest);src.start();};
})();"""
with sync_playwright() as p:
    browser=p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
    context=browser.new_context(viewport={'width':1280,'height':900})
    context.add_init_script(INIT)
    r=context.request.post(BASE+'/api/login',data={'token':c['JARVIS_ACCESS_TOKEN']},headers={'Origin':BASE});assert r.status==200
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE)
    expect(page.locator('#start-conversation')).to_be_enabled(timeout=15000)
    page.locator('#start-conversation').click()
    def wait(predicate,seconds=45):
        deadline=time.monotonic()+seconds
        while not page.evaluate(predicate):
            if time.monotonic()>deadline:raise AssertionError('Live condition timed out: '+predicate)
            page.wait_for_timeout(150)
    try:
        wait("() => window.voiceTest.events.some(e=>e.type==='session.created')")
        page.wait_for_timeout(2000)
        page.evaluate('(audio)=>playQuestion(audio)',AUDIO)
        wait("() => voiceTest.events.some(e=>e.type==='conversation.item.input_audio_transcription.completed')",40)
        wait("() => voiceTest.tools.length>0",180)
        wait("() => voiceTest.events.filter(e=>e.type==='response.output_audio_transcript.done').length>=2",60)
        wait("() => voiceTest.events.filter(e=>e.type==='output_audio_buffer.stopped').length>=2",60)
        report=page.evaluate("""async()=>{const p=voiceTest;const stats=[];for(const s of (await p.peer.getStats()).values())if(s.type==='inbound-rtp')stats.push({bytes:s.bytesReceived,packets:s.packetsReceived,energy:s.totalAudioEnergy});return {events:p.events,tools:p.tools,stats,peer:p.peer.connectionState};}""")
        assert report['tools'][0]['status']==200,report
        assert any(s.get('bytes',0)>0 for s in report['stats']),report
        # Exercise interruption while actual native speech is playing.
        previous_started=page.evaluate("voiceTest.events.filter(e=>e.type==='output_audio_buffer.started').length")
        page.locator('#command').evaluate('(el)=>{let p=el.parentElement;while(p){if(p.tagName==="DETAILS")p.open=true;p=p.parentElement;}}')
        page.locator('#command').fill('Tell me a long story about a friendly robot exploring a forest.')
        page.locator('#composer').evaluate('(form)=>form.requestSubmit()')
        wait(f"() => voiceTest.events.filter(e=>e.type==='output_audio_buffer.started').length>{previous_started}",40)
        page.locator('#interrupt').click()
        wait("() => voiceTest.events.some(e=>e.type==='output_audio_buffer.cleared')",10)
        report['live_interrupt']='provider audio buffer cleared during speech'
        page.locator('#end-conversation').click()
        wait("()=>voiceTest.peer.connectionState==='closed'",10)
        wait("()=>voiceTest.ends.length>0",20)
        report['end']=page.evaluate('voiceTest.ends');report['errors']=errors
        page.screenshot(path=str(ROOT/'data/realtime-live-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        report['mobile_overflow']=False
        (ROOT/'data/realtime-live-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
        assert not errors
    except Exception:
        print('DIAGNOSTIC',page.evaluate('({events:voiceTest.events,tools:voiceTest.tools,body:document.body.innerText})'))
        raise
    finally:
        try:page.locator('#end-conversation').click(timeout=1000)
        except Exception:pass
        context.close();browser.close()
