"""Import existing owner evidence without live reads, inferred aliases or mutations."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from collections import defaultdict
from .db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_imports(hash TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_records(import_hash TEXT, kind TEXT, record_key TEXT,
 source TEXT, source_row INTEGER, data_json TEXT, PRIMARY KEY(import_hash,kind,record_key));
"""

def number(value):
    if value in (None, ""):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite evidence value")
    return result

def cost_models(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['sku'], row['asin'])].append(row)
    models = {}
    for key, candidates in grouped.items():
        values = {number(r.get('expanded_model_unit_cost')) for r in candidates}
        nonmissing = values - {None}
        models[key] = dict(value=next(iter(nonmissing)) if len(nonmissing) == 1 else None,
                           status='CONFLICTING' if len(nonmissing) > 1 else 'ESTIMATED' if nonmissing else 'MISSING',
                           verified=False, rows=len(candidates))
    return models

def contribution(sales, units, cost, spend, fee):
    # Missing cost/spend is unknown, never zero. This is a partial scenario only.
    if any(x is None for x in (sales, units, cost, spend)):
        return None
    return round(sales * (1-fee) - units * cost - spend, 4)

def holdout_replay(scenarios):
    """July-only classification evaluated on August; never causal action uplift."""
    indexed={(s['sku'],s['asin'],s['month']):s for s in scenarios}
    rows=[]
    for sku,asin in sorted({(s['sku'],s['asin']) for s in scenarios}):
        before=indexed.get((sku,asin,'07'));after=indexed.get((sku,asin,'08'))
        if not before or not after:
            continue
        b=before['contribution_scenarios'];a=after['contribution_scenarios']
        complete=all(v is not None for v in list(b.values())+list(a.values()))
        classification='UNKNOWN' if not complete else 'POSITIVE_BOTH_FEES' if min(b.values())>0 else 'NONPOSITIVE_OR_FEE_SENSITIVE'
        rows.append(dict(sku=sku,asin=asin,training_period='2026-07',holdout_period='2026-08',
                         training_classification=classification,
                         holdout_positive_both_fees=None if not complete else min(a.values())>0,
                         false_positive=None if not complete or classification!='POSITIVE_BOTH_FEES' else min(a.values())<=0,
                         autonomous_release_qualified=False))
    return dict(rows=rows,evaluable=sum(r['training_classification']!='UNKNOWN' for r in rows),
                unknown=sum(r['training_classification']=='UNKNOWN' for r in rows),
                false_positives=sum(r['false_positive'] is True for r in rows),
                scope='Retrospective cost-scenario sign stability, not counterfactual optimizer/action validation')

