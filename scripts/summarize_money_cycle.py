"""Сводка денежных опытов; ошибки прогонов не превращаются в нулевые результаты."""
import argparse
import csv
import hashlib
import json
import pickle
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def load(path):
    path=Path(path);meta=json.loads((path/'metadata.json').read_text())
    if meta['status']!='completed':raise ValueError('Незавершённый прогон: '+str(path))
    trace=path/'monthly.jsonl'
    rows=[json.loads(l) for l in trace.read_text().splitlines()]
    assert len(rows)==360
    assert [r['month'] for r in rows]==list(range(181,541))
    windows={}
    for first,last in ((211,240),(301,360),(481,540)):
        window=[r for r in rows if first<=r['month']<=last]
        avg=lambda f:statistics.mean(f(r) for r in window)
        goods={g:dict(output=avg(lambda r:r['output'].get(g,0)),stock=avg(lambda r:r['stocks'][g]),
            household_consumed=avg(lambda r:r['household_consumed'][g]),need=avg(lambda r:r['needs'][g]),
            funded_orders=avg(lambda r:r['funded_orders'].get(g,0)) if 'funded_orders' in window[0] else None,
            zero_output_months=sum(r['output'].get(g,0)<=1e-7 for r in window)) for g in window[0]['stocks']}
        previous=next(r for r in rows if r['month']==first-1)
        for g,d in goods.items():
            produced=sum(r['output'].get(g,0) for r in window)
            used=produced+previous['stocks'][g]-window[-1]['stocks'][g]
            d['used']=used/len(window)
            d['reproduction']=produced/used if used>1e-7 else None
        d=dict(coverage=avg(lambda r:r['coverage']),household_net=avg(lambda r:r['household_net']),
            household_cash_end=window[-1]['household_cash'],goods=goods)
        if 'income_coverage_at_quotes' in window[0]:
            d.update(income_coverage_at_quotes=avg(lambda r:r['income_coverage_at_quotes']),
                funded_basic_share=avg(lambda r:r['funded_basic_share']),basic_bill=avg(lambda r:r['basic_bill']),
                income=avg(lambda r:sum(r['household_incoming'].values())),
                spending=avg(lambda r:sum(r['household_outgoing'].values())),
                basic_quotes={g:avg(lambda r:r['basic_quotes'][g]) for g in window[0]['basic_quotes']},
                retained_profit=avg(lambda r:r['retained_positive_profit']))
        windows[f'{first}-{last}']=d
    result=dict(metadata=meta,trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),windows=windows,
        invariant_failures=sum(not all(r['invariants'].values()) for r in rows))
    if 'sector_flows' in rows[0]:
        net=defaultdict(float)
        for r in rows:
            for f in r['sector_flows']:
                net[f['sender_kind']]-=f['gold'];net[f['recipient_kind']]+=f['gold']
        result.update(sector_net_over360=dict(net),sector_balances_end=rows[-1]['sector_balances'])
        end=pickle.loads((path/'final.pickle').read_bytes())
        result['corporate_cash_above_reserves_end']=sum(end.cash_after_commitments(a['id'],
            a.get('project_reserve',0),a.get('working_reserve',0)) for a in end.accounts.values() if a['kind']=='enterprise')
    return result,rows


def run(args):
    result=dict(pilot={},validation={},failed_validation={});monthly=[]
    for variant in ('control','profit_income','budget_prices','owner_profit'):
        data,rows=load(Path(args.pilot)/variant);result['pilot'][variant]=data
        for r in rows:monthly.append(dict(case='pilot',variant=variant,month=r['month'],coverage=r['coverage'],
            household_net=r['household_net'],household_cash=r['household_cash'],
            income_coverage_at_quotes=r['income_coverage_at_quotes'],funded_basic_share=r['funded_basic_share']))
    reference=result['pilot']['control']['metadata']
    assert reference.get('observer_state_parity')
    for data in result['pilot'].values():
        assert data['metadata']['checkpoint_sha256']==reference['checkpoint_sha256']
        assert data['metadata']['experiment_sha256']==reference['experiment_sha256']
    if args.validation:
        for seed in (73129,17,2026):
            cases={};base=Path(args.baselines)/(str(seed)+'-current')
            repaired=Path(args.validation)/(str(seed)+'-control-repaired')
            if (repaired/'metadata.json').exists() and json.loads((repaired/'metadata.json').read_text())['status']=='completed':
                base=repaired
            cases['control'],base_rows=load(base)
            for r in base_rows:monthly.append(dict(case=str(seed),variant='control',month=r['month'],coverage=r['coverage'],
                household_net=r['household_net'],household_cash=r['household_cash'],
                income_coverage_at_quotes=r.get('income_coverage_at_quotes'),funded_basic_share=r.get('funded_basic_share')))
            for v in ('profit_income','budget_prices'):
                path=Path(args.validation)/(str(seed)+'-'+v)
                if not (path/'metadata.json').exists():
                    result['failed_validation'][str(seed)+'-'+v]=dict(status='incomplete',path=str(path))
                    continue
                if (path/'metadata.json').exists():
                    meta=json.loads((path/'metadata.json').read_text())
                    if meta['status']!='completed':
                        result['failed_validation'][str(seed)+'-'+v]=meta
                        continue
                d,rows=load(path);cases[v]=d
                assert d['metadata']['experiment_sha256']==result['pilot'][v]['metadata']['experiment_sha256']
                assert d['metadata']['checkpoint_sha256']==cases['control']['metadata']['checkpoint_sha256']
                old_meta=json.loads((Path(args.baselines)/(str(seed)+'-current')/'metadata.json').read_text())
                allowed={old_meta['engine_sha256'],cases['control']['metadata']['engine_sha256']}
                assert d['metadata']['engine_sha256'] in allowed
                if d['metadata']['engine_sha256']!=cases['control']['metadata']['engine_sha256']:
                    assert cases['control']['metadata'].get('observer_state_parity')
                    d['comparison_note']='Different engine hash only for reservation repair; repaired control state matches archived original control exactly.'
                for r in rows:monthly.append(dict(case=str(seed),variant=v,month=r['month'],coverage=r['coverage'],
                    household_net=r['household_net'],household_cash=r['household_cash'],
                    income_coverage_at_quotes=r['income_coverage_at_quotes'],funded_basic_share=r['funded_basic_share']))
            result['validation'][str(seed)]=cases
        result['archived_failed_runs']=[json.loads(p.read_text()) for p in Path(args.validation).glob('*original-failed/metadata.json')]
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    (out/'money_cycle_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (out/'money_cycle_monthly.csv').open('w',newline='') as target:
        w=csv.DictWriter(target,fieldnames=list(monthly[0]),lineterminator='\n');w.writeheader();w.writerows(monthly)
    for case,vs in [('pilot',result['pilot']),*result['validation'].items()]:
        print(case,{v:round(r['windows']['481-540']['coverage'],4) for v,r in vs.items()})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pilot',required=True);p.add_argument('--validation')
    p.add_argument('--baselines',default='/workspace/stock-planning-long');p.add_argument('--out',required=True)
    run(p.parse_args())
