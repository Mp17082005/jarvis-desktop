"""Bounded actual gateway image-to-speech probe; no SDP/tokens persisted.
Run: env -u PYTHONPATH uv tool run --from playwright python tests/probe_realtime_vision.py
"""
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
import probe_webrtc as transport

OUT = Path(__file__).resolve().parents[1] / 'data' / 'vision-probe'
IMAGE = r"""() => {
 const c=document.createElement('canvas');c.width=800;c.height=500;
 const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,800,500);
 x.fillStyle='#0000ff';x.fillRect(90,100,200,200);
 x.fillStyle='#ff0000';x.beginPath();x.arc(600,200,100,0,Math.PI*2);x.fill();
 x.fillStyle='black';x.font='bold 56px Arial';x.textAlign='center';x.fillText('JARVIS 472',400,420);
 document.body.appendChild(c);probe.image=c.toDataURL('image/png');return probe.image;
}"""
EXERCISE = r"""async () => {
 const p=probe;
 const wait=async test=>{while(!test()){
  if(p.errors.length)throw Error('provider error');
  if(performance.now()>p.sessionDeadline-1500)throw Error('bounded event deadline');
  await new Promise(r=>setTimeout(r,50));
 }};
 try {
  await wait(()=>p.dc.readyState==='open'&&p.events.some(e=>e.type==='session.created'));
  p.send({type:'conversation.item.create',item:{type:'message',role:'user',content:[
   {type:'input_text',text:'Describe the shapes and read text in this image. Be specific and brief.'},
   {type:'input_image',image_url:p.image}
  ]}});
  p.send({type:'response.create',response:{output_modalities:['audio']}});
  await wait(()=>p.events.some(e=>e.type==='response.done'));
  await wait(()=>p.events.some(e=>e.type==='output_audio_buffer.stopped'));
  await new Promise(r=>setTimeout(r,300));
 }catch(e){p.errors.push({exercise:e.message})}
 // Do not save the image data URL twice in the JSON report.
 p.sent=p.sent.map(e=>e.item?{...e,item:{...e.item,content:e.item.content.map(c=>c.type==='input_image'?{type:c.type,image_url:'[PNG saved as image.png]'}:c)}}:e);
 return true;
}"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = transport.config()
    base = cfg['JARVIS_LLM_BASE_URL'].rstrip('/')
    transport.SESSION = {
        'type': 'realtime', 'model': 'gpt-realtime-2.1',
        'output_modalities': ['audio'],
        'instructions': 'Describe the user-provided image accurately and briefly in English. Read any visible text.',
        'max_output_tokens': 512,
        'audio': {'input': {'turn_detection': None}, 'output': {'voice': 'marin'}},
    }
    report = {
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'session_request': {'session': transport.SESSION},
        'input': 'Locally drawn canvas PNG and synthetic silent WebAudio track; no physical camera or microphone.',
        'expected_image': 'Blue square on left, red circle on right, black JARVIS 472 text below on white background.',
        'protocol': ['POST /v1/realtime/client_secrets', 'POST /v1/realtime/calls with ephemeral Bearer and application/sdp',
                     'oai-events conversation.item.create: message/user with input_text and input_image.image_url PNG data URL',
                     'response.create output_modalities=[audio]'],
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
        page = browser.new_page()
        page.route('http://127.0.0.1:18767/', lambda r: r.fulfill(status=200, content_type='text/html', body='<!doctype html><title>Native vision probe</title>'))
        page.goto('http://127.0.0.1:18767/')
        start = None
        try:
            offer = page.evaluate(transport.SETUP)
            data_url = page.evaluate(IMAGE)
            (OUT / 'image.png').write_bytes(base64.b64decode(data_url.split(',', 1)[1]))
            report['image_path'] = str(OUT / 'image.png')
            start = time.monotonic()
            token, report['mint'] = transport.mint(base, cfg['JARVIS_LLM_API_KEY'])
            if token:
                remaining_ms = max(0, int((55 - (time.monotonic() - start)) * 1000))
                page.evaluate('ms => {probe.sessionDeadline=performance.now()+ms;probe.cap=setTimeout(()=>{probe.dc.close();probe.pc.close();probe.dest.stream.getTracks().forEach(t=>t.stop())},ms)}', remaining_ms)
                report['exchange'] = page.evaluate(transport.FETCH, {'endpoint': base + '/realtime/calls', 'token': token, 'offer': offer})
                token = None
                if report['exchange'].get('answer_is_sdp'):
                    page.evaluate(EXERCISE)
            result = page.evaluate(transport.FINISH)
            recording = base64.b64decode(result.pop('recording_base64'))
            if recording:
                (OUT / 'answer.webm').write_bytes(recording)
                result['recording_path'] = str(OUT / 'answer.webm')
            report['result'] = result
            transcript = ' '.join(result['transcripts'])
            lower = transcript.lower()
            report['spoken_transcript'] = transcript
            report['audio_verified'] = bool(result['max_rms'] > .001 and recording and any(s.get('bytesReceived', 0) > 0 for s in result['rtp']))
            report['vision_checks'] = {name: term in lower for name, term in {
                'blue_square': 'blue square', 'left': 'left', 'red_circle': 'red circle',
                'right': 'right', 'label_word': 'jarvis', 'label_digits': '472',
            }.items()}
            report['vision_verified'] = all(report['vision_checks'].values())
            report['response_completed'] = any(e.get('type') == 'response.done' and e.get('response', {}).get('status') == 'completed' for e in result['events'])
            report['verified'] = report['audio_verified'] and report['vision_verified'] and report['response_completed'] and not result['errors']
        except Exception as exc:
            report['exception_type'] = type(exc).__name__
            report['verified'] = False
        finally:
            page.close()
            browser.close()
            if start is not None:
                report['session_duration_seconds'] = round(time.monotonic() - start, 2)
            (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('result', 'session_request')}, indent=2))
    return 0 if report.get('verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
