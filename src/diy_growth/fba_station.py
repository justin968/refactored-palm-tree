"""Authenticated private warehouse station. No browser endpoint can dispatch APIs.

Run via CLI with a private user file, a private SQLite path and explicit origin.
Bind is loopback only. A remote warehouse requires your TLS reverse proxy/VPN.
"""
from datetime import datetime, timezone
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlsplit

from .fba import Blocked, canonical, digest, text
from .fba_ops import secure_file
from .fba_warehouse import Warehouse
from .fba_resources import transaction

ROLES = {'supervisor','packer','viewer'}
AUTH_SCHEMA = '''
CREATE TABLE IF NOT EXISTS fba_station_sessions (
 token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, credential_version TEXT NOT NULL,
 csrf TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fba_station_logins (bucket TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fba_login_window ON fba_station_logins(bucket,at);
'''


def password_hash(password):
    if not isinstance(password,str) or not 14 <= len(password) <= 1024:
        raise Blocked('password must contain 14 to 1024 characters')
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
    return salt.hex() + ':' + value.hex()


def password_matches(password, encoded):
    try:
        salt,expected = encoded.split(':')
        actual = hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1,dklen=32)
        return hmac.compare_digest(actual,bytes.fromhex(expected))
    except (ValueError,TypeError,AttributeError):
        return False


def read_users(path):
    users = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(users,dict) or not users:
        raise Blocked('private named-user file is missing or empty')
    for name,row in users.items():
        if not name or not isinstance(row,dict) or set(row) != {'role','password_hash'} or row['role'] not in ROLES:
            raise Blocked('invalid named-user configuration')
        # Reject corrupt hashes before accepting logins.
        try:
            salt,key = row['password_hash'].split(':')
            if len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(key)) != 32:
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise Blocked('invalid password hash in user configuration') from None
    return users


def public_detail(d, role):
    if role == 'supervisor':
        return d
    plan = d['plan']
    return {k:v for k,v in d.items() if k not in ('plan','plan_digest','release','events')} | {
        'plan':{k:plan[k] for k in ('msku','pack_spec','planned_units','cartons')},
        'release': {k:d['release'].get(k) for k in ('owner','deadline','valid_until')} if d['release'] else None}


