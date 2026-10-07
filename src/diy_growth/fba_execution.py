"""Durable, single-attempt FBA mutation journal on a trusted private machine.

An operator approves the exact proposal hash. Dispatch also requires an explicit
runtime allowlist. DISPATCHING/UNCERTAIN actions are never automatically replayed.
API_SUCCEEDED is not proof of warehouse release, carrier acceptance or receipt.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import sqlite3

from .fba import Blocked, PackSpec, canonical, digest, instant, require_gates, text
from .fba_api import ApiError, api_id
from .fba_packout import (WarehouseStore, inbound_plan_payload, packing_payload,
                          shipstation_payload, tracking_payload)

OPERATIONS = frozenset(('createInboundPlan', 'setPackingInformation',
                       'updateShipmentTrackingDetails', 'createShipStationShipment'))


def prep_fields(row: dict) -> dict:
    args = {'prep_owner': row.get('prepOwner'), 'label_owner': row.get('labelOwner')}
    for source, target in (('expiration', 'expiration'), ('manufacturingLotCode', 'manufacturing_lot')):
        if source in row:
            args[target] = row[source]
    return args


class Journal(WarehouseStore):
    def __init__(self, path: str):
        super().__init__(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS fba_actions (
          action_key TEXT PRIMARY KEY, scope TEXT NOT NULL, resource TEXT NOT NULL,
          operation TEXT NOT NULL, digest TEXT NOT NULL, proposal TEXT NOT NULL,
          state TEXT NOT NULL, approval TEXT, response TEXT,
          UNIQUE(scope,resource,operation));
        CREATE TABLE IF NOT EXISTS fba_remote_refs (
          scope TEXT NOT NULL, operation TEXT NOT NULL, remote_id TEXT NOT NULL, action_key TEXT NOT NULL,
          PRIMARY KEY(scope,operation,remote_id));
        CREATE TABLE IF NOT EXISTS fba_action_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, action_key TEXT NOT NULL,
          recorded_at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
        ''')

    def _event(self, key: str, kind: str, body: dict, now: datetime):
        self.db.execute('INSERT INTO fba_action_events(action_key,recorded_at,kind,body) VALUES (?,?,?,?)',
                        (key, now.isoformat(), kind, canonical(body)))

    def action(self, key: str) -> dict:
        row = self.db.execute('SELECT action_key,digest,proposal,state,approval,response FROM fba_actions WHERE action_key=?', (key,)).fetchone()
        if not row:
            raise Blocked('action not found')
        return {'action_key': row[0], 'digest': row[1], 'proposal': json.loads(row[2]), 'state': row[3],
                'approval': json.loads(row[4]) if row[4] else None,
                'response': json.loads(row[5]) if row[5] else None}

    def validate_proposal(self, proposal: dict, now: datetime) -> str:
        required = {'scope', 'operation', 'work_key', 'ids', 'body', 'evidence_ref', 'valid_until'}
        if set(proposal) != required:
            raise Blocked('proposal fields differ from the supported contract')
        op = proposal['operation']
        if op not in OPERATIONS:
            raise Blocked('operation unsupported; no listings, purchases, charges, claims or refunds')
        scope = text(proposal['scope'], 'account scope')
        text(proposal['evidence_ref'], 'current remote/prep/assignment evidence')
        if instant(proposal['valid_until']) <= now:
            raise Blocked('proposal expired')
        p = self.work(proposal['work_key'])
        spec = PackSpec(**p['pack_spec'])
        require_gates(p['gates'], spec, now)
        if any(instant(proposal['valid_until']) > instant(g['valid_until']) for g in p['gates'].values()):
            raise Blocked('proposal outlives supporting approvals')
        ids, body = proposal['ids'], proposal['body']
        if not isinstance(ids, dict) or not isinstance(body, dict):
            raise Blocked('ids/body must be JSON objects')
        if op == 'createShipStationShipment':
            if not scope.startswith('shipstation:') or ids:
                raise Blocked('invalid ShipStation scope/IDs')
            record, = body['shipments']
            expected = shipstation_payload(p, self.boxes(proposal['work_key']), record['external_shipment_id'],
                                           record['ship_from'], record['ship_to'], now)
            resource = 'external:' + record['external_shipment_id']
        else:
            if not scope.startswith('amazon:NA:') or not scope.endswith(':' + spec.marketplace_id):
                raise Blocked('invalid Amazon account/marketplace scope')
            if op == 'createInboundPlan':
                if ids:
                    raise Blocked('new inbound plan must not supply a guessed plan ID')
                row, = body['items']
                expected = inbound_plan_payload(p, body['sourceAddress'], body['name'], now, **prep_fields(row))
                resource = 'work:' + proposal['work_key']
            elif op == 'setPackingInformation':
                if set(ids) != {'inboundPlanId'}:
                    raise Blocked('packing action needs an exact inbound plan ID')
                group, = body['packageGroupings']
                grouping = {k: group[k] for k in ('shipmentId', 'packingGroupId') if k in group}
                expected = packing_payload(p, self.boxes(proposal['work_key']), grouping, now,
                                           **prep_fields(group['boxes'][0]['items'][0]))
                resource = 'plan:' + api_id(ids['inboundPlanId'], 'inboundPlanId')
            else:
                if set(ids) != {'inboundPlanId', 'shipmentId'}:
                    raise Blocked('tracking action needs both current API IDs')
                api_id(ids['inboundPlanId'], 'inboundPlanId')
                resource = 'shipment:' + api_id(ids['shipmentId'], 'shipmentId')
                expected = tracking_payload(p, self.boxes(proposal['work_key'], linked=True), now)
        if canonical(body) != canonical(expected):
            raise Blocked('payload differs from recorded work/physical measurements or contains unsupported fields')
        return resource

    def stage(self, key: str, proposal: dict, now: datetime) -> dict:
        text(key, 'action key')
        resource = self.validate_proposal(proposal, now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior = self.db.execute('SELECT digest FROM fba_actions WHERE action_key=?', (key,)).fetchone()
            if prior:
                if prior[0] != digest(proposal):
                    raise Blocked('action key reused with changed payload')
            else:
                # A different external ID cannot create a second ShipStation task for the same work order.
                for old, in self.db.execute('SELECT proposal FROM fba_actions WHERE scope=? AND operation=?', (proposal['scope'], proposal['operation'])):
                    if json.loads(old)['work_key'] == proposal['work_key']:
                        raise Blocked('this work already has this operation; reconcile before replacement')
                self.db.execute('INSERT INTO fba_actions VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                                (key, proposal['scope'], resource, proposal['operation'], digest(proposal), canonical(proposal), 'STAGED'))
                self._event(key, 'STAGED', {'digest': digest(proposal)}, now)
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise Blocked('resource already has this operation under another key') from None
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return self.action(key)

    def approve(self, key: str, expected_digest: str, operator: str, evidence_ref: str,
                valid_until: str, now: datetime) -> dict:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            action = self.action(key)
            if action['state'] != 'STAGED' or action['digest'] != expected_digest:
                raise Blocked('only the exact staged proposal may be approved')
            self.validate_proposal(action['proposal'], now)
            if not now < instant(valid_until) <= instant(action['proposal']['valid_until']):
                raise Blocked('approval validity exceeds proposal window or has expired')
            approval = {'operator': text(operator, 'operator'), 'evidence_ref': text(evidence_ref, 'approval evidence'),
                        'digest': expected_digest, 'approved_at': now.isoformat(), 'valid_until': valid_until}
            self.db.execute("UPDATE fba_actions SET state='APPROVED',approval=? WHERE action_key=?", (canonical(approval), key))
            self._event(key, 'APPROVED', approval, now)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return self.action(key)

    def _finish(self, key: str, state: str, response: dict, now: datetime,
                expected_states: tuple[str, ...]) -> dict:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            action = self.action(key)
            if action['state'] not in expected_states:
                raise Blocked('action state changed concurrently; reload before proceeding')
            remote_id = response.get('remote_id')
            if remote_id:
                p = action['proposal']
                ref = (p['scope'], p['operation'], remote_id)
                prior = self.db.execute('SELECT action_key FROM fba_remote_refs WHERE scope=? AND operation=? AND remote_id=?', ref).fetchone()
                if prior and prior[0] != key:
                    raise Blocked('remote object already belongs to another action')
                self.db.execute('INSERT OR IGNORE INTO fba_remote_refs VALUES (?,?,?,?)', (*ref, key))
            self.db.execute('UPDATE fba_actions SET state=?,response=? WHERE action_key=?', (state, canonical(response), key))
            self._event(key, state, response, now)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return self.action(key)

    def execute(self, key: str, client, *, enabled: bool = False,
                allowed_operations: frozenset[str] = frozenset(), now: datetime) -> dict:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            action = self.action(key)
            p, approval = action['proposal'], action['approval']
            if enabled is not True or p['operation'] not in allowed_operations:
                raise Blocked('production dispatch disabled or operation not allowlisted')
            if client.scope != p['scope']:
                raise Blocked('runtime account differs from the approved account')
            if action['state'] != 'APPROVED' or not approval:
                raise Blocked('action is not dispatchable; never replay uncertain/submitted actions')
            if approval['digest'] != action['digest'] or digest(p) != action['digest']:
                raise Blocked('approved proposal digest mismatch')
            if not instant(approval['approved_at']) <= now < instant(approval['valid_until']):
                raise Blocked('approval expired or is future-dated')
            self.validate_proposal(p, now)
            self.db.execute("UPDATE fba_actions SET state='DISPATCHING' WHERE action_key=?", (key,))
            self._event(key, 'DISPATCHING', {'digest': action['digest']}, now)
            self.db.execute('COMMIT')  # Durable claim BEFORE the network call.
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        try:
            reply = client.dispatch(p['operation'], p['ids'], p['body']).require_success()
            receipt = {'http_status': reply.status, 'request_id': reply.request_id, 'body': reply.body}
            if p['operation'] == 'createShipStationShipment':
                shipment, = reply.body['shipments']
                receipt['remote_id'] = text(shipment['shipment_id'], 'remote shipment ID')
                if reply.body.get('errors'):
                    raise ValueError('response contains errors')
            else:
                api_id(reply.body['operationId'], 'operationId', operation=True)
                if p['operation'] == 'createInboundPlan':
                    receipt['remote_id'] = api_id(reply.body['inboundPlanId'], 'inboundPlanId')
            return self._finish(key, 'SUBMITTED', receipt, now, ('DISPATCHING',))
        except ApiError as exc:
            return self._finish(key, 'UNCERTAIN' if exc.ambiguous else 'REJECTED',
                                {'http_status': exc.status, 'request_id': exc.request_id}, now, ('DISPATCHING',))
        except Exception:
            # A valid HTTP response with missing IDs is also ambiguous. Never hide a
            # potentially accepted remote mutation by returning it to the retry queue.
            return self._finish(key, 'UNCERTAIN', {'reason': 'dispatch_or_receipt_not_confirmed'}, now, ('DISPATCHING',))

    def refresh(self, key: str, client, now: datetime) -> dict:
        action = self.action(key)
        p = action['proposal']
        if action['state'] != 'SUBMITTED' or client.scope != p['scope']:
            raise Blocked('only submitted actions for this account may be refreshed')
        if p['operation'] == 'createShipStationShipment':
            remote_id = action['response']['body']['shipments'][0]['shipment_id']
            reply = client.shipment(remote_id)
            expected = {k: v for k, v in p['body']['shipments'][0].items() if k != 'create_sales_order'}
            actual = reply.body
            if actual.get('shipment_id') != remote_id:
                raise Blocked('readback shipment identity mismatch')
            # Compare the meaningful fields recursively. Extra server fields are OK.
            if not subset(expected, actual):
                raise Blocked('ShipStation readback differs; keep action unverified')
            return self._finish(key, 'REMOTE_VERIFIED', {'prior': action['response'], 'readback': actual,
                                 'request_id': reply.request_id, 'carrier_acceptance_verified': False,
                                 'amazon_receipt_verified': False}, now, ('SUBMITTED',))
        op_id = action['response']['body']['operationId']
        reply = client.operation(op_id)
        data = reply.body
        if data.get('operationId') != op_id or data.get('operation') != p['operation']:
            raise Blocked('operation result identity/name mismatch')
        status, problems = data.get('operationStatus'), data.get('operationProblems')
        if status not in ('IN_PROGRESS', 'SUCCESS', 'FAILED') or not isinstance(problems, list):
            raise Blocked('unexpected operation result schema')
        if status == 'IN_PROGRESS':
            return action
        if any(not isinstance(r, dict) or r.get('severity') not in ('WARNING', 'ERROR') for r in problems):
            raise Blocked('unexpected operation problem schema')
        failed = status == 'FAILED' or any(r['severity'] == 'ERROR' for r in problems)
        return self._finish(key, 'API_FAILED' if failed else 'API_SUCCEEDED',
                            {'prior': action['response'], 'operation_result': data, 'request_id': reply.request_id,
                             'downstream_readback_verified': False, 'amazon_receipt_verified': False}, now, ('SUBMITTED',))

    def adopt(self, key: str, client, remote_id: str, operator: str,
              evidence_ref: str, now: datetime) -> dict:
        """Recover a known created remote object via READS ONLY, never a resend.

        Operator must identify the exact remote object. Matching by name alone is
        deliberately unsupported. Stop the old worker before recovery.
        """
        action = self.action(key)
        p = action['proposal']
        text(operator, 'reconciling operator')
        text(evidence_ref, 'remote-object identification evidence')
        if action['state'] not in ('DISPATCHING', 'UNCERTAIN') or client.scope != p['scope']:
            raise Blocked('only uncertain/interrupted actions in this account can be adopted')
        dispatch_at = self.db.execute("SELECT recorded_at FROM fba_action_events WHERE action_key=? AND kind='DISPATCHING' ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
        if not dispatch_at or now - instant(dispatch_at[0]) < timedelta(minutes=5):
            raise Blocked('stop old worker and wait for the five-minute recovery grace interval')
        if p['operation'] == 'createShipStationShipment':
            reply = client.shipment(text(remote_id, 'remote shipment ID')).require_success()
            expected = {k: v for k, v in p['body']['shipments'][0].items() if k != 'create_sales_order'}
            if reply.body.get('shipment_id') != remote_id or not subset(expected, reply.body):
                raise Blocked('remote shipment does not match the approved request')
            proof = {'readback': reply.body, 'request_id': reply.request_id}
        elif p['operation'] == 'createInboundPlan':
            api_id(remote_id, 'inboundPlanId')
            reply = client.plan(remote_id).require_success()
            items = client.plan_items(remote_id)
            expected = {'inboundPlanId': remote_id, 'name': p['body']['name'],
                        'sourceAddress': p['body']['sourceAddress']}
            if not subset(expected, reply.body) or items.get('complete') is not True:
                raise Blocked('remote plan identity/source differs or items are incomplete')
            wanted = sorted(p['body']['items'], key=lambda x: x['msku'])
            observed = sorted(items['rows'], key=lambda x: x['msku'])
            if not subset(wanted, observed):
                raise Blocked('remote plan items differ from approved quantities/prep')
            proof = {'readback': reply.body, 'items_readback': items, 'request_id': reply.request_id}
        else:
            raise Blocked('recovery of packing/tracking updates requires operation-status reconciliation; adoption is create-only')
        return self._finish(key, 'REMOTE_VERIFIED',
                            {**proof, 'remote_id': remote_id, 'operator': operator, 'evidence_ref': evidence_ref,
                             'prior': action['response'], 'carrier_acceptance_verified': False,
                             'amazon_receipt_verified': False}, now, ('DISPATCHING', 'UNCERTAIN'))

    def exceptions(self) -> list[dict]:
        rows = self.db.execute("SELECT action_key,state,operation FROM fba_actions WHERE state IN ('DISPATCHING','UNCERTAIN','REJECTED','API_FAILED') ORDER BY action_key").fetchall()
        return [{'action_key': key, 'state': state, 'operation': op,
                 'next_action': 'reconcile remote state; do not resubmit automatically'} for key, state, op in rows]


def subset(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(a, b) for a, b in zip(expected, actual))
    # Do not accept true as 1, or false as 0, when checking API attributes.
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual
