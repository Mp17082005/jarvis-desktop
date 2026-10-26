"""Opt-in real browser pairing flow; staging cloud + local companion needed."""
import os,time,json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
CLOUD=os.getenv('JARVIS_PAIRING_CLOUD','http://127.0.0.1:8766')
LOCAL='http://127.0.0.1:8767'
config={}
for line in (ROOT/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1);config[k]=v.strip().strip("'\"")
with sync_playwright() as p:
    browser=p.chromium.launch();ctx=browser.new_context()
    if os.getenv('JARVIS_TEST_LOCAL_NETWORK_PERMISSION') == 'granted':
        ctx.grant_permissions(['local-network-access'], origin=CLOUD)
    errors=[];page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        # Read-only cross-origin detection before local approval.
        page.goto(CLOUD+'/connect.html')
        result=page.evaluate("""async ({local,origin})=>{const r=await fetch(local+'/companion/discovery?origin='+encodeURIComponent(origin),{credentials:'omit'});return {status:r.status,data:await r.json()};}""",{'local':LOCAL,'origin':CLOUD})
        assert result['status']==200,result
        assert result['data']['installed'] is True
        # Local same-origin API approval is an explicit test action, never a cloud grant.
        assert ctx.request.post(LOCAL+'/api/login',data={'token':config['JARVIS_ACCESS_TOKEN']},headers={'Origin':LOCAL}).status==200
        approved=ctx.request.post(LOCAL+'/api/pairing/approve',data={'origin':CLOUD},headers={'Origin':LOCAL})
        assert approved.status==200,approved.text()
        back=ctx.request.get(LOCAL+'/api/pairing').json()
        assert CLOUD in back['origins'],back
        paired=page.evaluate("""async ({local,origin})=>{const r=await fetch(local+'/companion/discovery?origin='+encodeURIComponent(origin),{credentials:'omit'});return await r.json();}""",{'local':LOCAL,'origin':CLOUD})
        assert paired['paired'] is True,paired
        assert paired['target']==LOCAL+'/',paired
        denied=ctx.request.post(LOCAL+'/api/pairing/approve',data={'origin':'https://evil.invalid'},headers={'Origin':CLOUD})
        assert denied.status==403,denied.status
        revoked=ctx.request.post(LOCAL+'/api/pairing/revoke',data={'origin':CLOUD},headers={'Origin':LOCAL})
        assert revoked.status==200
        assert CLOUD not in ctx.request.get(LOCAL+'/api/pairing').json()['origins']
        assert not errors,errors
        print(json.dumps({'cross_origin_discovery':'passed','explicit_local_pair_readback':'passed','cloud_cannot_approve':'passed','revoke_readback':'passed','page_errors':errors}))
    finally:ctx.close();browser.close()
