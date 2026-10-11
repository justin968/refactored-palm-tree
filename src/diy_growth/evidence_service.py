"""Private, read-only HTTP interface for imported owner evidence.

Run behind the existing authenticated host proxy; default binding is loopback.
This module never imports a production executor or installs a scheduler.
"""
from __future__ import annotations
import argparse
import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .evidence import validate_history


def make_server(db: Path, fingerprint: str, token: str, host='127.0.0.1', port=8765):
    if len(token) < 32:
        raise ValueError('A private access token of at least32characters is required')
    uri = db.resolve().as_uri()+'?mode=ro'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            supplied = self.headers.get('Authorization','')
            if not hmac.compare_digest(supplied, 'Bearer '+token):
                self.respond(401, {'error':'unauthorized'})
                return
            if self.path not in ('/healthz','/v1/historical-evaluation'):
                self.respond(404, {'error':'not_found'})
                return
            try:
                with sqlite3.connect(uri,uri=True) as conn:
                    conn.row_factory=sqlite3.Row
                    if not conn.execute('SELECT 1 FROM evidence_imports WHERE hash=?',(fingerprint,)).fetchone():
                        self.respond(503,{'error':'configured_import_unavailable'})
                        return
                    if self.path=='/healthz':
                        payload=dict(status='ok',import_hash=fingerprint,writes_enabled=False,live_ingestion_verified=False)
                    else:
                        payload=validate_history(conn,fingerprint)
                self.respond(200,payload)
            except sqlite3.Error:
                self.respond(503,{'error':'evidence_database_unavailable'})

        def respond(self,status,payload):
            data=json.dumps(payload,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self,*args):
            # Avoid logging credentials, financial records or query strings.
            pass

    return ThreadingHTTPServer((host,port),Handler)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path)
    p.add_argument('--import-hash')
    p.add_argument('--config',type=Path)
    p.add_argument('--port',type=int,default=8765)
    args=p.parse_args()
    token=os.environ.get('GROWTH_EVIDENCE_TOKEN','')
    if args.config:
        config=json.loads(args.config.read_text())
        args.db=args.config.parent/'growth.sqlite3'
        args.import_hash=config['import_hash']
        token=config['token']
    if not args.db or not args.import_hash:
        p.error('--config or both --db and --import-hash are required')
    server=make_server(args.db,args.import_hash,token,port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__=='__main__':
    main()
