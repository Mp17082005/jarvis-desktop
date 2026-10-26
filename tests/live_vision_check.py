"""Live deployed image upload -> native spoken visual answer, no physical camera."""
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
with sync_playwright() as p:
    browser=p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
    context=browser.new_context(viewport={'width':390,'height':844});context.add_init_script(INIT)
    assert context.request.post(BASE+'/api/login',data={'token':c['JARVIS_ACCESS_TOKEN']},headers={'Origin':BASE}).status==200
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE);expect(page.locator('#start-conversation')).to_be_enabled();page.locator('#start-conversation').click()
    def wait(js,timeout=50):
        end=time.monotonic()+timeout
        while not page.evaluate(js):
            if time.monotonic()>end:raise AssertionError(js)
            page.wait_for_timeout(150)
    try:
        wait("()=>voiceTest.events.some(e=>e.type==='session.created')")
        page.locator('#image-upload').set_input_files(str(ROOT/'data/vision-probe/image.png'))
        expect(page.locator('#show-jarvis')).to_be_enabled(timeout=15000)
        page.locator('#vision-question').fill('Describe the colored shapes and read the label. Be brief.')
        page.locator('#show-jarvis').click()
        wait("()=>voiceTest.events.some(e=>e.type==='response.output_audio_transcript.done')")
        wait("()=>voiceTest.events.some(e=>e.type==='output_audio_buffer.stopped')")
        report=page.evaluate("async()=>{const stats=[];for(const s of (await voiceTest.peer.getStats()).values())if(s.type==='inbound-rtp')stats.push({bytes:s.bytesReceived,energy:s.totalAudioEnergy});return {transcripts:voiceTest.events.filter(e=>e.transcript).map(e=>e.transcript),stats,provider_errors:voiceTest.events.filter(e=>e.code)};}")
        text=' '.join(report['transcripts']).lower()
        assert 'blue' in text and 'red' in text and '472' in text,report
        assert any(s.get('bytes',0)>0 for s in report['stats'])
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.locator('#end-conversation').click();wait("()=>voiceTest.peer.connectionState==='closed'",10)
        report['page_errors']=errors;report['mobile_overflow']=False
        (ROOT/'data/live-vision-report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2));assert not errors
    finally:
        try:page.locator('#end-conversation').click(timeout=500)
        except Exception:pass
        context.close();browser.close()
