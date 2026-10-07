"""Physical packout before labels, immutable scans, and explicit API payloads."""
from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from .fba import (Blocked, DraftStore, PackSpec, canonical, digest, dimensions,
                  instant, integer, number, require_gates, text, validate_parcels)
from .fba_api import api_id


def validate_box(plan: dict, box: dict, now: datetime) -> None:
    spec = PackSpec(**plan['pack_spec'])
    spec.validate(now)
    require_gates(plan['gates'], spec, now)
    for name in ('local_box_id', 'lot_ref', 'packed_by', 'measurement_evidence_ref'):
        text(box.get(name), name)
    if not instant(plan['planned_at']) <= instant(box.get('packed_at')) <= now:
        raise Blocked('packing timestamp outside work order/current interval')
    for field, expected in (('msku', spec.msku), ('fnsku', spec.fnsku), ('pack_version', spec.version)):
        if box.get(field) != expected:
            raise Blocked('box identity or packaging revision mismatch')
    units = integer(box.get('units'), 'units', positive=True)
    if units != integer(spec.units_per_carton, 'units_per_carton', positive=True):
        raise Blocked('partial/mixed cartons are not supported')
    weight = number(box.get('gross_lb'), 'gross_lb', positive=True)
    if weight < number(spec.unit_gross_lb, 'unit_gross_lb') * units:
        raise Blocked('gross carton weight below contained units')
    if abs(weight - number(spec.carton_gross_lb, 'carton_gross_lb')) > number(plan['policy'].get('weight_tolerance_lb'), 'weight tolerance'):
        raise Blocked('packed weight outside approved tolerance')
    actual = sorted(dimensions(box.get('dimensions_in'), 'actual dimensions'))
    approved = sorted(dimensions(spec.carton_dimensions_in, 'approved dimensions'))
    tolerance = number(plan['policy'].get('dimension_tolerance_in'), 'dimension tolerance')
    if any(abs(a - b) > tolerance for a, b in zip(actual, approved)):
        raise Blocked('packed dimensions outside approved tolerance')


def validate_packout(plan: dict, boxes: list[dict], now: datetime) -> None:
    count = integer(plan.get('cartons'), 'cartons', positive=True)
    if len(boxes) != count or sum(integer(b.get('units'), 'units') for b in boxes) != plan['planned_units']:
        raise Blocked('incomplete packout or unit mismatch')
    seen = set()
    for box in boxes:
        validate_box(plan, box, now)
        if box['local_box_id'] in seen:
            raise Blocked('duplicate local carton')
        seen.add(box['local_box_id'])