def import_evidence(conn, manifest):
    conn.executescript(SCHEMA)
    sources = {}
    for name, filename in manifest['sources'].items():
        path = Path(filename)
        raw = path.read_bytes()
        sources[name] = dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest())
    receipt = dict(schema_version=4, year=manifest['year'], sources=sources)
    fingerprint = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    if conn.execute('SELECT 1 FROM evidence_imports WHERE hash=?', (fingerprint,)).fetchone():
        return fingerprint
    def read_csv(name):
        with open(sources[name]['path'], encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))
    economics = read_csv('economics')
    seed = read_csv('merchant')
    website_costs = read_csv('website_costs') if 'website_costs' in sources else []
    history = json.loads(Path(sources['history']['path']).read_text(encoding='utf-8-sig'))
    woo = json.loads(Path(sources['woo']['path']).read_text(encoding='utf-8-sig'))
    mappings = defaultdict(lambda: defaultdict(set))
    def add(sku, field, value):
        if sku and value not in (None, ''):
            mappings[sku][field].add(str(value))
    for row in economics:
        add(row['sku'], 'asin', row['asin'])
        add(row['sku'], 'amazon_seller_sku', row['sku'])
    for group in history:
        for month in group['monthly']:
            for offer in month['offers']:
                if offer['child_asin'] != group['asin']:
                    raise ValueError('history ASIN disagreement')
                add(offer['sku'], 'asin', offer['child_asin'])
                add(offer['sku'], 'amazon_seller_sku', offer['sku'])
    for group in woo['results']:
        for row in group.get('rows', []):
            add(row.get('sku'), 'woo_variation_id', row.get('id'))
    for row in seed:
        add(row['woocommerce_sku'], 'merchant_offer_id', row['merchant_item_id'])
    for row in website_costs:
        if row.get('catalog_matched','').lower() in ('true','yes','1'):
            add(row.get('woo_sku'),'woo_variation_id',row.get('woocommerce_item_id'))
    records = []
    for sku, fields in mappings.items():
        data = {k: sorted(v) for k,v in fields.items()}
        data['status'] = 'AMBIGUOUS' if any(len(v)>1 for v in fields.values()) else 'SOURCE_EXACT'
        records.append(('mapping', sku, 'multiple exact source records', 0, data))
    for (sku, asin), model in cost_models(economics).items():
        records.append(('cost', sku+'|'+asin, sources['economics']['path'], 0, model))
    for i, row in enumerate(economics, 2):
        records.append(('economics_source', str(i), sources['economics']['path'], i, row))
    target_pairs = {(r['sku'], r['asin']) for r in economics}
    if 'matched_economics' in sources:
        for i, row in enumerate(read_csv('matched_economics'), 2):
            if (row['sku'], row['asin']) in target_pairs and row.get('july_august_matched_ads_economics'):
                records.append(('matched_economics_source', str(i), sources['matched_economics']['path'], i, row))
    if 'current_costs' in sources:
        current = json.loads(Path(sources['current_costs']['path']).read_text(encoding='utf-8-sig'))
        for i, row in enumerate(current['results']):
            if (row['sku'], row['asin']) in target_pairs:
                records.append(('current_cost_scenario', row['sku']+'|'+row['asin'], sources['current_costs']['path'], i, row))
    for month in ('07','08'):
        seller_name = 'recovered_seller_'+month
        if seller_name in sources:
            seller = json.loads(Path(sources[seller_name]['path']).read_text(encoding='utf-8-sig'))['data']
            if seller['status'] != 'completed':
                raise ValueError('incomplete recovered seller report')
            fields = seller.get('canonical_field_ids') or seller['requested_field_ids']
            for i, values in enumerate(seller['data'][1:],2):
                row = dict(zip(fields,values))
                if (row['sku'],row['child_asin']) in target_pairs:
                    records.append(('recovered_seller',row['sku']+'|'+row['child_asin']+'|'+month,sources[seller_name]['path'],i,row))
        name = 'sp_'+month
        if name not in sources:
            continue
        report = json.loads(Path(sources[name]['path']).read_text(encoding='utf-8-sig'))['data']
        if report['status'] != 'completed':
            raise ValueError('incomplete SP report')
        grouped = defaultdict(list)
        for i, row in enumerate(report['data'][1:],2):
            if (row[0],row[1]) in target_pairs:
                grouped[(row[0],row[1])].append(dict(source_row=i,spend=number(row[2])))
        for (sku,asin), rows in grouped.items():
            records.append(('exact_sp_spend',sku+'|'+asin+'|'+month,sources[name]['path'],0,
                dict(spend=sum(r['spend'] for r in rows) if all(r['spend'] is not None for r in rows) else None,rows=rows)))
    for i,row in enumerate(website_costs,2):
        records.append(('website_cost_source',str(i),sources['website_costs']['path'],i,row))
    for i, group in enumerate(history):
        for month in group['monthly']:
            data = dict(month, asin=group['asin'], period=f"{manifest['year']}-{month['month']}",
                        basis='seller reported sales; not refunds-adjusted profit; sessions counted once per ASIN')
            records.append(('seller_history', f"{group['asin']}|{data['period']}", sources['history']['path'], i, data))
    with conn:
        conn.execute('INSERT INTO evidence_imports VALUES (?,?)', (fingerprint,json.dumps(receipt)))
        conn.executemany('INSERT INTO evidence_records VALUES (?,?,?,?,?,?)',
                         [(fingerprint,k,key,src,i,json.dumps(data,sort_keys=True)) for k,key,src,i,data in records])
        for sku, fields in mappings.items():
            prior=conn.execute('SELECT * FROM canonical_products WHERE sku=?',(sku,)).fetchone()
            conflicts=any(len(values)>1 for values in fields.values())
            if prior:
                conflicts = conflicts or any(prior[field] and prior[field] not in values for field,values in fields.items())
                if conflicts:
                    conn.execute("UPDATE canonical_products SET mapping_status='CONFLICTING' WHERE sku=?",(sku,))
                # Existing owner mappings are never replaced by an older export.
                continue
            values={field:next(iter(items)) if len(items)==1 else None for field,items in fields.items()}
            conn.execute('INSERT INTO canonical_products(sku,woo_variation_id,merchant_offer_id,amazon_seller_sku,asin,mapping_status) VALUES (?,?,?,?,?,?)',
                         (sku,values.get('woo_variation_id'),values.get('merchant_offer_id'),values.get('amazon_seller_sku'),values.get('asin'),
                          'AMBIGUOUS' if conflicts else 'STALE'))
        for (sku,asin),model in cost_models(economics).items():
            conn.execute('INSERT INTO sourced_values(entity_type,entity_id,metric,value,source,observed_at,confidence,status,estimated) SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM sourced_values WHERE entity_type=? AND entity_id=? AND metric=? AND source=?)',
                         ('amazon_sku_asin',sku+'|'+asin,'modeled_raw_pack_shipping_combined',model['value'],
                          sources['economics']['path']+'#sha256='+sources['economics']['sha256'],
                          'UNKNOWN_HISTORICAL_MODEL_DATE',0.5 if model['value'] is not None else 0,model['status'],1,
                          'amazon_sku_asin',sku+'|'+asin,'modeled_raw_pack_shipping_combined',sources['economics']['path']+'#sha256='+sources['economics']['sha256']))
        for i,row in enumerate(website_costs,2):
            for field in ('estimated_landed_or_raw_material_cost_usd','modeled_packaging_cost_usd',
                          'modeled_shipping_cost_usd','modeled_fixed_processor_cost_per_unit_usd','payment_fee_rate'):
                value=number(row.get(field))
                conn.execute('INSERT INTO sourced_values(entity_type,entity_id,metric,value,source,observed_at,confidence,status,estimated) SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM sourced_values WHERE entity_type=? AND entity_id=? AND metric=? AND source=?)',
                    ('woo_item',row['woocommerce_item_id'],field,value,
                     sources['website_costs']['path']+'#sha256='+sources['website_costs']['sha256']+f':row={i}',
                     row['as_of_date'],.5 if value is not None else 0,'ESTIMATED' if value is not None else 'MISSING',1,
                     'woo_item',row['woocommerce_item_id'],field,sources['website_costs']['path']+'#sha256='+sources['website_costs']['sha256']+f':row={i}'))
    return fingerprint

