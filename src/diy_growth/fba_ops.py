"""Private CLI for operational reads, warehouse capture and supervised dispatch.

Usage: python -m diy_growth.fba_ops --db /private/fba.sqlite3 COMMAND ...
This is not a public web server or an unattended scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from .fba import Blocked, canonical, digest
from .fba_api import Amazon, ApiError, HttpTransport, LWAToken, ShipStation


def client_for(scope: str):
    transport = HttpTransport()
    if scope.startswith('amazon:'):
        seller, market = os.environ.get('FBA_SELLER_ID', ''), os.environ.get('FBA_MARKETPLACE_ID', '')
        client = Amazon(LWAToken.from_env(transport), seller, market, transport)
    elif scope.startswith('shipstation:'):
        client = ShipStation(os.environ.get('FBA_SHIPSTATION_API_KEY', ''),
                             os.environ.get('FBA_SHIPSTATION_ACCOUNT_REF', ''), transport)
    else:
        raise Blocked('unsupported account scope')
    if client.scope != scope:
        raise Blocked('configured runtime account differs from requested scope')
    return client


def secure_file(path: Path, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Blocked('private state/output must not be a symbolic link')
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('status')
    for name in ('inventory', 'inbound-plans'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--out', type=Path, required=True)
    cmd = sub.add_parser('listing')
    cmd.add_argument('sku')
    cmd.add_argument('--out', type=Path, required=True)
    for name in ('capture', 'link', 'stage'):
        cmd = sub.add_parser(name)
        cmd.add_argument('key', help='Work-order key, local box ID, or action key respectively')
        cmd.add_argument('file', type=Path)
    cmd = sub.add_parser('approve')
    cmd.add_argument('key')
    for name in ('digest', 'operator', 'evidence-ref', 'valid-until'):
        cmd.add_argument('--' + name, required=True)
    for name in ('execute', 'refresh', 'show'):
        cmd = sub.add_parser(name)
        cmd.add_argument('key')
    cmd = sub.add_parser('adopt')
    cmd.add_argument('key')
    cmd.add_argument('--remote-id', required=True)
    cmd.add_argument('--operator', required=True)
    cmd.add_argument('--evidence-ref', required=True)
    args = parser.parse_args()
    store = None
    try:
        now = datetime.now(timezone.utc)
        if args.command == 'doctor':
            names = ('FBA_LWA_CLIENT_ID', 'FBA_LWA_CLIENT_SECRET', 'FBA_LWA_REFRESH_TOKEN',
                     'FBA_SELLER_ID', 'FBA_MARKETPLACE_ID', 'FBA_SHIPSTATION_API_KEY', 'FBA_SHIPSTATION_ACCOUNT_REF')
            result = {'credential_presence_only': {n: bool(os.environ.get(n)) for n in names},
                      'authentication_tested': False,
                      'mutations_enabled': os.environ.get('FBA_ENABLE_MUTATIONS') == 'yes',
                      'allowed_operations': os.environ.get('FBA_ALLOWED_OPERATIONS', '').split(',') if os.environ.get('FBA_ALLOWED_OPERATIONS') else []}
        elif args.command in ('inventory', 'inbound-plans', 'listing'):
            scope = f'amazon:NA:{os.environ.get("FBA_SELLER_ID", "")}:{os.environ.get("FBA_MARKETPLACE_ID", "")}'
            client = client_for(scope)
            if args.command == 'inventory':
                snapshot = client.inventory()
            elif args.command == 'inbound-plans':
                snapshot = client.inbound_plans()
            else:
                reply = client.listing(args.sku)
                snapshot = {'scope': scope, 'observed_at': now.isoformat(), 'request_id': reply.request_id, 'body': reply.body}
            secure_file(args.out, exclusive=True)
            args.out.write_text(canonical(snapshot), encoding='utf-8')
            result = {'saved': str(args.out), 'digest': digest(snapshot), 'live_read_completed': True,
                      'automatic_replenishment_enabled': False}
        else:
            secure_file(args.db)
            from .fba_warehouse import Warehouse
            store = Warehouse(str(args.db))
            if args.command == 'status':
                counts = dict(store.db.execute('SELECT state,COUNT(*) FROM fba_actions GROUP BY state').fetchall())
                result = {'actions_by_state': counts, 'exceptions': store.exceptions(),
                          'warehouse_order_release_implemented': True, 'external_erp_release_implemented': False, 'label_purchase_implemented': False}
            elif args.command == 'show':
                result = store.action(args.key)
            elif args.command in ('capture', 'link', 'stage'):
                data = json.loads(args.file.read_text(encoding='utf-8'))
                result = getattr(store, args.command)(args.key, data, now)
            elif args.command == 'approve':
                result = store.approve(args.key, args.digest, args.operator, args.evidence_ref, args.valid_until, now)
            else:
                action = store.action(args.key)
                client = client_for(action['proposal']['scope'])
                if args.command == 'adopt':
                    result = store.adopt(args.key, client, args.remote_id, args.operator, args.evidence_ref, now)
                elif args.command == 'refresh':
                    result = store.refresh(args.key, client, now)
                else:
                    result = store.execute(args.key, client, now=now,
                        enabled=os.environ.get('FBA_ENABLE_MUTATIONS') == 'yes',
                        allowed_operations=frozenset(filter(None, os.environ.get('FBA_ALLOWED_OPERATIONS', '').split(','))))
        print(json.dumps(result, indent=2, allow_nan=False))
    except (Blocked, ApiError, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc)}))
        raise SystemExit(2) from None
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    main()
