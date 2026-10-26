"""Real public app live-view integration; synthetic camera, real provider."""
import ast,json,os,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];BASE=os.getenv('JARVIS_TEST_URL','http://127.0.0.1:8765')
tree=ast.parse((ROOT/'tests/live_realtime_check.py').read_text())
INIT=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='INIT' for t in n.targets))
c={}
for line in (ROOT/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1);c[k]=v.strip().strip("'\"")
CAMERA="""(()=>{const original=navigator.mediaDevices.getUserMedia;navigator.mediaDevices.getUserMedia=async constraints=>{if(!constraints.video)return original(constraints);const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;const x=canvas.getContext('2d');x.fillStyle='purple';x.fillRect(0,0,640,360);x.fillStyle='white';x.font='bold 52px Arial';x.fillText('LIVE 628',80,200);const stream=canvas.captureStream(2);window.cameraTest={stream,canvas};return stream;};const originalCreate=RTCPeerConnection.prototype.createDataChannel;RTCPeerConnection.prototype.createDataChannel=function(...args){const dc=originalCreate.apply(this,args);const send=dc.send.bind(dc);window.sentFrames=0;dc.send=data=>{const e=JSON.parse(data);if(e.item?.content?.some(c=>c.type==='input_image'))window.sentFrames++;return send(data);};return dc;};})();"""
with sync_playwright() as p:
    browser=p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required']);ctx=browser.new_context(viewport={'width':390,'height':844});ctx.add_init_script(INIT+'\n'+CAMERA)
    assert ctx.request.post(BASE+'/api/login',data={'token':c['JARVIS_ACCESS_TOKEN']},headers={'Origin':BASE}).status==200
    page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(BASE)
    def wait(js,seconds=45):
        deadline=time.monotonic()+seconds
        while not page.evaluate(js):
            if time.monotonic()>deadline:raise AssertionError(js)
            page.wait_for_timeout(150)
    try:
        expect(page.locator('#start-conversation')).to_be_enabled();page.locator('#start-conversation').click()
        wait("()=>voiceTest.events.some(e=>e.type==='session.created')")
        page.locator('#start-live-view').click()
        wait('()=>window.sentFrames>=2',25)
        assert page.evaluate("voiceTest.events.filter(e=>e.type==='response.created').length")==0,'Passive camera frames spoke unprompted'
        page.locator('#command').evaluate('(el)=>{let p=el.parentElement;while(p){if(p.tagName==="DETAILS")p.open=true;p=p.parentElement;}}')
        page.locator('#command').fill('What color is the current camera image and what text does it show?')
        page.locator('#composer').evaluate('(form)=>form.requestSubmit()')
        wait("()=>voiceTest.events.some(e=>e.type==='output_audio_buffer.stopped')")
        spoken=page.evaluate("voiceTest.events.filter(e=>e.type==='response.output_audio_transcript.done').map(e=>e.transcript)")
        assert '628' in ' '.join(spoken) and 'purple' in ' '.join(spoken).lower(),spoken
        page.locator('#stop-live-view').click()
        wait("()=>cameraTest.stream.getTracks().every(t=>t.readyState==='ended')",5)
        count=page.evaluate('sentFrames');page.wait_for_timeout(5500);assert page.evaluate('sentFrames')==count
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        report={'frames_shared':count,'spoken_answer':spoken,'no_unprompted_narration':True,'stop_ends_camera_and_sharing':True,'page_errors':errors,'physical_camera':False}
        (ROOT/'data/live-watch-report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2));assert not errors
    except Exception:
        print('DIAGNOSTIC',page.evaluate('({body:document.body.innerText,frames:window.sentFrames,camera:!!window.cameraTest,events:voiceTest.events})'), 'ERRORS',errors)
        raise
    finally:
        try:
            page.locator('#end-conversation').click(timeout=1000)
            wait('()=>voiceTest.ends.length>0',15)
        except Exception:pass
        ctx.close();browser.close()
