"""Synthetic, remote-locked demo. Never load real work into this database."""
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import argparse
import json
from pathlib import Path
import secrets

from .fba import PackSpec, canonical
from .fba_ops import secure_file
from .fba_station import password_hash
from .fba_warehouse import Warehouse


def seed(path, now):
    w=Warehouse(str(path))
    if w.db.execute('SELECT 1 FROM fba_work_drafts LIMIT 1').fetchone():
        w.close();raise ValueError('demo requires an empty database')
    w.db.execute("INSERT OR REPLACE INTO fba_warehouse_settings VALUES ('mode','DEMO')")
    before=(now-timedelta(minutes=10)).isoformat();expiry=(now+timedelta(hours=8)).isoformat()
    for key,kind,unit,balance in [('demo-stock','stock','sellable_unit',60),('demo-cartons','carton','each',8),('demo-cash','cash','USD_cent',20000),('demo-capacity','capacity','minute',300)]:
        data=dict(resource_id=key,kind=kind,unit=unit,balance_including_local_holds=balance,expected_revision=0,observed_at=before,valid_until=expiry,evidence_ref='SYNTHETIC demonstration count',includes_local_holds=True)
        w.count(key,data,'demo-supervisor',now)
    for i in (1,2):
        spec=PackSpec('DEMO-MARKET',f'DEMO-SKU-{i}','DEMO-ASIN','DEMO-FNSKU','v1',[3,3,5],2.1,[12,9,11],27,12,before,'DEMO','SYNTHETIC measured package')
        snap=dict(msku=spec.msku,marketplace_id=spec.marketplace_id,evidence_ref='SYNTHETIC positions',observed_at=before,positions_reconciled=True,daily_units=2,fba_fulfillable=20,on_time_inbound=12,open_local_work=0,warehouse_unallocated=120,capacity_remaining_after_commitments=120,cash_cost_per_unit='3.00',cash_remaining='360.00')
        policy=dict(max_snapshot_age_hours=4,lead_days=14,review_days=7,safety_days=7,max_order_units=120,weight_tolerance_lb='0.5',dimension_tolerance_in='0.25')
        gates={name:dict(approved=True,msku=spec.msku,asin=spec.asin,marketplace_id=spec.marketplace_id,pack_version=spec.version,evidence_ref='SYNTHETIC demo only',observed_at=before,valid_until=expiry) for name in ('identity','fba_offer','inbound_eligibility','dangerous_goods','packout','replenishment_policy')}
        work=f'DEMO-WORK-{i}';w.import_request(dict(request_key=work,pack_spec=asdict(spec),snapshot=snap,policy=policy,gates=gates),now)
        lines=[dict(resource_id=key,unit=unit,per_unit=pu,per_carton=pc,fixed=0) for key,unit,pu,pc in [('demo-stock','sellable_unit',1,0),('demo-cartons','each',0,1),('demo-cash','USD_cent',300,0),('demo-capacity','minute',2,0)]]
        recipe=dict(recipe_id=f'DEMO-RECIPE-{i}',marketplace_id=spec.marketplace_id,msku=spec.msku,pack_version=spec.version,observed_at=before,valid_until=expiry,evidence_ref='SYNTHETIC resource recipe',lines=lines)
        w.add_recipe(recipe,work,'demo-supervisor',now)
    w.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dir',type=Path,required=True);a=p.parse_args()
    a.dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    db=a.dir/'demo.sqlite3';users=a.dir/'users.json'
    if db.exists() or users.exists():raise SystemExit('Refusing to overwrite existing state.')
    secure_file(db,exclusive=True);seed(db,datetime.now(timezone.utc))
    pw=secrets.token_urlsafe(18)
    records={name:{'role':role,'password_hash':password_hash(pw)} for name,role in [('demo-supervisor','supervisor'),('demo-packer','packer')]}
    secure_file(users,exclusive=True);users.write_text(canonical(records),encoding='utf-8')
    print('Synthetic, remote-locked demo created. No real shipments or accounts.')
    print('Users: demo-supervisor / demo-packer')
    print('One-time demo password:',pw)
    print(f'Run: python -m diy_growth.fba_station --db "{db}" serve --users "{users}"')


if __name__=='__main__':main()