def validate_history(conn, fingerprint):
    def records(kind):
        return [(r['record_key'],json.loads(r['data_json'])) for r in conn.execute(
            'SELECT * FROM evidence_records WHERE import_hash=? AND kind=? ORDER BY record_key',(fingerprint,kind))]
    models = dict(records('cost'))
    exact_sp = dict(records('exact_sp_spend'))
    recovered_seller = dict(records('recovered_seller'))
    mapped = dict(records('mapping'))
    scenarios = {}
    conflicts = []
    matched = records('matched_economics_source')
    selected_pairs = {(r['sku'],r['asin']) for _,r in matched}
    selected = [(k,r) for k,r in records('economics_source') if (r['sku'],r['asin']) not in selected_pairs] + matched
    for _, row in selected:
        encoded = row.get('july_august_matched_ads_economics') or row.get('july_august_available_ads_economics')
        if not encoded:
            continue
        data = json.loads(encoded)
        sku, asin = row['sku'], row['asin']
        if data.get('sku',sku) != sku:
            raise ValueError('matched economics SKU disagreement')
        model = models[sku+'|'+asin]
        incumbent = conn.execute('SELECT mapping_status FROM canonical_products WHERE sku=?',(sku,)).fetchone()
        mapping_status = 'CONFLICTING' if incumbent and incumbent['mapping_status']=='CONFLICTING' else mapped[sku]['status']
        for month, metrics in data.get('amazon',{}).items():
            key = sku+'|'+asin+'|'+month
            scenario = dict(sku=sku,asin=asin,month=month,
                seller_sales=number(metrics.get('sales')), seller_units=number(metrics.get('units')),
                sponsored_products_spend=number(metrics.get('sp_spend')), cost=model,
                mapping_status=mapping_status, contribution_scenarios={},
                metrics_source='matched_economics' if row.get('july_august_matched_ads_economics') else 'economics',
                measured_profit=None, causal_effect=None)
            if key in exact_sp:
                reported = exact_sp[key]['spend']
                joined = scenario['sponsored_products_spend']
                if joined is not None and reported is not None and abs(joined-reported) > .005:
                    conflicts.append(key)
                scenario['sponsored_products_spend'] = reported
                scenario['spend_provenance'] = exact_sp[key]
            if key in recovered_seller:
                verified = recovered_seller[key]
                for target,source in [('seller_sales','ordered_product_sales'),('seller_units','units_ordered')]:
                    value = number(verified[source])
                    prior = scenario[target]
                    if prior is not None and value is not None and abs(prior-value)>.005:
                        conflicts.append(key)
                    scenario[target] = value
                scenario['seller_provenance'] = verified
            for fee in (.12,.15):
                scenario['contribution_scenarios'][str(fee)] = contribution(
                    scenario['seller_sales'],scenario['seller_units'],model['value'],
                    scenario['sponsored_products_spend'],fee) if model['status']=='ESTIMATED' and scenario['mapping_status']=='SOURCE_EXACT' else None
            if key in scenarios and scenarios[key] != scenario:
                conflicts.append(key)
            scenarios[key] = scenario
    for key in conflicts:
        scenarios[key]['contribution_scenarios'] = {'0.12':None,'0.15':None}
        scenarios[key]['validation_status']='CONFLICTING'
    comparisons=[]
    for sku,asin in sorted({(s['sku'],s['asin']) for s in scenarios.values()}):
        before=scenarios.get(sku+'|'+asin+'|07');after=scenarios.get(sku+'|'+asin+'|08')
        if not before or not after:
            continue
        deltas={fee: None if before['contribution_scenarios'][fee] is None or after['contribution_scenarios'][fee] is None
                else round(after['contribution_scenarios'][fee]-before['contribution_scenarios'][fee],4) for fee in ('0.12','0.15')}
        comparisons.append(dict(sku=sku,asin=asin,estimated_monthly_change=deltas,
                                causal_effect=None, decision='READ_ONLY_REVIEW'))
    recovered = []
    for pair, current in records('current_cost_scenario'):
        value = number(current.get('source_backed_unit_cost_mean_shipping'))
        if value is None:
            continue
        for month in ('07','08'):
            scenario = scenarios.get(pair+'|'+month)
            if scenario:
                recovered.append(dict(sku=current['sku'],asin=current['asin'],month=month,
                    basis='Current source-backed cost sensitivity; September shipping applied retrospectively, not July/August actual costs',
                    assumptions=current.get('assumptions'),unit_cost=value,
                    provenance=current['sources'],autonomous_release_qualified=False,
                    contribution_scenarios={str(fee): contribution(scenario['seller_sales'],scenario['seller_units'],value,scenario['sponsored_products_spend'],fee)
                        if scenario['mapping_status']=='SOURCE_EXACT' and scenario.get('validation_status')!='CONFLICTING' else None for fee in (.12,.15)}))
    unresolved = [dict(sku=s['sku'],asin=s['asin'],month=s['month'],missing_inputs=[name for name,value in
        [('seller_sales',s['seller_sales']),('seller_units',s['seller_units']),('SP_spend',s['sponsored_products_spend']),('historical_unit_cost',s['cost']['value'])] if value is None],
        mapping_status=s['mapping_status']) for s in scenarios.values() if any(v is None for v in s['contribution_scenarios'].values())]
    return dict(import_hash=fingerprint,mode='READ_ONLY',mappings=len(mapped),
        ambiguous_mappings=sum(m['status']=='AMBIGUOUS' for m in mapped.values()),
        costs=len(models),cost_status_counts={status:sum(m['status']==status for m in models.values()) for status in ('ESTIMATED','MISSING','CONFLICTING')},
        seller_history_rows=len(records('seller_history')),website_cost_rows=len(records('website_cost_source')),
        scenarios=list(scenarios.values()),comparisons=comparisons,current_cost_sensitivities=recovered,unresolved=unresolved,
        holdout_replay=holdout_replay(list(scenarios.values())),
        limitations=['Historical cost estimates applied retrospectively, not contemporaneous actual invoices.',
                     'Fees12/15percent are assumptions; Sponsored Products spend excludes other formats.',
                     'July and August each contain31days; no causal price-lift claim.',
                     'No counterfactual policy replay, refunds-adjusted profit or autonomous-release qualification.',
                     'Exact SKU joins only; no fuzzy family/grade/pack aliases.'])

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    conn=connect(args.db)
    try:
        fingerprint=import_evidence(conn,json.loads(args.manifest.read_text(encoding='utf-8-sig')))
        result=validate_history(conn,fingerprint)
        args.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in ('scenarios','comparisons')}))
    finally:
        conn.close()

if __name__=='__main__':
    main()
