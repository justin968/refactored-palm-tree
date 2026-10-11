import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
from diy_growth.db import connect
from diy_growth.evidence import import_evidence
from diy_growth.evidence_service import make_server


def test_private_http_database_to_evaluation_and_auth(tmp_path):
    from test_evidence import EvidenceTests
    manifest=EvidenceTests().fixture(tmp_path)
    db=tmp_path/'db'
    conn=connect(db)
    fingerprint=import_evidence(conn,manifest)
    conn.close()
    before=db.read_bytes()
    server=make_server(db,fingerprint,'x'*32,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        root=f'http://127.0.0.1:{server.server_port}'
        try:
            urllib.request.urlopen(root+'/v1/historical-evaluation')
            assert False,'unauthenticated data must not be returned'
        except urllib.error.HTTPError as e:
            assert e.code==401
        for route in ('/healthz','/v1/historical-evaluation'):
            req=urllib.request.Request(root+route,headers={'Authorization':'Bearer '+'x'*32})
            with urllib.request.urlopen(req) as response:
                payload=json.load(response)
                assert payload['import_hash']==fingerprint
                assert response.headers['Cache-Control']=='no-store'
        assert db.read_bytes()==before
    finally:
        server.shutdown();server.server_close();thread.join()
