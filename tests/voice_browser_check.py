"""Opt-in browser UI integration using simulated speech, not physical audio."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv('JARVIS_TEST_URL', 'http://127.0.0.1:8765')
config = {}
for line in (ROOT/'.env').read_text(encoding='utf-8').splitlines():
    if '=' in line and not line.startswith('#'):
        key, value = line.split('=', 1)
        config[key] = value.strip().strip("'\"")

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': 1280, 'height': 960})
    context.add_init_script("""
    window.voiceProbe = {spoken: [], cancelled: 0, listening: false};
    class Recognition {
        constructor() {window.testRecognition=this;}
        start() {window.voiceProbe.listening=true; this.onstart?.();}
        abort() {window.voiceProbe.listening=false; this.onend?.();}
        stop() {this.abort();}
    }
    Object.defineProperty(window, 'SpeechRecognition', {value: Recognition, configurable:true});
    Object.defineProperty(window, 'SpeechSynthesisUtterance', {value: class {constructor(text){this.text=text;}}, configurable:true});
    Object.defineProperty(window, 'speechSynthesis', {value: {
        cancel(){window.voiceProbe.cancelled++;},
        speak(u){window.voiceProbe.spoken.push({text:u.text,volume:u.volume});window.lastUtterance=u;u.onstart?.();},
        getVoices(){return [];}
    }, configurable:true});
    window.hear = text => window.testRecognition.onresult({resultIndex:0,results:[{isFinal:true,0:{transcript:text}}]});
    """)
    response=context.request.post(BASE+'/api/login',data={'token':config['JARVIS_ACCESS_TOKEN']},headers={'Origin':BASE})
    assert response.status==200
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE)
    expect(page.locator('#login-panel')).to_be_hidden()
    page.locator('#voice-options').evaluate('(el) => { el.open = true; }')
    expect(page.locator('#voice-volume')).to_be_visible(timeout=5000)
    page.locator('#test-voice').click()
    assert page.evaluate('voiceProbe.spoken.length')>0
    page.locator('#mic-toggle').click()
    page.evaluate("hear('Jarvis text mode')")
    expect(page.locator('#speak-replies')).not_to_be_checked()
    page.evaluate("hear('Jarvis talk mode')")
    expect(page.locator('#speak-replies')).to_be_checked()
    page.locator('#voice-volume').fill('40')
    page.locator('#voice-volume').dispatch_event('input')
    page.evaluate("hear('Jarvis increase volume')")
    assert int(page.locator('#voice-volume').input_value())>40
    page.locator('#test-voice').click()
    before=page.evaluate('voiceProbe.cancelled')
    assert page.evaluate('voiceProbe.listening'), 'Recognition must listen for stop during playback'
    page.evaluate("hear('stop')")
    assert page.evaluate('voiceProbe.cancelled')>before
    expect(page.locator('#stop-speaking')).to_be_hidden()
    # Contract integration: simulated speech and chat isolate continuous listening.
    chats=[]
    def chat_route(route):
        chats.append(route.request.post_data_json)
        route.fulfill(json={'text':'**A spoken answer to your question.** ✨ 😊', 'session_id':'browser-test'})
    page.route('**/api/chat', chat_route)
    page.evaluate("hear('Jarvis')")
    page.evaluate('window.lastUtterance?.onend?.()')
    page.evaluate("hear('Tell me about the moon')")
    expect(page.locator('#messages')).to_contain_text('A spoken answer to your question.')
    expect(page.locator('#send')).to_be_enabled()
    page.evaluate('window.lastUtterance?.onend?.()')
    page.evaluate("hear('And how far away is it')")
    expect(page.locator('#send')).to_be_enabled()
    assert len(chats)==2, 'Follow-up required repeating the wake word'
    assert page.evaluate('window.lastUtterance.text') == 'A spoken answer to your question.'
    page.evaluate('window.lastUtterance?.onend?.()')
    page.evaluate("hear('Stop Jarvis')")
    page.evaluate("hear('Private conversation must not be sent')")
    assert len(chats)==2, 'Conversation did not stop'
    page.locator('#voice-language').select_option('hi-IN')
    assert page.evaluate('window.testRecognition.lang')=='hi-IN'
    page.locator('#test-voice').click()
    assert page.evaluate('window.lastUtterance.lang')=='hi-IN'
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(ROOT/'data/voice-controls-mobile.png'),full_page=True)
    assert not errors,errors
    print(json.dumps({'browser':'Chromium','speech_source':'simulated, not physical microphone','talk_text_switch':'passed','volume':'passed','stop_during_playback':'passed','mobile_overflow':False,'page_errors':errors}))
    browser.close()