def create_app(db_path=None, users_path=None, origin=None, clock=None):
    from starlette.applications import Starlette
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Route
    from starlette.concurrency import run_in_threadpool

    db_path = str(db_path or os.environ.get('FBA_WAREHOUSE_DB',''))
    users_path = str(users_path or os.environ.get('FBA_WAREHOUSE_USERS',''))
    origin = origin or os.environ.get('FBA_WAREHOUSE_ORIGIN','http://127.0.0.1:8765')
    parsed = urlsplit(origin)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise Blocked('an exact origin without a path or credentials is required')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise Blocked('remote access requires an HTTPS origin')
    if not db_path or not users_path:
        raise Blocked('private database and user-file paths must be configured')
    read_users(users_path)
    secure_file(Path(db_path))
    w = Warehouse(db_path)
    w.db.executescript(AUTH_SCHEMA)
    w.close()
    clock = clock or (lambda:datetime.now(timezone.utc))
    assets = Path(__file__).with_name('station_assets')
    dummy = password_hash(secrets.token_urlsafe(24))

    def process(request, data):
        now = clock()
        epoch = int(now.timestamp())
        w = Warehouse(db_path)
        try:
            path, method = request.url.path,request.method
            if method != 'GET' and request.headers.get('origin') != origin:
                return JSONResponse({'error':'origin rejected'},status_code=403)
            users = read_users(users_path)
            sid = request.cookies.get('fba_station','')
            token_hash = hashlib.sha256(sid.encode()).hexdigest()
            if path == '/api/login' and method == 'POST':
                username,password = data.get('username'),data.get('password')
                if not isinstance(username,str) or not isinstance(password,str) or len(username)>128 or len(password)>1024:
                    return JSONResponse({'error':'invalid credentials'},status_code=401)
                buckets = ['u:'+hashlib.sha256(username.encode()).hexdigest(),
                           'ip:'+hashlib.sha256((request.client.host if request.client else 'unknown').encode()).hexdigest()]
                with transaction(w.db):
                    w.db.execute('DELETE FROM fba_station_logins WHERE at<?',(epoch-900,))
                    for bucket,limit in zip(buckets,(10,40)):
                        if w.db.execute('SELECT COUNT(*) FROM fba_station_logins WHERE bucket=? AND at>=?',(bucket,epoch-900)).fetchone()[0]>=limit:
                            return JSONResponse({'error':'login temporarily rate limited'},status_code=429)
                    w.db.executemany('INSERT INTO fba_station_logins VALUES (?,?)',[(b,epoch) for b in buckets])
                user = users.get(username)
                matched = password_matches(password,user['password_hash'] if user else dummy)
                if not user or not matched:
                    return JSONResponse({'error':'invalid credentials'},status_code=401)
                session_id,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                with transaction(w.db):
                    w.db.execute('DELETE FROM fba_station_sessions WHERE token_hash=? OR expires<=?',(token_hash,epoch))
                    w.db.execute('INSERT INTO fba_station_sessions VALUES (?,?,?,?,?)',
                                 (hashlib.sha256(session_id.encode()).hexdigest(),username,digest(user),csrf,epoch+14400))
                response = JSONResponse({'username':username,'role':user['role'],'csrf':csrf})
                response.set_cookie('fba_station',session_id,httponly=True,secure=parsed.scheme=='https',samesite='strict',max_age=14400,path='/')
                return response
            auth = w.db.execute('SELECT username,credential_version,csrf,expires FROM fba_station_sessions WHERE token_hash=?',(token_hash,)).fetchone()
            if not auth or auth[3]<=epoch or auth[0] not in users or auth[1]!=digest(users[auth[0]]):
                return JSONResponse({'error':'sign in required'},status_code=401)
            actor,role = auth[0],users[auth[0]]['role']
            if method != 'GET' and not hmac.compare_digest(request.headers.get('x-csrf-token',''),auth[2]):
                return JSONResponse({'error':'CSRF token required'},status_code=403)
            if path == '/api/session' and method=='GET':
                return JSONResponse({'username':actor,'role':role,'csrf':auth[2]})
            if path == '/api/logout' and method=='POST':
                w.db.execute('DELETE FROM fba_station_sessions WHERE token_hash=?',(token_hash,))
                response=JSONResponse({'signed_out':True});response.delete_cookie('fba_station',path='/');return response
            if path == '/api/queue' and method=='GET':
                out=w.queue(now)
                out['recipes']=[json.loads(r[0]) for r in w.db.execute('SELECT body FROM fba_recipes')] if role=='supervisor' else []
                if role!='supervisor':
                    out['resources']=[];out['exceptions']=[]
                mode=w.db.execute("SELECT value FROM fba_warehouse_settings WHERE name='mode'").fetchone()
                out['mode']=mode[0] if mode else 'PRIVATE'
                return JSONResponse(out)
            key=request.path_params.get('key')
            action=request.path_params.get('action')
            if path.startswith('/api/work/') and key:
                if method=='GET' and not action:
                    return JSONResponse(public_detail(w.detail(key),role))
                if method=='POST':
                    allowed = {'scan','link'} if role=='packer' else ({'scan','link','release','void','seal','cancel','prepare-packing'} if role=='supervisor' else set())
                    if action not in allowed:
                        return JSONResponse({'error':'role does not permit this action'},status_code=403)
                    if action=='scan':
                        result=w.scan(key,data,actor,now)
                    elif action=='release':
                        if set(data)!={'recipe_id','plan_digest','owner','deadline','evidence_ref'} or data['owner'] not in users:
                            raise Blocked('release requires a named existing owner and exact reviewed fields')
                        result=w.release(key,data['recipe_id'],data['plan_digest'],data['owner'],data['deadline'],data['evidence_ref'],actor,now)
                    elif action=='void':
                        result=w.void_box(key,data['box_id'],data['evidence_ref'],actor,now)
                    elif action=='seal':
                        result=w.seal(key,data['evidence_ref'],actor,now)
                    elif action=='cancel':
                        result=w.cancel(key,data['evidence_ref'],actor,now)
                    elif action=='prepare-packing':
                        result=w.prepare_packing(key,data,actor,now)
                    else:
                        if set(data)!={'box_id','amazon_box_id','carrier','tracking_number','evidence_ref'}:
                            raise Blocked('linkage fields differ from contract')
                        d=w.detail(key)
                        if d['state']!='PACKED' or data['box_id'] not in {b['local_box_id'] for b in d['boxes']}:
                            raise Blocked('link only a captured carton in this sealed work order')
                        link={k:v for k,v in data.items() if k!='box_id'}
                        result=w.link(data['box_id'],{**link,'linked_by':actor,'linked_at':now.isoformat()},now)
                    # Do not return economic data to packers after mutations.
                    if 'plan' in result:
                        result=public_detail(result,role)
                    return JSONResponse(result)
            return JSONResponse({'error':'endpoint not available'},status_code=404)
        except Blocked as exc:
            return JSONResponse({'error':str(exc)},status_code=409)
        except (ValueError,KeyError,TypeError,AttributeError):
            return JSONResponse({'error':'invalid input fields or value'},status_code=400)
        except (OSError,sqlite3.Error):
            return JSONResponse({'error':'private state unavailable; no retry of remote writes'},status_code=503)
        finally:
            w.close()

    async def api(request):
        data={}
        if request.method=='POST':
            if request.headers.get('content-type','').split(';')[0]!='application/json':
                return JSONResponse({'error':'JSON required'},status_code=415)
            raw=bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw)>65536:
                    return JSONResponse({'error':'request too large'},status_code=413)
            try:
                data=json.loads(raw)
                if not isinstance(data,dict):raise ValueError()
            except (ValueError,UnicodeError):
                return JSONResponse({'error':'JSON object required'},status_code=400)
        return await run_in_threadpool(process,request,data)

    async def asset(request):
        name=request.path_params.get('name','index.html')
        if name not in ('index.html','app.js','style.css'):
            return JSONResponse({'error':'not found'},status_code=404)
        return FileResponse(assets/name)

    app=Starlette(routes=[Route('/',asset),Route('/assets/{name}',asset),
        Route('/api/work/{key}/{action}',api,methods=['POST']),Route('/api/work/{key}',api,methods=['GET']),
        Route('/api/{name}',api,methods=['GET','POST'])])
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[parsed.hostname])
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        if parsed.scheme=='https':response.headers['Strict-Transport-Security']='max-age=31536000'
        return response
    from starlette.middleware.base import BaseHTTPMiddleware
    app.add_middleware(BaseHTTPMiddleware, dispatch=headers)
    return app


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('serve');s.add_argument('--users',type=Path,required=True);s.add_argument('--port',type=int,default=8765);s.add_argument('--origin',default='http://127.0.0.1:8765')
    s=sub.add_parser('user');s.add_argument('--users',type=Path,required=True);s.add_argument('--name',required=True);s.add_argument('--role',choices=sorted(ROLES),required=True)
    for name in ('request','count','recipe'):
        s=sub.add_parser(name);s.add_argument('file',type=Path);s.add_argument('--actor',required=True)
        if name=='count':s.add_argument('--event-id',required=True)
        if name=='recipe':s.add_argument('--work-key',required=True)
    s=sub.add_parser('backup');s.add_argument('--out',type=Path,required=True)
    a=p.parse_args();w=None
    try:
        if a.command=='user':
            users=read_users(a.users) if a.users.exists() else {}
            pw=getpass.getpass('New password (14+ characters): ')
            if pw!=getpass.getpass('Repeat password: '):raise Blocked('passwords differ')
            users[text(a.name,'username')]={'role':a.role,'password_hash':password_hash(pw)}
            tmp=a.users.with_name(a.users.name+'.new-'+secrets.token_hex(8))
            secure_file(tmp,exclusive=True);tmp.write_text(canonical(users),encoding='utf-8');os.replace(tmp,a.users)
            print('Named user saved; old sessions are invalid after credential changes.')
        elif a.command=='serve':
            import uvicorn
            app=create_app(a.db,a.users,a.origin)
            uvicorn.run(app,host='127.0.0.1',port=a.port,proxy_headers=False,access_log=False,limit_concurrency=32)
        else:
            secure_file(a.db);w=Warehouse(str(a.db));now=datetime.now(timezone.utc)
            if a.command=='backup':
                secure_file(a.out,exclusive=True)
                target=sqlite3.connect(a.out)
                try:w.db.backup(target)
                finally:target.close()
                result={'backup':str(a.out),'mode':'SQLite online backup'}
            else:
                data=json.loads(a.file.read_text(encoding='utf-8'))
                if a.command=='request':result=w.import_request(data,now)
                elif a.command=='count':result=w.count(a.event_id,data,a.actor,now)
                else:result=w.add_recipe(data,a.work_key,a.actor,now)
            print(json.dumps(result,indent=2))
    except (Blocked,KeyError,ValueError,TypeError,OSError,sqlite3.Error):
        print('Blocked: check validated input, private paths and permissions; no external action taken.')
        raise SystemExit(2) from None
    finally:
        if w:w.close()


if __name__=='__main__':main()
