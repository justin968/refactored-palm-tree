from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
import pytest
from test_fba import NOW,FUTURE,PAST,plan
from fba_v3_helpers import provision,release,scan,other_plan
from diy_growth.fba import Blocked,digest
from diy_growth.fba_warehouse import Warehouse
from diy_growth.fba_resources import resource,guard_dispatch,transaction
from diy_growth.fba_execution import Journal,OPERATIONS


@pytest.fixture
def w(tmp_path):
    db=Warehouse(str(tmp_path/'station.sqlite3'));db.save('work-1',plan(),NOW)
    yield db
    db.close()


def count(w,key,balance,event='short',**changes):
    r=resource(w.db,key)
    data=dict(resource_id=key,kind=r['kind'],unit=r['unit'],balance_including_local_holds=balance,expected_revision=r['revision'],observed_at=NOW.isoformat(),valid_until=FUTURE,evidence_ref='synthetic recount',includes_local_holds=True)
    data.update(changes)
    return w.count(event,data,'test-supervisor',NOW)


def test_release_reserves_exact_resources_and_is_idempotent(w):
    d=release(w);assert d['state']=='RELEASED'
    r=resource(w.db,'shared-stock');assert (r['balance'],r['held'],r['available'])==(120,24,96)
    assert resource(w.db,'shared-cash')['held']==7200
    assert release(w)==d
    assert w.db.execute("SELECT COUNT(*) FROM fba_warehouse_events WHERE kind='RELEASED'").fetchone()[0]==1


def test_two_skus_share_stock_and_no_partial_reservation(w):
    release(w,stock=36);w.save('work-2',other_plan(),NOW)
    with pytest.raises(Blocked,match='insufficient'):release(w,'work-2')
    assert resource(w.db,'shared-stock')['held']==24
    assert w.db.execute("SELECT COUNT(*) FROM fba_reservations WHERE work_key='work-2'").fetchone()[0]==0
    assert w.detail('work-2')['state']=='DRAFT'


@pytest.mark.parametrize('limit',[{'stock':23},{'cash':7199},{'cartons':1},{'capacity':47}])
def test_each_shared_resource_can_block_without_side_effects(w,limit):
    with pytest.raises(Blocked):release(w,**limit)
    assert w.db.execute('SELECT COUNT(*) FROM fba_reservations').fetchone()[0]==0
    assert w.detail('work-1')['state']=='DRAFT'


def test_concurrent_workers_reserve_only_once(w):
    provision(w,stock=36);w.save('work-2',other_plan(),NOW);provision(w,'work-2')
    path=w.db.execute('PRAGMA database_list').fetchone()[2]
    def run(key):
        other=Warehouse(path)
        try:
            return release(other,key)['state']
        except Blocked:return 'BLOCKED'
        finally:other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['work-1','work-2']))
    assert sorted(results)==['BLOCKED','RELEASED']
    assert resource(w.db,'shared-stock')['held']==24


def test_active_release_blocks_second_same_sku_draft(w):
    release(w)
    with pytest.raises(Blocked):w.save('new-work',plan(),NOW)


def test_cancel_untouched_returns_holds_not_extra_stock(w):
    release(w)
    w.cancel('work-1','synthetic cancellation','supervisor',NOW)
    assert resource(w.db,'shared-stock')['available']==120
    w.cancel('work-1','synthetic cancellation','supervisor',NOW)
    assert resource(w.db,'shared-stock')['balance']==120
    w.save('replacement',plan(),NOW)


def test_cancel_draft_is_terminal_and_cannot_resurrect(w):
    provision(w);w.cancel('work-1','cancel','supervisor',NOW)
    with pytest.raises(Blocked):release(w)


def test_short_real_count_is_recorded_and_blocks_dispatch(w):
    release(w);r=count(w,'shared-stock',10)
    assert r['balance']==10 and r['available']==-14
    with pytest.raises(Blocked,match='shortage'):guard_dispatch(w.db,'work-1','createInboundPlan',NOW)
    count(w,'shared-stock',30,event='recount')
    guard_dispatch(w.db,'work-1','createInboundPlan',NOW)


@pytest.mark.parametrize('change',[{'includes_local_holds':False},{'expected_revision':99},{'unit':'lb'},{'balance_including_local_holds':True},{'observed_at':FUTURE},{'valid_until':PAST}])
def test_count_conflicts_and_units_rejected(w,change):
    provision(w)
    with pytest.raises(Blocked):count(w,'shared-stock',120,**change)


def test_count_idempotency_does_not_reapply_after_consumption(w):
    provision(w);a=count(w,'shared-stock',100)
    count(w,'shared-stock',90,event='second')
    # Replay exact original event must return current 90, not reset to 100.
    r=w.count('short',dict(resource_id='shared-stock',kind='stock',unit='sellable_unit',balance_including_local_holds=100,expected_revision=1,observed_at=NOW.isoformat(),valid_until=FUTURE,evidence_ref='synthetic recount',includes_local_holds=True),'test-supervisor',NOW)
    assert r['balance']==90


def test_expired_resource_blocks_dispatch_without_releasing_hold(w):
    release(w)
    with pytest.raises(Blocked):guard_dispatch(w.db,'work-1','createInboundPlan',NOW+timedelta(days=2))
    assert resource(w.db,'shared-stock')['held']==24


def test_recipe_immutable_and_version_bound(w):
    recipe=provision(w);recipe['lines'][0]['per_unit']=2
    with pytest.raises(Blocked):w.add_recipe(recipe,'work-1','supervisor',NOW)
    recipe=provision(w);recipe['pack_version']='v2'
    with pytest.raises(Blocked):w.add_recipe(recipe,'work-1','supervisor',NOW)


