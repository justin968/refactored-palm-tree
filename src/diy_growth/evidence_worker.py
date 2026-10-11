"""One-shot read-only worker for a private hosted PHP gateway."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from .evidence import validate_history


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--import-hash',required=True)
    p.add_argument('--route',choices=['health','historical'],required=True)
    args=p.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory=sqlite3.Row
        if not conn.execute('SELECT 1 FROM evidence_imports WHERE hash=?',(args.import_hash,)).fetchone():
            raise RuntimeError('configured evidence unavailable')
        payload=validate_history(conn,args.import_hash) if args.route=='historical' else dict(
            status='ok',import_hash=args.import_hash,writes_enabled=False,live_ingestion_verified=False)
    print(json.dumps(payload,allow_nan=False))


if __name__=='__main__':
    main()
