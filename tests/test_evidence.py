import unittest
import csv,json,tempfile
from pathlib import Path
from diy_growth.db import connect
from diy_growth.evidence import import_evidence
from diy_growth.evidence import number,cost_models,contribution,holdout_replay,validate_history

class EvidenceTests(unittest.TestCase):
    def fixture(self,path,asin='A'):
        def write_csv(name,rows):
            with (path/name).open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        write_csv('economics.csv',[dict(sku='S',asin='A',expanded_model_unit_cost='20')])
        write_csv('merchant.csv',[dict(woocommerce_sku='S',merchant_item_id='123')])
        (path/'woo.json').write_text(json.dumps({'results':[{'rows':[{'sku':'S','id':123}]}]}))
        (path/'history.json').write_text(json.dumps([{'asin':'A','monthly':[{'month':'07','offers':[{'sku':'S','child_asin':asin}]}]}]))
        return {'year':2026,'sources':{k:str(path/v) for k,v in dict(economics='economics.csv',merchant='merchant.csv',woo='woo.json',history='history.json').items()}}

    def test_repeat_import_and_preserve_existing_mapping(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            p=Path(d);m=self.fixture(p);c=connect(p/'db')
            c.execute("INSERT INTO canonical_products(sku,asin,mapping_status) VALUES ('S','OWNER-ASIN','CURRENT')");c.commit()
            fingerprint=import_evidence(c,m)
            counts=c.execute('SELECT COUNT(*) FROM sourced_values').fetchone()[0]
            self.assertEqual(import_evidence(c,m),fingerprint)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sourced_values').fetchone()[0],counts)
            row=c.execute("SELECT * FROM canonical_products WHERE sku='S'").fetchone()
            self.assertEqual(row['asin'],'OWNER-ASIN')
            self.assertEqual(row['mapping_status'],'CONFLICTING')
            c.close()

    def test_conflicting_history_refused_before_import(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            p=Path(d);c=connect(p/'db')
            with self.assertRaises(ValueError):import_evidence(c,self.fixture(p,'DIFFERENT'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM canonical_products').fetchone()[0],0)
            c.close()

    def test_missing_spend_and_cost_are_not_zero(self):
        self.assertIsNone(contribution(100,2,20,None,.12))
        self.assertIsNone(contribution(100,2,None,10,.12))
        self.assertEqual(contribution(100,2,20,0,.12),48)

    def test_different_asins_cannot_borrow_cost(self):
        models=cost_models([{'sku':'S','asin':'A','expanded_model_unit_cost':'20'},
                           {'sku':'S','asin':'B','expanded_model_unit_cost':''}])
        self.assertEqual(models[('S','A')]['value'],20)
        self.assertIsNone(models[('S','B')]['value'])

    def test_conflicting_models_are_not_averaged(self):
        models=cost_models([{'sku':'S','asin':'A','expanded_model_unit_cost':str(v)} for v in [20,30]])
        self.assertEqual(models[('S','A')]['status'],'CONFLICTING')
        self.assertIsNone(models[('S','A')]['value'])

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):number('NaN')
        self.assertIsNone(number(''))

    def test_exact_sp_recovery_and_current_cost_separation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            p=Path(d);m=self.fixture(p);c=connect(p/'db')
            with (p/'economics.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['sku','asin','expanded_model_unit_cost','july_august_available_ads_economics'])
                w.writeheader();w.writerow(dict(sku='S',asin='A',expanded_model_unit_cost='',july_august_available_ads_economics=json.dumps({'amazon':{'07':dict(sales=100,units=2,sp_spend=None),'08':dict(sales=120,units=2,sp_spend=None)}})))
            for month in ('07','08'):
                name='sp_'+month
                (p/(name+'.json')).write_text(json.dumps({'data':{'status':'completed','data':[['SKU','ASIN','Spend'],['S','A',0],['S','OTHER',999]]}}))
                m['sources'][name]=str(p/(name+'.json'))
            (p/'current.json').write_text(json.dumps({'results':[dict(sku='S',asin='A',source_backed_unit_cost_mean_shipping=20,sources=['later cost'])]}))
            m['sources']['current_costs']=str(p/'current.json')
            h=import_evidence(c,m);v=validate_history(c,h)
            self.assertEqual(v['holdout_replay']['unknown'],1)
            self.assertEqual(v['scenarios'][0]['sponsored_products_spend'],0)
            self.assertEqual(v['current_cost_sensitivities'][0]['contribution_scenarios']['0.12'],48)
            self.assertIsNone(v['scenarios'][0]['contribution_scenarios']['0.12'])
            c.close()

    def test_source_values_deduplicated_across_manifest_versions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            p=Path(d);m=self.fixture(p);c=connect(p/'db')
            import_evidence(c,m)
            (p/'woo.json').write_text(json.dumps({'results':[{'rows':[{'sku':'S','id':123},{'sku':'T','id':456}]}]}))
            import_evidence(c,m)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sourced_values').fetchone()[0],1)
            c.close()

    def test_holdout_does_not_select_using_future_result(self):
        def row(month,value):
            return dict(sku='S',asin='A',month=month,contribution_scenarios={'0.12':value,'0.15':value})
        result=holdout_replay([row('07',10),row('08',-20)])
        self.assertEqual(result['rows'][0]['training_classification'],'POSITIVE_BOTH_FEES')
        self.assertEqual(result['false_positives'],1)
        self.assertFalse(result['rows'][0]['autonomous_release_qualified'])
        self.assertEqual(holdout_replay([row('07',10),row('08',None)])['evaluable'],0)
