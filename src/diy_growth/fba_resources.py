"""Shared integer resource ledger and mandatory warehouse release gate.

Balances include this database's HELD reservations. A count refresh must include
those holds, not report a net-free number; inventory outside this DB is reconciled
by the operator. No inferred material conversions or remotely sourced counts.
"""
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json

from .fba import Blocked, canonical, digest, instant, integer, number, text

SCHEMA = '''
CREATE TABLE IF NOT EXISTS fba_resources (
 resource_id TEXT PRIMARY KEY, kind TEXT NOT NULL, unit TEXT NOT NULL,
 balance INTEGER NOT NULL CHECK(balance>=0), revision INTEGER NOT NULL,
 observed_at TEXT NOT NULL, valid_until TEXT NOT NULL, evidence_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fba_resource_events (
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fba_recipes (
 recipe_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fba_releases (
 work_key TEXT PRIMARY KEY REFERENCES fba_work_drafts(request_key),
 state TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS fba_reservations (
 work_key TEXT NOT NULL REFERENCES fba_releases(work_key),
 resource_id TEXT NOT NULL REFERENCES fba_resources(resource_id),
 quantity INTEGER NOT NULL CHECK(quantity>0), state TEXT NOT NULL,
 PRIMARY KEY(work_key,resource_id));
CREATE TABLE IF NOT EXISTS fba_warehouse_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, work_key TEXT NOT NULL,
 recorded_at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fba_box_voids (
 box_id TEXT PRIMARY KEY REFERENCES fba_box_scans(box_id), body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fba_warehouse_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS fba_one_open_work_v3
 ON fba_work_drafts(marketplace,sku) WHERE state NOT IN ('CLOSED','CANCELLED');
'''
KINDS = {'stock', 'material', 'carton', 'packaging', 'cash', 'capacity'}


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def window(observed, expires, now):
    if not instant(observed) <= now < instant(expires):
        raise Blocked('evidence is stale, expired or future-dated')


def event(db, work_key, actor, kind, body, now):
    db.execute('INSERT INTO fba_warehouse_events(work_key,recorded_at,actor,kind,body) VALUES (?,?,?,?,?)',
               (work_key, now.isoformat(), text(actor, 'actor'), kind, canonical(body)))


def resource(db, key):
    row = db.execute('SELECT resource_id,kind,unit,balance,revision,observed_at,valid_until,evidence_ref FROM fba_resources WHERE resource_id=?', (key,)).fetchone()
    if not row:
        raise Blocked('required resource is not configured')
    out = dict(zip(('resource_id','kind','unit','balance','revision','observed_at','valid_until','evidence_ref'), row))
    held = db.execute("SELECT COALESCE(SUM(quantity),0) FROM fba_reservations WHERE resource_id=? AND state='HELD'", (key,)).fetchone()[0]
    return {**out, 'held': held, 'available': out['balance'] - held}


def set_count(db, event_id, data, actor, now):
    """Compare-and-set a reconciled count, including all local HELD reservations."""
    required = {'resource_id','kind','unit','balance_including_local_holds','expected_revision',
                'observed_at','valid_until','evidence_ref','includes_local_holds'}
    if set(data) != required or data['includes_local_holds'] is not True:
        raise Blocked('count contract requires balance INCLUDING local reservations')
    key, kind, unit = (text(data[k], k) for k in ('resource_id','kind','unit'))
    if kind not in KINDS or (kind == 'cash' and unit != 'USD_cent'):
        raise Blocked('unsupported resource kind or currency unit')
    balance = integer(data['balance_including_local_holds'], 'balance')
    if balance > 2**53 - 1:
        raise Blocked('resource count exceeds supported precision')
    rev = integer(data['expected_revision'], 'expected_revision')
    window(data['observed_at'], data['valid_until'], now)
    text(data['evidence_ref'], 'count evidence')
    fingerprint = digest({'data': data, 'actor': text(actor, 'actor')})
    with transaction(db):
        prior = db.execute('SELECT digest FROM fba_resource_events WHERE event_id=?', (text(event_id, 'event id'),)).fetchone()
        if prior:
            if prior[0] != fingerprint:
                raise Blocked('count event key reused with different content')
            return resource(db, key)
        old = db.execute('SELECT kind,unit,revision,observed_at FROM fba_resources WHERE resource_id=?', (key,)).fetchone()
        if old:
            if (kind, unit, rev) != old[:3] or instant(data['observed_at']) < instant(old[3]):
                raise Blocked('count revision, unit, kind or timestamp conflicts; refresh before retry')
        elif rev != 0:
            raise Blocked('new resource requires expected revision zero')
        # A real short count is recorded even if below holds. That creates an
        # exception and blocks release/dispatch rather than concealing shrinkage.
        db.execute('INSERT INTO fba_resources VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET balance=excluded.balance,revision=excluded.revision,observed_at=excluded.observed_at,valid_until=excluded.valid_until,evidence_ref=excluded.evidence_ref',
                   (key, kind, unit, balance, rev + 1, data['observed_at'], data['valid_until'], data['evidence_ref']))
        db.execute('INSERT INTO fba_resource_events VALUES (?,?,?)',
                   (event_id, fingerprint, canonical({'actor': actor, 'recorded_at': now.isoformat(), 'data': data})))
    return resource(db, key)


