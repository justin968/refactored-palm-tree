"""Synthetic contract/failure tests only. No network, credentials or real shipments."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from test_fba import NOW, FUTURE, PAST, plan, boxes
from diy_growth.fba import Blocked
from diy_growth.fba_api import Amazon, ApiError, HttpTransport, LWAToken, Reply, ShipStation, api_id
from diy_growth.fba_execution import Journal, OPERATIONS, subset
from diy_growth.fba_packout import (inbound_plan_payload, packing_payload, shipstation_payload,
                                    tracking_payload, validate_packout)
from diy_growth.fba_ops import secure_file

PID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
SID = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
GID = 'pg1234abcd-1234-abcd-5678-1234abcd5678'
OID = '1234abcd-1234-abcd-5678-1234abcd5678'
SCOPE = 'amazon:NA:TEST-SELLER:TEST-MARKET'
SOURCE = {'name': 'Synthetic warehouse', 'addressLine1': '1 Test Street', 'city': 'Test City',
          'stateOrProvinceCode': 'TX', 'postalCode': '00000', 'countryCode': 'US', 'phoneNumber': '0000000000'}
SS_ADDRESS = {'name': 'Synthetic destination', 'address_line1': '1 Test Street', 'city_locality': 'Test City',
              'state_province': 'TX', 'postal_code': '00000', 'country_code': 'US'}
PREP = {'prep_owner': 'SELLER', 'label_owner': 'SELLER'}


class Script:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Token:
    def get(self):
        return 'synthetic-access-token'


def amazon(script):
    return Amazon(Token(), 'TEST-SELLER', 'TEST-MARKET', script, sleep=lambda _: None)


def scans():
    return [{k: v for k, v in b.items() if k not in ('amazon_box_id', 'tracking_number', 'carrier')} for b in boxes(plan())]


@pytest.fixture
def journal(tmp_path):
    j = Journal(str(tmp_path / 'state.sqlite3'))
    j.save('work-1', plan(), NOW)
    try:
        yield j
    finally:
        j.close()


def capture_all(j):
    for b in scans():
        j.capture('work-1', b, NOW)


def proposal(operation='createInboundPlan'):
    return dict(scope=SCOPE, operation=operation, work_key='work-1', ids={},
                body=inbound_plan_payload(plan(), SOURCE, 'synthetic-work-1', NOW, **PREP),
                evidence_ref='synthetic-current-Amazon-prep-and-assignment-read', valid_until=FUTURE)


def approved(j):
    staged = j.stage('action-1', proposal(), NOW)
    return j.approve('action-1', staged['digest'], 'synthetic-operator', 'synthetic-review', FUTURE, NOW)


class MutationClient:
    scope = SCOPE

    def __init__(self, response=None):
        self.calls = []
        self.response = response or Reply(202, {'inboundPlanId': PID, 'operationId': OID}, 'synthetic-request')

    def dispatch(self, *args):
        self.calls.append(args)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def operation(self, op_id):
        return Reply(200, dict(operationId=op_id, operation='createInboundPlan', operationStatus='SUCCESS', operationProblems=[]))


def dispatch(j, client=None):
    return j.execute('action-1', client or MutationClient(), now=NOW, enabled=True, allowed_operations=OPERATIONS)


def test_token_cache_refresh_and_secret_safe_repr():
    script = Script(Reply(200, dict(access_token='secret-1', expires_in=3600, token_type='bearer')),
                    Reply(200, dict(access_token='secret-2', expires_in=3600, token_type='bearer')))
    clock = [0]
    token = LWAToken('synthetic-client', 'synthetic-secret', 'synthetic-refresh', script, clock=lambda: clock[0])
    assert token.get() == token.get() == 'secret-1'
    assert len(script.calls) == 1
    clock[0] = 3541
    assert token.get() == 'secret-2'
    assert 'synthetic-secret' not in repr(token)
    assert 'synthetic-refresh' not in repr(token)
    params = parse_qs(script.calls[0][3].decode())
    assert params['grant_type'] == ['refresh_token']


def test_missing_credentials_fail_closed(monkeypatch):
    for key in ('FBA_LWA_CLIENT_ID', 'FBA_LWA_CLIENT_SECRET', 'FBA_LWA_REFRESH_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Blocked):
        LWAToken.from_env(Script())


@pytest.mark.parametrize('body', [{}, {'access_token': 'secret', 'expires_in': -1, 'token_type': 'bearer'},
                                 {'access_token': 'secret', 'expires_in': 3600, 'token_type': 'other'}])
def test_bad_token_response_not_leaked(body):
    with pytest.raises(ApiError) as exc:
        LWAToken('client', 'secret', 'refresh', Script(Reply(200, body))).get()
    assert 'secret' not in str(exc.value)


def test_transport_blocks_unapproved_host_before_io():
    with pytest.raises(Blocked):
        HttpTransport()('GET', 'https://example.invalid/', {'api-key': 'secret'}, None)


def test_inventory_all_pages_and_no_false_zero_defaults():
    script = Script(Reply(200, {'payload': {'inventorySummaries': [{'sellerSku': 'A'}]}, 'pagination': {'nextToken': 'token'}}, 'r1'),
                    Reply(200, {'payload': {'inventorySummaries': [{'sellerSku': 'B'}]}}, 'r2'))
    data = amazon(script).inventory()
    assert data['complete'] and not data['atomic_snapshot'] and data['started_at']
    assert [r['sellerSku'] for r in data['rows']] == ['A', 'B']
    assert data['request_ids'] == ['r1', 'r2']
    query = parse_qs(urlsplit(script.calls[1][1]).query)
    assert query['nextToken'] == ['token'] and query['details'] == ['true']
    assert 'fba_fulfillable' not in data


def test_inbound_pagination_uses_distinct_query_parameter():
    script = Script(Reply(200, {'inboundPlans': [], 'pagination': {'nextToken': 'a'}}),
                    Reply(200, {'inboundPlans': []}))
    amazon(script).inbound_plans()
    assert 'paginationToken=a' in script.calls[1][1]


@pytest.mark.parametrize('bad', [{}, {'payload': {}}, {'payload': {'inventorySummaries': None}}, {'payload': {'inventorySummaries': [1]}}])
def test_bad_inventory_schema_is_not_empty_inventory(bad):
    with pytest.raises(Blocked):
        amazon(Script(Reply(200, bad))).inventory()


def test_repeated_page_token_is_incomplete():
    r = Reply(200, {'payload': {'inventorySummaries': []}, 'pagination': {'nextToken': 'same'}})
    with pytest.raises(Blocked, match='repeated'):
        amazon(Script(r, r)).inventory()


def test_read_retries_bounded_and_failures_never_partial_success():
    script = Script(Reply(503, {}), Reply(429, {}, retry_after=0), Reply(200, {'payload': {'inventorySummaries': []}}))
    assert amazon(script).inventory()['complete'] and len(script.calls) == 3
    script = Script(Reply(503, {}), Reply(503, {}), Reply(503, {}))
    with pytest.raises(ApiError):
        amazon(script).inventory()
    assert len(script.calls) == 3


def test_write_is_single_attempt_even_on_503():
    script = Script(Reply(503, {}))
    with pytest.raises(ApiError):
        amazon(script).dispatch('createInboundPlan', {}, proposal()['body'])
    assert len(script.calls) == 1


def test_sku_path_is_percent_encoded():
    script = Script(Reply(200, {}))
    amazon(script).listing('TEST/A+B %')
    assert '/TEST%2FA%2BB%20%25?' in script.calls[0][1]


@pytest.mark.parametrize('bad', ['FBA19ABCDEF', '', None, PID + '/other', 'x' * 36])
def test_legacy_or_bad_plan_id_blocked(bad):
    with pytest.raises(Blocked):
        api_id(bad, 'inboundPlanId')


def test_no_purchase_or_listing_write_endpoints():
    with pytest.raises(Blocked):
        amazon(Script()).dispatch('purchaseLabel', {'inboundPlanId': PID}, {})
    with pytest.raises(Blocked):
        ShipStation('synthetic', 'test', Script()).dispatch('purchaseLabel', {}, {})


def test_physical_scan_before_tracking_and_idempotent(journal):
    b = scans()[0]
    assert journal.capture('work-1', b, NOW)['created']
    assert not journal.capture('work-1', b, NOW)['created']
    assert journal.boxes('work-1') == [b]
    with pytest.raises(Blocked):
        journal.boxes('work-1', linked=True)


@pytest.mark.parametrize('change', [{'fnsku': 'wrong'}, {'gross_lb': 4}, {'units': 11},
                                    {'lot_ref': ''}, {'dimensions_in': [100, 100, 100]}, {'packed_at': PAST},
                                    {'tracking_number': 'premature'}])
def test_bad_physical_scan_rejected(journal, change):
    with pytest.raises(Blocked):
        journal.capture('work-1', {**scans()[0], **change}, NOW)


def test_scan_cannot_change_or_overfill(journal):
    capture_all(journal)
    with pytest.raises(Blocked):
        journal.capture('work-1', {**scans()[0], 'gross_lb': 27.1}, NOW)
    with pytest.raises(Blocked):
        journal.capture('work-1', {**scans()[0], 'local_box_id': 'third'}, NOW)


def link(i=0):
    return dict(amazon_box_id=f'AMAZON-TEST-{i}', tracking_number=f'TRACK-TEST-{i}', carrier='TEST',
                evidence_ref='synthetic-label-scan', linked_by='synthetic-operator', linked_at=NOW.isoformat())


def test_carton_links_unique_and_immutable(journal):
    capture_all(journal)
    assert journal.link('L0', link(), NOW)['created']
    assert not journal.link('L0', link(), NOW)['created']
    with pytest.raises(Blocked):
        journal.link('L1', link(), NOW)
    with pytest.raises(Blocked):
        journal.link('L0', link(1), NOW)
    journal.link('L1', link(1), NOW)
    payload = tracking_payload(plan(), journal.boxes('work-1', linked=True), NOW)
    assert len(payload['trackingDetails']['spdTrackingDetail']['spdTrackingItems']) == 2


def test_packing_uses_actual_cartons_not_units_as_box_quantity():
    p = packing_payload(plan(), scans(), {'packingGroupId': GID}, NOW, **PREP)
    assert len(p['packageGroupings'][0]['boxes']) == 2
    assert all(b['quantity'] == 1 and b['items'][0]['quantity'] == 12 for b in p['packageGroupings'][0]['boxes'])
    assert 'local_box_id' not in json.dumps(p)


@pytest.mark.parametrize('group', [{}, {'shipmentId': SID, 'packingGroupId': GID}, {'shipmentId': 'FBA123'}])
def test_packing_assignment_must_be_explicit(group):
    with pytest.raises(Blocked):
        packing_payload(plan(), scans(), group, NOW, **PREP)


def test_incomplete_packout_blocked():
    with pytest.raises(Blocked):
        validate_packout(plan(), scans()[:1], NOW)


def test_us_prep_services_not_guessed():
    with pytest.raises(Blocked):
        inbound_plan_payload(plan(), SOURCE, 'test', NOW, prep_owner='AMAZON', label_owner='AMAZON')


def test_shipstation_payload_no_sales_order_no_label_purchase():
    data = shipstation_payload(plan(), scans(), 'test-work-1', SS_ADDRESS, SS_ADDRESS, NOW)['shipments'][0]
    assert data['shipment_status'] == 'pending'
    assert data['create_sales_order'] is False
    assert len(data['packages']) == 2
    assert len({b['external_package_id'] for b in data['packages']}) == 2


def test_stage_duplicate_and_changed_proposal(journal):
    a = journal.stage('a', proposal(), NOW)
    assert journal.stage('a', proposal(), NOW) == a
    with pytest.raises(Blocked):
        journal.stage('different-key', proposal(), NOW)
    p = proposal()
    p['body']['name'] = 'changed'
    with pytest.raises(Blocked):
        journal.stage('a', p, NOW)


@pytest.mark.parametrize('change', [{'operation': 'purchaseLabel'}, {'scope': 'amazon:NA:wrong:WRONG-MARKET'},
                                   {'valid_until': PAST}, {'evidence_ref': ''}])
def test_invalid_proposal(journal, change):
    with pytest.raises(Blocked):
        journal.stage('a', {**proposal(), **change}, NOW)


def test_payload_cannot_change_work_quantity(journal):
    p = proposal()
    p['body']['items'][0]['quantity'] = 999
    with pytest.raises(Blocked):
        journal.stage('a', p, NOW)


def test_approval_exact_digest_and_expiry(journal):
    a = journal.stage('a', proposal(), NOW)
    with pytest.raises(Blocked):
        journal.approve('a', 'wrong', 'op', 'evidence', FUTURE, NOW)
    with pytest.raises(Blocked):
        journal.approve('a', a['digest'], 'op', 'evidence', PAST, NOW)
    with pytest.raises(Blocked):
        journal.approve('a', a['digest'], 'op', 'evidence', '2030-01-01T00:00:00Z', NOW)


def test_dispatch_off_by_default(journal):
    approved(journal)
    client = MutationClient()
    with pytest.raises(Blocked):
        journal.execute('action-1', client, now=NOW)
    assert not client.calls


def test_dispatch_wrong_scope_or_expired_approval(journal):
    approved(journal)
    client = MutationClient()
    client.scope = 'amazon:NA:OTHER:TEST-MARKET'
    with pytest.raises(Blocked):
        dispatch(journal, client)
    client.scope = SCOPE
    with pytest.raises(Blocked):
        journal.execute('action-1', client, enabled=True, allowed_operations=OPERATIONS, now=NOW + timedelta(days=2))
    assert not client.calls


def test_success_is_submitted_not_received_and_never_replayed(journal):
    approved(journal)
    client = MutationClient()
    assert dispatch(journal, client)['state'] == 'SUBMITTED'
    with pytest.raises(Blocked):
        dispatch(journal, client)
    assert len(client.calls) == 1
    polled = journal.refresh('action-1', client, NOW)
    assert polled['state'] == 'API_SUCCEEDED'
    assert polled['response']['downstream_readback_verified'] is False
    assert polled['response']['amazon_receipt_verified'] is False


@pytest.mark.parametrize('reply,state', [(ApiError(0), 'UNCERTAIN'), (ApiError(503), 'UNCERTAIN'),
                                        (ApiError(403), 'REJECTED'), (ApiError(429), 'REJECTED'),
                                        (Reply(202, {}), 'UNCERTAIN'), (Reply(500, {}), 'UNCERTAIN')])
def test_failure_never_auto_retries(journal, reply, state):
    approved(journal)
    client = MutationClient(reply)
    assert dispatch(journal, client)['state'] == state
    with pytest.raises(Blocked):
        dispatch(journal, client)
    assert len(client.calls) == 1
    assert journal.exceptions()[0]['state'] == state


def test_process_crash_leaves_durable_reconcile_marker(journal):
    approved(journal)
    with pytest.raises(KeyboardInterrupt):
        dispatch(journal, MutationClient(KeyboardInterrupt()))
    assert journal.action('action-1')['state'] == 'DISPATCHING'
    path = journal.db.execute('PRAGMA database_list').fetchone()[2]
    reopened = Journal(path)
    try:
        with pytest.raises(Blocked):
            dispatch(reopened)
        assert reopened.exceptions()[0]['state'] == 'DISPATCHING'
    finally:
        reopened.close()


def test_two_workers_only_one_dispatch(journal):
    approved(journal)
    path = journal.db.execute('PRAGMA database_list').fetchone()[2]
    client = MutationClient()
    def run():
        j = Journal(path)
        try:
            return dispatch(j, client)['state']
        except Blocked:
            return 'BLOCKED'
        finally:
            j.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ['BLOCKED', 'SUBMITTED']
    assert len(client.calls) == 1


@pytest.mark.parametrize('status,problems,state', [
    ('FAILED', [], 'API_FAILED'), ('SUCCESS', [{'severity': 'ERROR', 'code': 'TEST'}], 'API_FAILED'),
    ('SUCCESS', [{'severity': 'WARNING', 'code': 'TEST'}], 'API_SUCCEEDED'),
    ('IN_PROGRESS', [], 'SUBMITTED')])
def test_async_outcome_not_http_status(journal, status, problems, state):
    approved(journal)
    c = MutationClient()
    dispatch(journal, c)
    c.operation = lambda op_id: Reply(200, dict(operationId=op_id, operation='createInboundPlan', operationStatus=status, operationProblems=problems))
    assert journal.refresh('action-1', c, NOW)['state'] == state


def test_async_operation_id_must_match(journal):
    approved(journal)
    c = MutationClient()
    dispatch(journal, c)
    c.operation = lambda _: Reply(200, dict(operationId='wrong', operation='createInboundPlan', operationStatus='SUCCESS', operationProblems=[]))
    with pytest.raises(Blocked):
        journal.refresh('action-1', c, NOW)
    assert journal.action('action-1')['state'] == 'SUBMITTED'


def test_shipstation_stage_and_remote_readback(journal):
    capture_all(journal)
    p = proposal('createShipStationShipment')
    p['scope'] = 'shipstation:TEST'
    p['body'] = shipstation_payload(plan(), scans(), 'test-work-1', SS_ADDRESS, SS_ADDRESS, NOW)
    staged = journal.stage('action-1', p, NOW)
    journal.approve('action-1', staged['digest'], 'test', 'evidence', FUTURE, NOW)
    c = MutationClient(Reply(200, {'shipments': [{'shipment_id': 'se-test'}]}))
    c.scope = 'shipstation:TEST'
    dispatch(journal, c)
    persisted = {k: v for k, v in p['body']['shipments'][0].items() if k != 'create_sales_order'}
    c.shipment = lambda _: Reply(200, {**persisted, 'shipment_id': 'se-test'})
    assert journal.refresh('action-1', c, NOW)['state'] == 'REMOTE_VERIFIED'


def test_shipstation_external_id_not_idempotency_key(journal):
    capture_all(journal)
    p = proposal('createShipStationShipment')
    p['scope'] = 'shipstation:TEST'
    p['body'] = shipstation_payload(plan(), scans(), 'test-work-1', SS_ADDRESS, SS_ADDRESS, NOW)
    journal.stage('a', p, NOW)
    p['body']['shipments'][0]['external_shipment_id'] = 'different-id'
    p['body']['shipments'][0]['shipment_number'] = 'different-id'
    with pytest.raises(Blocked):
        journal.stage('b', p, NOW)


def test_private_file_mode_and_no_silent_overwrite(tmp_path):
    p = tmp_path / 'private' / 'state.json'
    secure_file(p, exclusive=True)
    assert p.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        secure_file(p, exclusive=True)


def test_symlink_output_blocked(tmp_path):
    target = tmp_path / 'target'
    target.write_text('keep')
    link_path = tmp_path / 'link'
    link_path.symlink_to(target)
    with pytest.raises(Blocked):
        secure_file(link_path)
    assert target.read_text() == 'keep'


def test_boolean_readback_not_equal_to_numeric():
    assert not subset({'created': False}, {'created': 0})
    assert subset({'weight': {'value': 27}}, {'weight': {'value': 27.0, 'unit': 'pound'}})


def test_recover_interrupted_create_by_authenticated_readback_only(journal):
    approved(journal)
    c = MutationClient(ApiError(0))
    dispatch(journal, c)
    c.plan = lambda _: Reply(200, {'inboundPlanId': PID, 'name': 'synthetic-work-1', 'sourceAddress': SOURCE})
    c.plan_items = lambda _: {'complete': True, 'rows': proposal()['body']['items']}
    result = journal.adopt('action-1', c, PID, 'test-operator', 'exact-remote-plan-evidence', NOW + timedelta(minutes=6))
    assert result['state'] == 'REMOTE_VERIFIED'
    assert result['response']['amazon_receipt_verified'] is False
    assert len(c.calls) == 1
    with pytest.raises(Blocked):
        dispatch(journal, c)


def test_recovery_waits_for_interrupted_worker(journal):
    approved(journal)
    c = MutationClient(ApiError(0))
    dispatch(journal, c)
    with pytest.raises(Blocked, match='grace'):
        journal.adopt('action-1', c, PID, 'op', 'evidence', NOW)


@pytest.mark.parametrize('difference', ['source', 'quantity', 'name', 'incomplete'])
def test_recovery_cannot_adopt_similar_or_partial_plan(journal, difference):
    approved(journal)
    c = MutationClient(ApiError(0))
    dispatch(journal, c)
    remote = {'inboundPlanId': PID, 'name': 'synthetic-work-1', 'sourceAddress': deepcopy(SOURCE)}
    items = {'complete': True, 'rows': deepcopy(proposal()['body']['items'])}
    if difference == 'source':
        remote['sourceAddress']['postalCode'] = 'WRONG'
    elif difference == 'quantity':
        items['rows'][0]['quantity'] = 23
    elif difference == 'name':
        remote['name'] = 'other-plan'
    else:
        items['complete'] = False
    c.plan = lambda _: Reply(200, remote)
    c.plan_items = lambda _: items
    with pytest.raises(Blocked):
        journal.adopt('action-1', c, PID, 'op', 'evidence', NOW + timedelta(minutes=6))
    assert journal.action('action-1')['state'] == 'UNCERTAIN'


def test_stage_packing_payload_from_physical_scan(journal):
    capture_all(journal)
    p = proposal('setPackingInformation')
    p['ids'] = {'inboundPlanId': PID}
    p['body'] = packing_payload(plan(), scans(), {'packingGroupId': GID}, NOW, **PREP)
    assert journal.stage('packing', p, NOW)['state'] == 'STAGED'


def test_stage_tracking_only_after_all_box_links(journal):
    capture_all(journal)
    for i in range(2):
        journal.link(f'L{i}', link(i), NOW)
    p = proposal('updateShipmentTrackingDetails')
    p['ids'] = {'inboundPlanId': PID, 'shipmentId': SID}
    p['body'] = tracking_payload(plan(), journal.boxes('work-1', linked=True), NOW)
    assert journal.stage('tracking', p, NOW)['state'] == 'STAGED'


def test_duplicate_remote_plan_id_cannot_confirm_two_work_orders(journal):
    from test_fba import inputs
    from diy_growth.fba import plan_replenishment
    from dataclasses import replace
    approved(journal)
    assert dispatch(journal)['state'] == 'SUBMITTED'
    spec, snapshot, policy, gates = inputs()
    spec = replace(spec, msku='TEST-FBA-2')
    snapshot['msku'] = spec.msku
    for g in gates.values():
        g['msku'] = spec.msku
    other = plan_replenishment(spec, snapshot, policy, gates, NOW)
    journal.save('work-2', other, NOW)
    p = proposal()
    p['work_key'] = 'work-2'
    p['body'] = inbound_plan_payload(other, SOURCE, 'synthetic-work-2', NOW, **PREP)
    a = journal.stage('action-2', p, NOW)
    journal.approve('action-2', a['digest'], 'op', 'evidence', FUTURE, NOW)
    out = journal.execute('action-2', MutationClient(), enabled=True, allowed_operations=OPERATIONS, now=NOW)
    assert out['state'] == 'UNCERTAIN'
