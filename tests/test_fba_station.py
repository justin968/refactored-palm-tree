"""Real ASGI request tests with a synthetic private SQLite database."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from starlette.testclient import TestClient
from test_fba import NOW, FUTURE, plan
from fba_v3_helpers import provision,release,scan
from diy_growth.fba import Blocked,canonical
from diy_growth.fba_station import create_app,password_hash,password_matches,read_users
from diy_growth.fba_warehouse import Warehouse

PASSWORD='synthetic-station-passphrase'
ORIGIN='http://127.0.0.1:8765'


@pytest.fixture
def station(tmp_path):
    db=tmp_path/'station.sqlite3';users=tmp_path/'users.json';time=[NOW]
    users.write_text(canonical({u:{'role':r,'password_hash':password_hash(PASSWORD)} for u,r in [('supervisor','supervisor'),('packer','packer'),('viewer','viewer')]}))
    w=Warehouse(str(db));w.save('work-1',plan(),NOW);provision(w);w.close()
    app=create_app(db,users,ORIGIN,clock=lambda:time[0])
    with TestClient(app,base_url=ORIGIN) as client:
        yield client,db,users,time


def login(client,user='supervisor'):
    out=client.post('/api/login',headers={'Origin':ORIGIN},json={'username':user,'password':PASSWORD})
    assert out.status_code==200,out.text
    client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':out.json()['csrf']})
    return out


def test_login_cookie_security_and_no_anonymous_data(station):
    c,db,users,time=station
    assert c.get('/api/queue').status_code==401
    r=login(c);cookie=r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert c.get('/api/session').json()['username']=='supervisor'
    assert c.get('/api/queue').status_code==200
    con=Warehouse(str(db));saved=con.db.execute('SELECT token_hash FROM fba_station_sessions').fetchone()[0];con.close()
    assert c.cookies.get('fba_station')!=saved


def test_password_hash_salted_and_verifies():
    a,b=password_hash(PASSWORD),password_hash(PASSWORD)
    assert a!=b and password_matches(PASSWORD,a) and not password_matches('bad',a)
    assert PASSWORD not in a
    with pytest.raises(Blocked):password_hash('short')


def test_login_origin_required(station):
    c,*_=station
    for origin in [None,'https://evil.example']:
        r=c.post('/api/login',headers={'Origin':origin} if origin else {},json={'username':'supervisor','password':PASSWORD})
        assert r.status_code==403


def test_untrusted_host_rejected(station):
    c,*_=station
    assert c.get('/',headers={'Host':'evil.example'}).status_code==400


def test_remote_insecure_origin_rejected(station):
    c,db,users,time=station
    with pytest.raises(Blocked):create_app(db,users,'http://warehouse.example')


def test_https_cookie_is_secure(station):
    c,db,users,time=station
    app=create_app(db,users,'https://warehouse.example',clock=lambda:NOW)
    with TestClient(app,base_url='https://warehouse.example') as c2:
        r=c2.post('/api/login',headers={'Origin':'https://warehouse.example'},json={'username':'supervisor','password':PASSWORD})
        assert r.status_code==200 and 'secure' in r.headers['set-cookie'].lower()
        assert r.headers['strict-transport-security']


def test_csrf_required_even_for_authenticated_user(station):
    c,*_=station;login(c);del c.headers['x-csrf-token']
    assert c.post('/api/work/work-1/cancel',json={'evidence_ref':'cancel'}).status_code==403


def test_cross_origin_mutation_rejected(station):
    c,*_=station;login(c)
    assert c.post('/api/work/work-1/cancel',headers={'Origin':'https://evil.example'},json={'evidence_ref':'cancel'}).status_code==403


@pytest.mark.parametrize('who',['packer','viewer'])
def test_packer_and_viewer_cannot_release_or_cancel(station,who):
    c,*_=station;login(c,who)
    assert c.post('/api/work/work-1/release',json={}).status_code==403
    assert c.post('/api/work/work-1/cancel',json={'evidence_ref':'cancel'}).status_code==403
    d=c.get('/api/work/work-1').json()
    assert 'snapshot' not in d['plan'] and 'gates' not in d['plan'] and 'estimated_cash' not in d['plan']
    assert c.get('/api/queue').json()['resources']==[]


def test_viewer_cannot_scan(station):
    c,*_=station;login(c,'viewer')
    assert c.post('/api/work/work-1/scan',json=scan()).status_code==403


def test_supervisor_release_reserves_resources(station):
    c,*_=station;login(c)
    d=c.get('/api/work/work-1').json()
    r=c.post('/api/work/work-1/release',json={'recipe_id':'recipe-work-1','owner':'packer','plan_digest':d['plan_digest'],'deadline':FUTURE,'evidence_ref':'supervised release'})
    assert r.status_code==200,r.text
    assert r.json()['release']['reviewer']=='supervisor'
    q=c.get('/api/queue').json()
    assert next(x for x in q['resources'] if x['resource_id']=='shared-stock')['held']==24


def test_packer_scan_is_attributed_to_session_not_body(station):
    c,db,*_=station
    w=Warehouse(str(db));release(w);w.close();login(c,'packer')
    r=c.post('/api/work/work-1/scan',json=scan())
    assert r.status_code==200 and r.json()['box']['packed_by']=='packer'
    assert c.post('/api/work/work-1/scan',json={**scan(1),'packed_by':'supervisor'}).status_code==409


def test_unknown_owner_cannot_receive_release(station):
    c,*_=station;login(c);d=c.get('/api/work/work-1').json()
    r=c.post('/api/work/work-1/release',json={'recipe_id':'recipe-work-1','owner':'invented','plan_digest':d['plan_digest'],'deadline':FUTURE,'evidence_ref':'release'})
    assert r.status_code==409


def test_old_session_invalidated_by_role_or_password_change(station):
    c,db,users,time=station;login(c)
    data=read_users(users);data['supervisor']['role']='viewer';users.write_text(canonical(data))
    assert c.get('/api/queue').status_code==401


def test_logout_invalidates_cookie_server_side(station):
    c,*_=station;login(c);old=c.cookies.get('fba_station')
    assert c.post('/api/logout',json={}).status_code==200
    c.cookies.set('fba_station',old)
    assert c.get('/api/queue').status_code==401


def test_session_expires(station):
    c,db,users,time=station;login(c);time[0]+=timedelta(hours=5)
    assert c.get('/api/queue').status_code==401


def test_login_throttled_persistently(station):
    c,*_=station
    for _ in range(10):
        assert c.post('/api/login',headers={'Origin':ORIGIN},json={'username':'supervisor','password':'bad'}).status_code==401
    assert c.post('/api/login',headers={'Origin':ORIGIN},json={'username':'supervisor','password':PASSWORD}).status_code==429


def test_request_limits_and_nonjson(station):
    c,*_=station;login(c)
    assert c.post('/api/work/work-1/scan',content='x').status_code==415
    assert c.post('/api/work/work-1/scan',content='x'*65537,headers={'Content-Type':'application/json'}).status_code==413
    assert c.post('/api/work/work-1/scan',json=[]).status_code==400
    assert c.post('/api/work/work-1/scan',content='{oops',headers={'Content-Type':'application/json'}).status_code==400


def test_browser_cannot_call_remote_executor(station):
    c,*_=station;login(c)
    assert c.post('/api/work/work-1/execute',json={}).status_code==403
    assert c.post('/api/execute',json={}).status_code==404


def test_assets_security_headers_and_no_secrets(station):
    c,*_=station
    page=c.get('/');assert page.status_code==200
    assert page.headers['cache-control']=='no-store'
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert 'synthetic-station-passphrase' not in page.text
    assert c.get('/assets/app.js').status_code==200
    assert c.get('/assets/users.json').status_code==404


def test_end_to_end_api_release_scan_seal_and_stage_without_network(station):
    from test_fba_execution import PID,GID,SCOPE
    c,db,*_=station;login(c)
    d=c.get('/api/work/work-1').json()
    assert c.post('/api/work/work-1/release',json=dict(recipe_id='recipe-work-1',owner='packer',deadline=FUTURE,evidence_ref='review',plan_digest=d['plan_digest'])).status_code==200
    login(c,'packer')
    for i in (0,1):assert c.post('/api/work/work-1/scan',json=scan(i)).status_code==200
    assert c.post('/api/work/work-1/seal',json={'evidence_ref':'qa'}).status_code==403
    login(c)
    assert c.post('/api/work/work-1/seal',json={'evidence_ref':'qa'}).json()['state']=='PACKED'
    params=dict(action_key='packing',scope=SCOPE,inbound_plan_id=PID,packing_group_id=GID,prep_owner='SELLER',label_owner='SELLER',evidence_ref='confirmed assignment',valid_until=FUTURE)
    out=c.post('/api/work/work-1/prepare-packing',json=params)
    assert out.status_code==200 and out.json()['state']=='STAGED'
    assert out.json()['approval'] is None
    w=Warehouse(str(db));assert w.db.execute("SELECT COUNT(*) FROM fba_action_events WHERE kind='DISPATCHING'").fetchone()[0]==0;w.close()
