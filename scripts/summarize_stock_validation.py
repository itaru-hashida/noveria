"""Сводка длинной парной проверки: питание, деньги, выпуск и запасы отраслей."""
import argparse
import csv
import hashlib
import json
import pickle
import statistics
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.validate_stock_planning import LegacyEconomy


def load(path):
    meta=json.loads((path/'metadata.json').read_text())
    if meta['status']!='completed':raise ValueError('Незавершённый прогон: '+str(path))
    trace=path/'monthly.jsonl'
    rows=[json.loads(l) for l in trace.read_text().splitlines()]
    if len(rows)!=meta['months']:raise ValueError('Неполная трасса')
    if [r['month'] for r in rows]!=list(range(meta['initial_month']+1,meta['initial_month']+meta['months']+1)):
        raise ValueError('Нарушен порядок месяцев')
    windows={}
    for first,last in ((211,240),(301,360),(481,540)):
        window=[r for r in rows if first<=r['month']<=last]
        if len(window)!=last-first+1:raise ValueError('Неполное окно')
        avg=lambda fn:statistics.mean(fn(r) for r in window)
        goods={}
        previous=next(r for r in rows if r['month']==first-1)
        for g in window[0]['stocks']:
            produced=sum(r['output'].get(g,0) for r in window)
            used=produced+previous['stocks'][g]-window[-1]['stocks'][g]
            goods[g]=dict(output=produced/len(window),used=used/len(window),
                stock=avg(lambda r:r['stocks'][g]),household_consumed=avg(lambda r:r['household_consumed'][g]),
                need=avg(lambda r:r['needs'][g]),zero_output_months=sum(r['output'].get(g,0)<1e-7 for r in window))
        windows[str(first)+'-'+str(last)]=dict(coverage=avg(lambda r:r['coverage']),
            household_cash_end=window[-1]['household_cash'], household_net=avg(lambda r:r['household_net']),
            open_end=window[-1]['open'],goods=goods)
    # Только собственные доверенные состояния, записанные длинным прогоном.
    end=pickle.loads((path/'final.pickle').read_bytes())
    history=end.meta['market_history'][-3:]
    requested={g:sum(m['requested'] for h in history for m in h['markets'] if m['good']==g)/len(history) for g in end.goods}
    return dict(metadata=meta,trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),windows=windows,
        funded_requested_last3=requested,
        invariant_failures=sum(not all(r['invariants'].values()) for r in rows)),rows


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();root=Path(args.root);out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    cases={};monthly=[]
    for label in ('73129','17','2026','fresh-73129'):
        cases[label]={}
        for variant in ('legacy','current'):
            result,rows=load(root/(label+'-'+variant));cases[label][variant]=result
            for r in rows:
                monthly.append(dict(case=label,variant=variant,month=r['month'],coverage=r['coverage'],
                    household_cash=r['household_cash'],household_net=r['household_net'],
                    **{g+'_output':r['output'].get(g,0) for g in r['stocks']},
                    **{g+'_stock':v for g,v in r['stocks'].items()}))
        a,b=cases[label]['legacy'],cases[label]['current']
        for key in ('checkpoint_sha256','months','engine_sha256'):
            assert a['metadata'][key]==b['metadata'][key]
    (out/'stock_validation_summary.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
    with (out/'stock_validation_monthly.csv').open('w',newline='') as target:
        writer=csv.DictWriter(target,fieldnames=list(monthly[0]),lineterminator='\n');writer.writeheader();writer.writerows(monthly)
    for label,variants in cases.items():
        print(label,[(v,{w:round(d['coverage'],3) for w,d in r['windows'].items()}) for v,r in variants.items()])