def recipe_requirements(db, recipe, plan, now):
    if set(recipe) != {'recipe_id','marketplace_id','msku','pack_version','observed_at','valid_until','evidence_ref','lines'}:
        raise Blocked('recipe fields differ from required contract')
    for key, expected in [('marketplace_id',plan['marketplace_id']), ('msku',plan['msku']),
                          ('pack_version',plan['pack_spec']['version'])]:
        if recipe[key] != expected:
            raise Blocked('resource recipe identity/package version mismatch')
    window(recipe['observed_at'], recipe['valid_until'], now)
    text(recipe['evidence_ref'], 'measured bill of materials / labor / cost evidence')
    text(recipe['recipe_id'], 'recipe id')
    if not isinstance(recipe['lines'], list) or not 4 <= len(recipe['lines']) <= 50:
        raise Blocked('recipe must cover material/stock, cartons, cash and capacity')
    kinds, allocations, seen, cash = set(), [], set(), 0
    for line in recipe['lines']:
        if set(line) != {'resource_id','unit','per_unit','per_carton','fixed'}:
            raise Blocked('invalid resource recipe line')
        key = text(line['resource_id'], 'resource id')
        if key in seen:
            raise Blocked('duplicate resource in recipe')
        seen.add(key)
        r = resource(db, key)
        if r['unit'] != line['unit']:
            raise Blocked('recipe resource unit mismatch')
        per_unit, per_carton, fixed = [integer(line[k], k) for k in ('per_unit','per_carton','fixed')]
        qty = per_unit * plan['planned_units'] + per_carton * plan['cartons'] + fixed
        if not 0 < qty <= 2**53 - 1:
            raise Blocked('recipe quantity must be positive and within supported precision')
        if r['kind'] == 'stock' and (r['unit'] != 'sellable_unit' or per_unit != 1 or per_carton or fixed):
            raise Blocked('finished stock must reserve one canonical sellable unit per unit')
        if r['kind'] == 'carton' and (r['unit'] != 'each' or qty < plan['cartons']):
            raise Blocked('carton resource must cover every planned carton')
        if r['kind'] == 'cash':
            cash += qty
        kinds.add(r['kind'])
        allocations.append({'resource_id': key, 'unit': r['unit'], 'quantity': qty})
    if not {'carton','cash','capacity'} <= kinds or not kinds & {'stock','material'}:
        raise Blocked('recipe omits required shared resource categories')
    if cash < int((number(plan['estimated_cash'], 'estimated cash') * Decimal(100)).to_integral_value(rounding=ROUND_CEILING)):
        raise Blocked('cash reservation is below the approved planned cost')
    return sorted(allocations, key=lambda r: r['resource_id'])


def guard_dispatch(db, work_key, operation, now, *, remote=True):
    """Called INSIDE Journal.execute's write lock. Legacy CLI cannot bypass it."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fba_releases'").fetchone():
        raise Blocked('warehouse release and shared resource reservations are required')
    mode = db.execute("SELECT value FROM fba_warehouse_settings WHERE name='mode'").fetchone()
    if remote and mode and mode[0] == 'DEMO':
        raise Blocked('demo warehouse cannot dispatch remote mutations')
    row = db.execute('SELECT state,body FROM fba_releases WHERE work_key=?', (work_key,)).fetchone()
    allowed = {'RELEASED','PACKING','PACKED'} if operation == 'createInboundPlan' else {'PACKED'}
    if not row or row[0] not in allowed:
        raise Blocked('work order is not released / sealed for this operation')
    release = json.loads(row[1])
    window(release['released_at'], release['valid_until'], now)
    holds = db.execute("SELECT resource_id,quantity FROM fba_reservations WHERE work_key=? AND state='HELD' ORDER BY resource_id", (work_key,)).fetchall()
    if holds != [(r['resource_id'], r['quantity']) for r in release['allocations']]:
        raise Blocked('warehouse reservations differ from reviewed release')
    for key, _ in holds:
        r = resource(db, key)
        window(r['observed_at'], r['valid_until'], now)
        if r['available'] < 0:
            raise Blocked('resource shortage after reservation; reconcile before dispatch')
