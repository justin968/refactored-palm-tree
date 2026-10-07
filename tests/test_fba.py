"""Synthetic data only. No business identifiers or actual shipping approvals."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from diy_growth.fba import Blocked, DraftStore, PackSpec, instant, number, plan_replenishment, validate_parcels

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
PAST = '2026-01-02T11:00:00Z'
FUTURE = '2026-01-03T12:00:00Z'


def inputs():
    spec = PackSpec('TEST-MARKET', 'TEST-FBA', 'TEST-ASIN', 'TEST-FNSKU', 'v1',
                    [3, 3, 5], 2.1, [12, 9, 11], 27, 12, PAST, 'test-operator', 'synthetic-measurement')
    snapshot = dict(msku=spec.msku, marketplace_id=spec.marketplace_id, evidence_ref='synthetic-snapshot',
        observed_at=PAST, positions_reconciled=True, daily_units=2, fba_fulfillable=20,
        on_time_inbound=12, open_local_work=0, warehouse_unallocated=120,
        capacity_remaining_after_commitments=120, cash_cost_per_unit='3.00', cash_remaining='360.00')
    policy = dict(max_snapshot_age_hours=4, lead_days=14, review_days=7, safety_days=7,
                  max_order_units=120, weight_tolerance_lb='0.5', dimension_tolerance_in='0.25')
    gates = {name: dict(approved=True, msku=spec.msku, asin=spec.asin,
                       marketplace_id=spec.marketplace_id, pack_version=spec.version,
                       evidence_ref='synthetic-approval', observed_at=PAST, valid_until=FUTURE)
             for name in ('identity', 'fba_offer', 'inbound_eligibility', 'dangerous_goods', 'packout', 'replenishment_policy')}
    return spec, snapshot, policy, gates


def plan():
    return plan_replenishment(*inputs(), NOW)


def boxes(p):
    return [dict(local_box_id=f'L{i}', amazon_box_id=f'A{i}', tracking_number=f'T{i}', carrier='TEST',
                 lot_ref='test-lot', packed_by='test-operator', measurement_evidence_ref='test-scale',
                 packed_at=NOW.isoformat(), msku='TEST-FBA', fnsku='TEST-FNSKU', pack_version='v1',
                 units=12, gross_lb=27, dimensions_in=[12, 9, 11]) for i in range(p['cartons'])]


def test_replenishment_net_position_and_full_cases():
    p = plan()
    assert (p['target_units'], p['inventory_position'], p['planned_units'], p['cartons']) == (56, 32, 24, 2)
    assert p['execution_enabled'] is False
    assert p['estimated_cash'] == '72.00'


@pytest.mark.parametrize('key,value', [('warehouse_unallocated', 13), ('capacity_remaining_after_commitments', 23), ('cash_remaining', '38.99')])
def test_caps_round_down_not_overcommit(key, value):
    spec, snap, policy, gates = inputs()
    snap[key] = value
    result = plan_replenishment(spec, snap, policy, gates, NOW)
    assert result['planned_units'] == 12 and result['constrained']


def test_zero_stock_no_partial_case():
    spec, snap, policy, gates = inputs()
    snap['warehouse_unallocated'] = 11
    assert plan_replenishment(spec, snap, policy, gates, NOW)['planned_units'] == 0


def test_low_demand_rounds_up_one_full_case():
    spec, snap, policy, gates = inputs()
    snap['daily_units'] = '1.2'
    assert plan_replenishment(spec, snap, policy, gates, NOW)['planned_units'] == 12


def test_no_duplicate_replenishment_for_open_work():
    spec, snap, policy, gates = inputs()
    snap['open_local_work'] = 24
    assert plan_replenishment(spec, snap, policy, gates, NOW)['planned_units'] == 0


@pytest.mark.parametrize('value', [None, True, -1, 'nan', 'inf', '-Infinity', 'oops'])
def test_invalid_numbers(value):
    with pytest.raises(Blocked):
        number(value, 'test')


@pytest.mark.parametrize('changes', [{'observed_at': '2026-01-01T11:00:00Z'}, {'observed_at': FUTURE},
                                    {'msku': 'OTHER'}, {'positions_reconciled': False}, {'warehouse_unallocated': 1.5}])
def test_snapshot_blocks(changes):
    spec, snap, policy, gates = inputs()
    snap.update(changes)
    with pytest.raises(Blocked):
        plan_replenishment(spec, snap, policy, gates, NOW)


@pytest.mark.parametrize('gate', ['identity', 'fba_offer', 'inbound_eligibility', 'dangerous_goods', 'packout', 'replenishment_policy'])
def test_every_gate_required(gate):
    spec, snap, policy, gates = inputs()
    gates[gate]['approved'] = False
    with pytest.raises(Blocked):
        plan_replenishment(spec, snap, policy, gates, NOW)


@pytest.mark.parametrize('change', [{'valid_until': PAST}, {'observed_at': FUTURE}, {'pack_version': 'v0'}, {'evidence_ref': ''}])
def test_gate_evidence_fresh_and_bound(change):
    spec, snap, policy, gates = inputs()
    gates['packout'].update(change)
    with pytest.raises(Blocked):
        plan_replenishment(spec, snap, policy, gates, NOW)


@pytest.mark.parametrize('changes', [{'carton_gross_lb': 2}, {'unit_dimensions_in': [50, 50, 50]},
    {'units_per_carton': 1.2}, {'measured_at': FUTURE}, {'carton_dimensions_in': [12, 9]}, {'fnsku': ''}])
def test_bad_measurements_block(changes):
    spec = replace(inputs()[0], **changes)
    with pytest.raises(Blocked):
        spec.validate(NOW)


def test_timezone_required():
    with pytest.raises(Blocked):
        instant('2026-01-02T12:00:00')


def test_valid_manifest_is_not_proof_of_shipping_or_receipt():
    p = plan()
    out = validate_parcels(p, boxes(p), NOW)
    assert out['status'] == 'PARCEL_MANIFEST_VALIDATED'
    assert out['carrier_acceptance_verified'] is False
    assert out['amazon_receipt_verified'] is False


@pytest.mark.parametrize('key', ['local_box_id', 'amazon_box_id', 'tracking_number'])
def test_duplicate_box_identifiers(key):
    p = plan()
    b = boxes(p)
    b[1][key] = b[0][key]
    with pytest.raises(Blocked):
        validate_parcels(p, b, NOW)


@pytest.mark.parametrize('change', [{'tracking_number': ''}, {'units': 11}, {'gross_lb': 29},
    {'dimensions_in': [18, 18, 18]}, {'fnsku': 'OTHER'}, {'packed_at': PAST}, {'lot_ref': ''}])
def test_bad_box_evidence(change):
    p = plan()
    b = boxes(p)
    b[0].update(change)
    with pytest.raises(Blocked):
        validate_parcels(p, b, NOW)


def test_box_count_mismatch():
    p = plan()
    with pytest.raises(Blocked):
        validate_parcels(p, boxes(p)[:1], NOW)


def test_draft_idempotency_and_conflict(tmp_path):
    s = DraftStore(str(tmp_path / 'state.sqlite3'))
    p = plan()
    try:
        assert s.save('request-1', p, NOW)['created']
        later = deepcopy(p)
        later['planned_at'] = (NOW + timedelta(minutes=1)).isoformat()
        assert not s.save('request-1', later, NOW + timedelta(minutes=1))['created']
        assert s.db.execute('SELECT COUNT(*) FROM fba_work_drafts').fetchone()[0] == 1
        with pytest.raises(Blocked, match='active draft'):
            s.save('request-2', p, NOW)
        changed = plan_replenishment(inputs()[0], {**inputs()[1], 'daily_units': 3}, inputs()[2], inputs()[3], NOW)
        with pytest.raises(Blocked, match='request key reused'):
            s.save('request-1', changed, NOW)
    finally:
        s.close()


def test_tampered_plan_blocked(tmp_path):
    s = DraftStore(str(tmp_path / 'state.sqlite3'))
    p = plan()
    p['planned_units'] = 120
    try:
        with pytest.raises(Blocked):
            s.save('tampered', p, NOW)
    finally:
        s.close()


def test_pack_revision_immutable(tmp_path):
    s = DraftStore(str(tmp_path / 'state.sqlite3'))
    spec, snap, policy, gates = inputs()
    try:
        s.save('first', plan(), NOW)
        updated = plan_replenishment(replace(spec, carton_gross_lb=28), snap, policy, gates, NOW)
        with pytest.raises(Blocked, match='immutable'):
            s.save('second', updated, NOW)
    finally:
        s.close()
