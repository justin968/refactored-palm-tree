from copy import deepcopy
from test_fba import NOW,PAST,FUTURE,plan
from diy_growth.fba import digest


def provision(w,key='work-1',now=NOW,stock=120,cash=36000,cartons=20,capacity=240):
    p=w.work(key)
    shared=[('shared-stock','stock','sellable_unit',stock,1,0),('shared-cash','cash','USD_cent',cash,300,0),('shared-cartons','carton','each',cartons,0,1),('shared-capacity','capacity','minute',capacity,2,0)]
    for rid,kind,unit,count,pu,pc in shared:
        if not w.db.execute('SELECT 1 FROM fba_resources WHERE resource_id=?',(rid,)).fetchone():
            w.count(rid,dict(resource_id=rid,kind=kind,unit=unit,balance_including_local_holds=count,expected_revision=0,observed_at=PAST,valid_until=FUTURE,evidence_ref='synthetic reconciled inclusive count',includes_local_holds=True),'test-supervisor',now)
    recipe=dict(recipe_id='recipe-'+key,marketplace_id=p['marketplace_id'],msku=p['msku'],pack_version=p['pack_spec']['version'],observed_at=PAST,valid_until=FUTURE,evidence_ref='synthetic measured recipe',lines=[dict(resource_id=rid,unit=unit,per_unit=pu,per_carton=pc,fixed=0) for rid,kind,unit,count,pu,pc in shared])
    w.add_recipe(recipe,key,'test-supervisor',now)
    return recipe


def release(w,key='work-1',now=NOW,**limits):
    recipe=provision(w,key,now,**limits)
    return w.release(key,recipe['recipe_id'],digest(w.work(key)),'test-packer',FUTURE,'synthetic release evidence','test-supervisor',now)


def scan(i=0,sku='TEST-FBA'):
    return dict(local_box_id=f'BOX-{i}',msku=sku,fnsku='TEST-FNSKU',pack_version='v1',units=12,gross_lb=27,dimensions_in=[12,9,11],lot_ref='TEST-LOT',measurement_evidence_ref='TEST-scale-record')


def other_plan():
    from dataclasses import replace
    from test_fba import inputs
    from diy_growth.fba import plan_replenishment
    spec,snap,policy,gates=inputs();spec=replace(spec,msku='TEST-ALT');snap['msku']=spec.msku
    for g in gates.values():g['msku']=spec.msku
    return plan_replenishment(spec,snap,policy,gates,NOW)
