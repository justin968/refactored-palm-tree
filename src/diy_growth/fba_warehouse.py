"""Warehouse work-order release, shared reservations and auditable carton capture.

No remote API writes. All actor/role enforcement belongs to the authenticated
station or trusted local CLI; stock counts and recipes remain supplied evidence.
"""
import json
from .fba import Blocked, PackSpec, canonical, digest, instant, integer, plan_replenishment, require_gates, text
from .fba_execution import Journal
from .fba_packout import validate_box, validate_packout, packing_payload
from .fba_resources import (SCHEMA, event, guard_dispatch, recipe_requirements, resource,
                            set_count, transaction, window)


class Warehouse(Journal):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript(SCHEMA)

    def import_request(self, request, now):
        p = plan_replenishment(PackSpec(**request['pack_spec']), request['snapshot'], request['policy'], request['gates'], now)
        if not p['planned_units']:
            raise Blocked('no full-case replenishment needed/available')
        return self.save(request['request_key'], p, now)

    def count(self, event_id, data, actor, now):
        return set_count(self.db, event_id, data, actor, now)

    def add_recipe(self, data, work_key, actor, now):
        recipe_requirements(self.db, data, self.work(work_key), now)
        with transaction(self.db):
            prior = self.db.execute('SELECT digest FROM fba_recipes WHERE recipe_id=?', (data['recipe_id'],)).fetchone()
            if prior and prior[0] != digest(data):
                raise Blocked('recipe revision is immutable; create a new recipe ID')
            if not prior:
                self.db.execute('INSERT INTO fba_recipes VALUES (?,?,?)', (data['recipe_id'], digest(data), canonical(data)))
                event(self.db, work_key, actor, 'RECIPE_RECORDED', {'recipe_id':data['recipe_id'],'evidence_ref':data['evidence_ref']}, now)
        return {'recipe_id': data['recipe_id'], 'digest': digest(data)}

    def allocations(self, work_key, recipe_id, now):
        row = self.db.execute('SELECT body FROM fba_recipes WHERE recipe_id=?', (recipe_id,)).fetchone()
        if not row:
            raise Blocked('reviewed recipe is missing')
        return recipe_requirements(self.db, json.loads(row[0]), self.work(work_key), now)

    def release(self, work_key, recipe_id, expected_plan_digest, owner, deadline, evidence_ref, actor, now):
        for value, name in ((owner,'owner'),(evidence_ref,'release evidence'),(actor,'actor')):
            text(value,name)
        if instant(deadline) <= now:
            raise Blocked('warehouse due date must be in the future')
        with transaction(self.db):
            p = self.work(work_key)
            if digest(p) != expected_plan_digest:
                raise Blocked('work plan changed since review')
            existing = self.db.execute('SELECT body FROM fba_releases WHERE work_key=?', (work_key,)).fetchone()
            signature = dict(recipe_id=recipe_id, plan_digest=expected_plan_digest, owner=owner,
                             deadline=deadline,evidence_ref=evidence_ref,reviewer=actor)
            if existing:
                if any(json.loads(existing[0]).get(k) != v for k,v in signature.items()):
                    raise Blocked('release already exists with different terms')
                return self.detail(work_key)
            if self.db.execute('SELECT state FROM fba_work_drafts WHERE request_key=?',(work_key,)).fetchone()[0] != 'DRAFT':
                raise Blocked('only a fresh draft can be released')
            if self.db.execute('SELECT 1 FROM fba_box_scans WHERE work_key=?',(work_key,)).fetchone():
                raise Blocked('legacy captured boxes need reconciliation before release')
            if self.db.execute('SELECT 1 FROM fba_actions WHERE json_extract(proposal,\'$.work_key\')=?',(work_key,)).fetchone():
                raise Blocked('legacy remote action exists; reconcile before releasing')
            checked = plan_replenishment(PackSpec(**p['pack_spec']),p['snapshot'],p['policy'],p['gates'],now)
            checked['planned_at'] = p['planned_at']
            if canonical(checked) != canonical(p):
                raise Blocked('replenishment plan is no longer valid')
            allocations = self.allocations(work_key,recipe_id,now)
            recipe = json.loads(self.db.execute('SELECT body FROM fba_recipes WHERE recipe_id=?',(recipe_id,)).fetchone()[0])
            expiries = [instant(g['valid_until']) for g in p['gates'].values()] + [instant(recipe['valid_until'])]
            for a in allocations:
                r = resource(self.db,a['resource_id'])
                window(r['observed_at'],r['valid_until'],now)
                if r['available'] < a['quantity']:
                    raise Blocked('insufficient shared resource: ' + a['resource_id'])
                expiries.append(instant(r['valid_until']))
            body = {**signature,'released_at':now.isoformat(),'valid_until':min(expiries).isoformat(),'allocations':allocations}
            self.db.execute('INSERT INTO fba_releases VALUES (?,?,?,1)',(work_key,'RELEASED',canonical(body)))
            self.db.executemany("INSERT INTO fba_reservations VALUES (?,?,?,'HELD')",[(work_key,a['resource_id'],a['quantity']) for a in allocations])
            self.db.execute("UPDATE fba_work_drafts SET state='RELEASED' WHERE request_key=?",(work_key,))
            event(self.db,work_key,actor,'RELEASED',body,now)
        return self.detail(work_key)

    def boxes(self, work_key, *, linked=False):
        rows = self.db.execute('SELECT s.body,l.body FROM fba_box_scans s LEFT JOIN fba_box_links l USING(box_id) LEFT JOIN fba_box_voids v USING(box_id) WHERE s.work_key=? AND v.box_id IS NULL ORDER BY s.box_id',(work_key,)).fetchall()
        if linked and any(b is None for _,b in rows):
            raise Blocked('some cartons still lack individual tracking links')
        return [{**json.loads(a),**(json.loads(b) if linked and b else {})} for a,b in rows]

    def _unfrozen(self, work_key):
        if self.db.execute("SELECT 1 FROM fba_actions WHERE json_extract(proposal,'$.work_key')=? AND operation!='createInboundPlan'",(work_key,)).fetchone():
            raise Blocked('packing/shipping proposal exists; physical records frozen pending reconciliation')

    def capture(self, work_key, box, now):
        # Compatibility with the trusted local v2 CLI; the browser stamps its
        # authenticated actor and time through scan(), not through this method.
        validate_box(self.work(work_key), box, now)
        actor = box.get('packed_by')
        return self.scan(work_key, {k:v for k,v in box.items() if k not in ('packed_by','packed_at')}, actor, now)

    def scan(self, work_key, scan, actor, now):
        fields = {'local_box_id','msku','fnsku','pack_version','units','gross_lb','dimensions_in','lot_ref','measurement_evidence_ref'}
        if set(scan) != fields:
            raise Blocked('physical scan contract differs; actor and timestamp are server-supplied')
        with transaction(self.db):
            p = self.work(work_key)
            box = {**scan,'packed_by':text(actor,'actor'),'packed_at':now.isoformat()}
            prior = self.db.execute('SELECT work_key,body FROM fba_box_scans WHERE box_id=?',(scan['local_box_id'],)).fetchone()
            if prior:
                old = json.loads(prior[1])
                if prior[0] != work_key or {k:old[k] for k in fields} != scan or old['packed_by'] != actor or self.db.execute('SELECT 1 FROM fba_box_voids WHERE box_id=?',(scan['local_box_id'],)).fetchone():
                    raise Blocked('barcode already used, voided or recorded with different measurements')
                return {'created':False,'box':old}
            guard_dispatch(self.db,work_key,'createInboundPlan',now,remote=False)
            row = self.db.execute('SELECT state FROM fba_releases WHERE work_key=?',(work_key,)).fetchone()
            if row[0] not in ('RELEASED','PACKING'):
                raise Blocked('sealed packout must be reopened by a supervisor')
            self._unfrozen(work_key)
            validate_box(p,box,now)
            if len(self.boxes(work_key)) >= p['cartons']:
                raise Blocked('all planned cartons are already captured')
            self.db.execute('INSERT INTO fba_box_scans VALUES (?,?,?,?)',(scan['local_box_id'],work_key,digest(box),canonical(box)))
            self.db.execute("UPDATE fba_releases SET state='PACKING',revision=revision+1 WHERE work_key=?",(work_key,))
            event(self.db,work_key,actor,'BOX_CAPTURED',{'box_id':scan['local_box_id'],'evidence_ref':scan['measurement_evidence_ref']},now)
        return {'created':True,'box':box}

    def void_box(self, work_key, box_id, evidence_ref, actor, now):
        with transaction(self.db):
            self._unfrozen(work_key)
            state = self.db.execute('SELECT state FROM fba_releases WHERE work_key=?',(work_key,)).fetchone()
            if not state or state[0] not in ('RELEASED','PACKING','PACKED'):
                raise Blocked('work order cannot be corrected in this state')
            row = self.db.execute('SELECT work_key FROM fba_box_scans WHERE box_id=?',(box_id,)).fetchone()
            if not row or row[0] != work_key:
                raise Blocked('box is not part of this work order')
            if self.db.execute('SELECT 1 FROM fba_box_links WHERE box_id=?',(box_id,)).fetchone():
                raise Blocked('linked carton cannot be voided without shipment reconciliation')
            body = dict(actor=text(actor,'actor'),evidence_ref=text(evidence_ref,'correction evidence'),voided_at=now.isoformat())
            prior = self.db.execute('SELECT body FROM fba_box_voids WHERE box_id=?',(box_id,)).fetchone()
            if not prior:
                self.db.execute('INSERT INTO fba_box_voids VALUES (?,?)',(box_id,canonical(body)))
                self.db.execute("UPDATE fba_releases SET state='PACKING',revision=revision+1 WHERE work_key=?",(work_key,))
                event(self.db,work_key,actor,'BOX_VOIDED',{'box_id':box_id,**body},now)
        return self.detail(work_key)

    def seal(self, work_key, evidence_ref, actor, now):
        with transaction(self.db):
            guard_dispatch(self.db,work_key,'createInboundPlan',now,remote=False)
            validate_packout(self.work(work_key),self.boxes(work_key),now)
            row = self.db.execute('SELECT state FROM fba_releases WHERE work_key=?',(work_key,)).fetchone()
            if row[0] != 'PACKED':
                self.db.execute("UPDATE fba_releases SET state='PACKED',revision=revision+1 WHERE work_key=?",(work_key,))
                event(self.db,work_key,actor,'PACKOUT_SEALED',{'evidence_ref':text(evidence_ref,'packout review evidence')},now)
        return self.detail(work_key)

    def cancel(self, work_key, evidence_ref, actor, now):
        with transaction(self.db):
            self.work(work_key)
            if self.db.execute('SELECT 1 FROM fba_box_scans WHERE work_key=?',(work_key,)).fetchone() or self.db.execute("SELECT 1 FROM fba_actions WHERE json_extract(proposal,'$.work_key')=?",(work_key,)).fetchone():
                raise Blocked('physical work or remote actions exist; do not release commitments blindly')
            state = self.db.execute('SELECT state FROM fba_work_drafts WHERE request_key=?',(work_key,)).fetchone()[0]
            if state == 'CANCELLED':
                return self.detail(work_key)
            if state not in ('DRAFT','RELEASED'):
                raise Blocked('work cannot be cancelled in this state')
            self.db.execute("UPDATE fba_work_drafts SET state='CANCELLED' WHERE request_key=?",(work_key,))
            self.db.execute("UPDATE fba_releases SET state='CANCELLED',revision=revision+1 WHERE work_key=?",(work_key,))
            self.db.execute("UPDATE fba_reservations SET state='RELEASED' WHERE work_key=?",(work_key,))
            event(self.db,work_key,actor,'CANCELLED',{'evidence_ref':text(evidence_ref,'cancellation evidence')},now)
        return self.detail(work_key)

    def link(self, box_id, link, now):
        old = self.db.execute('SELECT body FROM fba_box_links WHERE box_id=?', (box_id,)).fetchone()
        if old:
            previous = json.loads(old[0])
            if {k:v for k,v in previous.items() if k!='linked_at'} == {k:v for k,v in link.items() if k!='linked_at'}:
                return {'created':False}
        return super().link(box_id, link, now)

    def prepare_packing(self, work_key, params, actor, now):
        required = {'action_key','scope','inbound_plan_id','packing_group_id','prep_owner','label_owner','evidence_ref','valid_until'}
        if set(params) != required:
            raise Blocked('packing preparation fields differ from contract')
        with transaction(self.db):
            guard_dispatch(self.db,work_key,'setPackingInformation',now,remote=False)
        payload = packing_payload(self.work(work_key),self.boxes(work_key),{'packingGroupId':params['packing_group_id']},now,
                                  prep_owner=params['prep_owner'],label_owner=params['label_owner'])
        p = dict(scope=params['scope'],operation='setPackingInformation',work_key=work_key,
                 ids={'inboundPlanId':params['inbound_plan_id']},body=payload,
                 evidence_ref=params['evidence_ref'],valid_until=params['valid_until'])
        # Journal.stage reconstructs under its transaction in v3, freezing scans.
        return self.stage(params['action_key'],p,now)

    def detail(self, work_key):
        p = self.work(work_key)
        row = self.db.execute('SELECT state,body,revision FROM fba_releases WHERE work_key=?',(work_key,)).fetchone()
        state = row[0] if row else self.db.execute('SELECT state FROM fba_work_drafts WHERE request_key=?',(work_key,)).fetchone()[0]
        ev = self.db.execute('SELECT recorded_at,actor,kind,body FROM fba_warehouse_events WHERE work_key=? ORDER BY seq',(work_key,)).fetchall()
        return {'work_key':work_key,'plan':p,'plan_digest':digest(p),'state':state,'revision':row[2] if row else 0,
                'release':json.loads(row[1]) if row else None,'boxes':self.boxes(work_key),
                'events':[dict(recorded_at=a,actor=b,kind=c,body=json.loads(d)) for a,b,c,d in ev],
                'box_links':{bid:json.loads(body) for bid,body in self.db.execute('SELECT l.box_id,l.body FROM fba_box_links l JOIN fba_box_scans s USING(box_id) WHERE s.work_key=?',(work_key,))},
                'amazon_receipt_verified':False,'carrier_acceptance_verified':False}

    def queue(self, now):
        out = []
        for key, in self.db.execute('SELECT request_key FROM fba_work_drafts ORDER BY request_key'):
            d = self.detail(key)
            rel = d['release'] or {}
            out.append(dict(work_key=key,msku=d['plan']['msku'],state=d['state'],units=d['plan']['planned_units'],
                            cartons=d['plan']['cartons'],captured=len(d['boxes']),owner=rel.get('owner'),deadline=rel.get('deadline'),
                            overdue=bool(rel.get('deadline') and instant(rel['deadline'])<now and d['state']!='CANCELLED')))
        resources = [resource(self.db,k) for k, in self.db.execute('SELECT resource_id FROM fba_resources ORDER BY resource_id')]
        for r in resources:
            r['expired'] = not instant(r['observed_at']) <= now < instant(r['valid_until'])
        return {'work':out,'resources':resources,'exceptions':self.exceptions(),
                'remote_dispatch_exposed':False,'automatic_receiving_monitoring':False}