class WarehouseStore(DraftStore):
    """Trusted local operator input; not a publicly authenticated warehouse service."""
    def __init__(self, path: str):
        super().__init__(path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS fba_box_scans (
          box_id TEXT PRIMARY KEY, work_key TEXT NOT NULL REFERENCES fba_work_drafts(request_key),
          digest TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fba_box_links (
          box_id TEXT PRIMARY KEY REFERENCES fba_box_scans(box_id),
          amazon_box_id TEXT NOT NULL UNIQUE, carrier TEXT NOT NULL, tracking TEXT NOT NULL,
          body TEXT NOT NULL, UNIQUE(carrier,tracking));
        ''')

    def work(self, key: str) -> dict:
        row = self.db.execute('SELECT body FROM fba_work_drafts WHERE request_key=?', (text(key, 'work key'),)).fetchone()
        if not row:
            raise Blocked('work order not found; save a validated planner draft first')
        return json.loads(row[0])

    def capture(self, work_key: str, box: dict, now: datetime) -> dict:
        plan = self.work(work_key)
        validate_box(plan, box, now)
        # Physical records must not smuggle a shipping claim into a scan.
        if any(k in box for k in ('amazon_box_id', 'tracking_number', 'carrier', 'delivered', 'received')):
            raise Blocked('record physical scan first; link shipping identifiers separately')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior = self.db.execute('SELECT work_key,digest FROM fba_box_scans WHERE box_id=?', (box['local_box_id'],)).fetchone()
            if prior:
                if prior != (work_key, digest(box)):
                    raise Blocked('scan is immutable or barcode belongs to another work order')
                self.db.execute('COMMIT')
                return {'created': False, 'box_id': box['local_box_id']}
            n = self.db.execute('SELECT COUNT(*) FROM fba_box_scans WHERE work_key=?', (work_key,)).fetchone()[0]
            if n >= plan['cartons']:
                raise Blocked('all planned cartons already captured')
            self.db.execute('INSERT INTO fba_box_scans VALUES (?,?,?,?)', (box['local_box_id'], work_key, digest(box), canonical(box)))
            self.db.execute('COMMIT')
            return {'created': True, 'box_id': box['local_box_id']}
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def link(self, box_id: str, link: dict, now: datetime) -> dict:
        row = self.db.execute('SELECT body FROM fba_box_scans WHERE box_id=?', (box_id,)).fetchone()
        if not row:
            raise Blocked('physical carton not captured')
        box = json.loads(row[0])
        for name in ('amazon_box_id', 'carrier', 'tracking_number', 'evidence_ref', 'linked_by'):
            text(link.get(name), name)
        if not instant(box['packed_at']) <= instant(link.get('linked_at')) <= now:
            raise Blocked('invalid linkage timestamp')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior = self.db.execute('SELECT body FROM fba_box_links WHERE box_id=?', (box_id,)).fetchone()
            if prior:
                if prior[0] != canonical(link):
                    raise Blocked('existing carton linkage differs; reconcile rather than overwrite')
                self.db.execute('COMMIT')
                return {'created': False}
            self.db.execute('INSERT INTO fba_box_links VALUES (?,?,?,?,?)',
                            (box_id, link['amazon_box_id'], link['carrier'], link['tracking_number'], canonical(link)))
            self.db.execute('COMMIT')
            return {'created': True}
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise Blocked('Amazon box or carrier tracking already assigned to another carton') from None
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def boxes(self, work_key: str, *, linked: bool = False) -> list[dict]:
        rows = self.db.execute('SELECT s.body,l.body FROM fba_box_scans s LEFT JOIN fba_box_links l USING(box_id) WHERE s.work_key=? ORDER BY s.box_id', (work_key,)).fetchall()
        if linked and any(b is None for _, b in rows):
            raise Blocked('some cartons still lack individual tracking links')
        return [{**json.loads(a), **(json.loads(b) if linked and b else {})} for a, b in rows]


def item(plan: dict, quantity: int, *, prep_owner: str, label_owner: str,
         expiration: str | None = None, manufacturing_lot: str | None = None) -> dict:
    # Initial US workflow is seller-prepared/labeled or explicitly not required.
    # Do not automatically choose Amazon services or derive expiration from retest dates.
    if prep_owner not in ('SELLER', 'NONE') or label_owner not in ('SELLER', 'NONE'):
        raise Blocked('current US workflow requires explicit SELLER/NONE ownership')
    out = {'msku': plan['msku'], 'quantity': integer(quantity, 'quantity', positive=True),
           'prepOwner': prep_owner, 'labelOwner': label_owner}
    if expiration is not None:
        from datetime import date
        try:
            if date.fromisoformat(expiration).isoformat() != expiration:
                raise ValueError()
        except (ValueError, TypeError):
            raise Blocked('explicit ISO expiration date required') from None
        out['expiration'] = expiration
    if manufacturing_lot is not None:
        out['manufacturingLotCode'] = text(manufacturing_lot, 'manufacturing lot code')
    return out


def inbound_plan_payload(plan: dict, source_address: dict, name: str, now: datetime, **prep) -> dict:
    spec = PackSpec(**plan['pack_spec'])
    spec.validate(now)
    require_gates(plan['gates'], spec, now)
    for field in ('name', 'addressLine1', 'city', 'stateOrProvinceCode', 'postalCode', 'countryCode', 'phoneNumber'):
        text(source_address.get(field), 'sourceAddress.' + field)
    if len(text(name, 'plan name')) > 40:
        raise Blocked('inbound plan name exceeds 40 characters')
    return {'destinationMarketplaces': [spec.marketplace_id], 'name': name,
            'sourceAddress': source_address,
            'items': [item(plan, plan['planned_units'], **prep)]}


def packing_payload(plan: dict, boxes: list[dict], grouping: dict, now: datetime, **prep) -> dict:
    validate_packout(plan, boxes, now)
    if set(grouping) not in ({'packingGroupId'}, {'shipmentId'}):
        raise Blocked('exactly one confirmed packing group or shipment assignment required')
    key = next(iter(grouping))
    api_id(grouping[key], key)
    packed = []
    for box in boxes:
        length, width, height = (float(v) for v in dimensions(box['dimensions_in'], 'box dimensions'))
        packed.append({'contentInformationSource': 'BOX_CONTENT_PROVIDED', 'quantity': 1,
                       'dimensions': {'unitOfMeasurement': 'IN', 'length': length, 'width': width, 'height': height},
                       'weight': {'unit': 'LB', 'value': float(number(box['gross_lb'], 'weight', positive=True))},
                       'items': [item(plan, box['units'], **prep)]})
    # Local IDs are not Amazon box IDs. Reconcile returned Amazon boxes explicitly.
    return {'packageGroupings': [{**grouping, 'boxes': packed}]}


def tracking_payload(plan: dict, linked_boxes: list[dict], now: datetime) -> dict:
    validate_parcels(plan, linked_boxes, now)
    return {'trackingDetails': {'spdTrackingDetail': {'spdTrackingItems': [
        {'boxId': b['amazon_box_id'], 'trackingId': b['tracking_number']} for b in linked_boxes]}}}


def shipstation_payload(plan: dict, boxes: list[dict], external_id: str,
                        ship_from: dict, ship_to: dict, now: datetime) -> dict:
    validate_packout(plan, boxes, now)
    if len(text(external_id, 'external shipment ID')) > 50:
        raise Blocked('external shipment ID exceeds 50 characters')
    for address in (ship_from, ship_to):
        for field in ('name', 'address_line1', 'city_locality', 'state_province', 'postal_code', 'country_code'):
            text(address.get(field), field)
    packages = []
    for box in boxes:
        length, width, height = map(float, dimensions(box['dimensions_in'], 'dimensions'))
        packages.append({'external_package_id': box['local_box_id'],
                         'weight': {'unit': 'pound', 'value': float(number(box['gross_lb'], 'gross lb', positive=True))},
                         'dimensions': {'unit': 'inch', 'length': length, 'width': width, 'height': height}})
    return {'shipments': [{'external_shipment_id': external_id, 'shipment_number': external_id,
                          'shipment_status': 'pending', 'create_sales_order': False,
                          'ship_from': ship_from, 'ship_to': ship_to, 'packages': packages}]}