def test_recipe_cannot_omit_budget_or_mix_units(w):
    recipe=provision(w);recipe['recipe_id']='bad';recipe['lines'][1]['per_unit']=1
    with pytest.raises(Blocked):w.add_recipe(recipe,'work-1','supervisor',NOW)
    recipe=provision(w);recipe['recipe_id']='bad';recipe['lines'][2]['unit']='lb'
    with pytest.raises(Blocked):w.add_recipe(recipe,'work-1','supervisor',NOW)


def test_physical_capture_requires_release(w):
    with pytest.raises(Blocked):w.scan('work-1',scan(),'packer',NOW)


def test_authenticated_actor_and_timestamp_are_not_client_fields(w):
    release(w)
    with pytest.raises(Blocked):w.scan('work-1',{**scan(),'packed_by':'someone-else'},'packer',NOW)
    out=w.scan('work-1',scan(),'packer',NOW)
    assert out['box']['packed_by']=='packer' and out['box']['packed_at']==NOW.isoformat()
    assert not w.scan('work-1',scan(),'packer',NOW+timedelta(seconds=5))['created']


def test_void_keeps_original_and_replacement_uses_new_barcode(w):
    release(w);w.scan('work-1',scan(),'packer',NOW)
    w.void_box('work-1','BOX-0','reweighed; original entered incorrectly','supervisor',NOW)
    assert w.boxes('work-1')==[]
    assert w.db.execute('SELECT COUNT(*) FROM fba_box_scans').fetchone()[0]==1
    with pytest.raises(Blocked):w.scan('work-1',scan(),'packer',NOW)
    w.scan('work-1',scan(9),'packer',NOW)
    assert [b['local_box_id'] for b in w.boxes('work-1')]==['BOX-9']
    with pytest.raises(Blocked):w.cancel('work-1','cancel','supervisor',NOW)


def test_seal_requires_complete_packout_and_is_not_shipped(w):
    release(w);w.scan('work-1',scan(),'packer',NOW)
    with pytest.raises(Blocked):w.seal('work-1','qa','supervisor',NOW)
    w.scan('work-1',scan(1),'packer',NOW);d=w.seal('work-1','qa','supervisor',NOW)
    assert d['state']=='PACKED' and not d['carrier_acceptance_verified'] and not d['amazon_receipt_verified']
    with pytest.raises(Blocked):w.scan('work-1',scan(3),'packer',NOW)
    assert resource(w.db,'shared-stock')['held']==24


def test_linked_carton_cannot_be_voided(w):
    release(w);w.scan('work-1',scan(),'packer',NOW)
    w.link('BOX-0',dict(amazon_box_id='A1',carrier='TEST',tracking_number='T1',evidence_ref='scan',linked_by='packer',linked_at=NOW.isoformat()),NOW)
    with pytest.raises(Blocked):w.void_box('work-1','BOX-0','change','supervisor',NOW)


def test_demo_allows_physical_work_but_never_remote_execution(w):
    release(w);w.db.execute("INSERT INTO fba_warehouse_settings VALUES ('mode','DEMO')")
    w.scan('work-1',scan(),'packer',NOW)
    with pytest.raises(Blocked,match='demo'):guard_dispatch(w.db,'work-1','createInboundPlan',NOW)


def test_real_execution_without_release_is_blocked(tmp_path):
    from test_fba_execution import approved,MutationClient
    j=Journal(str(tmp_path/'legacy.sqlite3'));j.save('work-1',plan(),NOW);approved(j);c=MutationClient()
    try:
        with pytest.raises(Blocked,match='warehouse release'):j.execute('action-1',c,enabled=True,allowed_operations=OPERATIONS,now=NOW)
        assert not c.calls
    finally:j.close()


def test_packing_proposal_freezes_scan_corrections(w):
    from test_fba_execution import PID,GID,SCOPE
    release(w);w.scan('work-1',scan(),'packer',NOW);w.scan('work-1',scan(1),'packer',NOW);w.seal('work-1','qa','supervisor',NOW)
    params=dict(action_key='pack',scope=SCOPE,inbound_plan_id=PID,packing_group_id=GID,prep_owner='SELLER',label_owner='SELLER',evidence_ref='exact assignment',valid_until=FUTURE)
    staged=w.prepare_packing('work-1',params,'supervisor',NOW)
    assert staged['state']=='STAGED'
    assert len(staged['proposal']['body']['packageGroupings'][0]['boxes'])==2
    with pytest.raises(Blocked):w.void_box('work-1','BOX-0','correct','supervisor',NOW)


def test_forged_release_digest_rejected(w):
    provision(w)
    with pytest.raises(Blocked):w.release('work-1','recipe-work-1','wrong','packer',FUTURE,'evidence','supervisor',NOW)


def test_sqlite_backup_includes_wal_state(w,tmp_path):
    release(w);dest=sqlite3.connect(tmp_path/'backup.sqlite3')
    w.db.backup(dest);assert dest.execute('SELECT COUNT(*) FROM fba_reservations').fetchone()[0]==4;dest.close()


def test_duplicate_link_from_browser_preserves_original_timestamp(w):
    release(w);w.scan('work-1',scan(),'packer',NOW)
    data=dict(amazon_box_id='A1',carrier='TEST',tracking_number='T1',evidence_ref='scan',linked_by='packer',linked_at=NOW.isoformat())
    assert w.link('BOX-0',data,NOW)['created']
    assert not w.link('BOX-0',{**data,'linked_at':(NOW+timedelta(seconds=5)).isoformat()},NOW+timedelta(seconds=5))['created']
    assert w.detail('work-1')['box_links']['BOX-0']['linked_at']==data['linked_at']
