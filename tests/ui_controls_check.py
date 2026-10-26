"""Browser UI regression with real DOM/media tracks; API responses are simulated."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];BASE=os.getenv('JARVIS_UI_TEST_URL','http://127.0.0.1:8765')
INIT=r"""(()=>{
 window.uiTest={mic:[],cam:[],sent:[]};
 navigator.mediaDevices.getUserMedia=async constraints=>{if(constraints.video){const c=document.createElement('canvas');c.width=640;c.height=360;const x=c.getContext('2d');x.fillStyle='teal';x.fillRect(0,0,640,360);const s=c.captureStream(1);uiTest.cam.push(s);return s;}const a=new AudioContext();await a.resume();const d=a.createMediaStreamDestination();uiTest.mic.push(d.stream);return d.stream;};
 class Peer extends EventTarget {constructor(){super();this.connectionState='new';this.iceGatheringState='complete';this.tracks=[];uiTest.peer=this;}addTrack(t){this.tracks.push(t);}createDataChannel(){const d={readyState:'connecting',bufferedAmount:0,send(data){uiTest.sent.push(JSON.parse(data));},close(){this.readyState='closed';this.onclose?.();}};this.dc=d;return d;}async createOffer(){return {type:'offer',sdp:'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'};}async setLocalDescription(d){this.localDescription=d;}async setRemoteDescription(){this.connectionState='connected';this.dc.readyState='open';this.onconnectionstatechange?.();this.dc.onopen?.();this.dc.onmessage?.({data:JSON.stringify({type:'session.created'})});}close(){this.connectionState='closed';}}
 window.RTCPeerConnection=Peer;
})();"""
with sync_playwright() as p:
    browser=p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
    ctx=browser.new_context(viewport={'width':1280,'height':900});ctx.add_init_script(INIT)
    counts={}
    def api(route):
        path=route.request.url.split('/api/',1)[1];counts[path]=counts.get(path,0)+1
        if path=='realtime/status':body={'enabled':True,'model':'test-model','max_seconds':3300,'session':None}
        elif path=='desktop/status':body={'enabled':False,'wake_available':False}
        elif path=='realtime/connect':body={'id':'ui-test-session','sdp':'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n','max_seconds':3300}
        else:body={'ok':True}
        route.fulfill(json=body)
    ctx.route('**/api/**',api)
    page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(BASE)
    expect(page.locator('#start-conversation')).to_be_enabled()
    page.locator('#start-conversation').dblclick()
    expect(page.locator('#end-conversation')).to_be_visible()
    assert counts.get('realtime/connect')==1,counts
    page.locator('#mic-toggle').click();expect(page.locator('#mic-toggle')).to_have_attribute('aria-pressed','true')
    assert page.evaluate('uiTest.mic[0].getAudioTracks().every(t=>!t.enabled)')
    page.locator('#mic-toggle').click();expect(page.locator('#mic-toggle')).to_have_attribute('aria-pressed','false')
    page.locator('#enable-camera').click();expect(page.locator('#enable-camera')).to_have_attribute('aria-pressed','true')
    page.locator('#enable-camera').click();expect(page.locator('#enable-camera')).to_have_attribute('aria-pressed','false')
    assert page.evaluate('uiTest.cam[0].getTracks().every(t=>t.readyState==="ended")')
    page.locator('#start-live-view').click();expect(page.locator('#start-live-view')).to_have_attribute('aria-pressed','true')
    page.locator('#start-live-view').click();expect(page.locator('#start-live-view')).to_have_attribute('aria-pressed','false')
    assert page.evaluate('uiTest.cam.every(s=>s.getTracks().every(t=>t.readyState==="ended"))')
    page.locator('#interrupt').click();assert page.evaluate('uiTest.sent.some(e=>e.type==="output_audio_buffer.clear")')
    page.locator('#end-conversation').click();expect(page.locator('#start-conversation')).to_be_visible()
    assert page.evaluate('uiTest.mic.every(s=>s.getTracks().every(t=>t.readyState==="ended"))')
    for width in [1280,390,320]:
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    purposes=page.evaluate("()=>Array.from(document.querySelectorAll('button')).filter(e=>e.getClientRects().length && !e.title && !e.getAttribute('aria-describedby')).map(e=>e.id)")
    assert not purposes,purposes
    page.screenshot(path=str(ROOT/'data/ui-controls-mobile.png'),full_page=True)
    assert not errors,errors
    print(json.dumps({'api':'simulated','media_tracks':'real synthetic tracks','start_double_click':'single connection','mic_camera_live_toggles':'passed','cleanup':'passed','widths':[1280,390,320],'purpose_labels':'passed','page_errors':errors}))
    ctx.close();browser.close()
