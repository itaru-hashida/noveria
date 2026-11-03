"""Диагностика доходов и бюджетных цен; вмешательства не включены в игру."""
import argparse
import hashlib
import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy
from scripts.validate_stock_planning import record as basic_record

EPS = 1e-7


def weighted_median(values):
    rows = sorted((v, w) for v, w in values if w > EPS)
    if not rows:
        return None
    half = sum(w for _, w in rows)/2
    cumulative = 0.0
    for value, weight in rows:
        cumulative += weight
        if cumulative >= half:
            return value


class MoneyCycleEconomy(Economy):
    PROFIT_DISTRIBUTION_REVISION = 0
    HOUSEHOLD_TOP_UP_REVISION = 0
    BASIC_BUDGET_REVISION = 0
    def household_requests(self, households, needs, purpose, sells):
        if purpose == 'Базовые нужды':
            self.basic_signals = []
            for a in households:
                requests = []
                for good, rate in needs.items():
                    needed = self.population(a)*rate
                    missing = max(0, needed-a['stock'].get(good, 0))
                    quote = self.quoted_cost(a, good, missing, sells)[1]
                    requests.append((good, needed, missing, quote))
                bill = sum(q*p for _, _, q, p in requests)
                fraction = min(1, self.free_cash(a['id'])/bill) if bill > EPS else 0
                for good, needed, missing, quote in requests:
                    self.basic_signals.append(dict(id=a['id'], tile=self.market_tile(a), good=good,
                        needed=needed, missing=missing, quote=quote, fraction=fraction,
                        full_quote=self.quoted_cost(a,good,needed,sells)[1] if needed!=missing else quote,
                        budget_price=quote*fraction))
        super().household_requests(households, needs, purpose, sells)

    def update_prices(self):
        self.budget_price_changes = 0
        if self.variant != 'budget_prices':
            return super().update_prices()
        subjects = []
        for f in self.enterprises.values():
            if f['good'] in self.cfg['basic_needs']:
                subjects.append((f, 'price', self.market_tile(self.accounts[f['id']]), f['good'], f['price']))
        for a in self.accounts.values():
            if a['kind'] == 'household':
                continue
            firm = self.enterprises.get(a['id'])
            for good in self.cfg['basic_needs']:
                if a['stock'].get(good, 0) > EPS and not (firm and firm['good'] == good):
                    prices = a.get('ask_prices', {})
                    if good in prices:
                        subjects.append((prices, good, self.market_tile(a), good, prices[good]))
        super().update_prices()
        for target, key, tile, good, before in subjects:
            budgets = []
            for s in getattr(self, 'basic_signals', []):
                if s['good'] != good or s['missing'] <= EPS:
                    continue
                quote = self.landed(tile, s['tile'], good, 0)
                if quote is not None:
                    budgets.append((max(0, s['budget_price']-quote[1]), s['missing']))
            median = weighted_median(budgets)
            if median is None or median >= before-EPS:
                continue
            price = max(self.rules['minimum_price'], min(target[key],
                max(median, before*(1-self.rules['price_step']))))
            if price < target[key]-EPS:
                target[key] = price
                self.budget_price_changes += 1

    def pay_dividends(self, sells):
        if self.variant != 'profit_income':
            return super().pay_dividends(sells)
        for f in self.enterprises.values():
            if f.get('dividend_month') == self.meta['month']:
                continue
            f.update(dividends=0.0, dividend_month=self.meta['month'])
            if f['status'] == 'closed':
                continue
            recipients = [a for a in self.accounts.values()
                if a['kind'] == 'household' and a['state_id'] == f['state_id']]
            if not sum(self.population(a) for a in recipients):
                continue
            working = self.working_cash_reserve(f, sells)
            project = self.project_cash_reserve(f['id'], sells)
            available = self.cash_after_commitments(f['id'], project, working)
            limit = max(0, f['profit'])
            amount = min(limit, available)
            self.distribute(f['id'], recipients, amount, 'Участие населения в прибыли (исследование)')
            self.accounts[f['id']].update(working_reserve=working, project_reserve=project)
            f.update(dividends=amount, working_reserve=working, project_reserve=project,
                dividend_available_cash=available)
            row = dict(enterprise=f['id'], owner=f['owner_id'],
                owner_kind=self.accounts[f['owner_id']]['kind'], recipient_kind='household',
                profit=f['profit'], dividends=amount, profit_limit=limit, available_cash=available,
                working_reserve=working, project_reserve=project, experiment='profit_income')
            self.report['dividends'].append(row)
            self.report['diagnostics'].append(dict(kind='dividend', **row))


def record(e):
    row = basic_record(e)
    incoming, outgoing = defaultdict(float), defaultdict(float)
    for f in e.report['flows']:
        if e.accounts[f['recipient']]['kind'] == 'household': incoming[f['reason']] += f['gold']
        if e.accounts[f['sender']]['kind'] == 'household': outgoing[f['reason']] += f['gold']
    incomes = {f['id']: f['incoming'] for f in e.report['account_flows'] if f['kind'] == 'household'}
    bills, values = defaultdict(float), defaultdict(float)
    quote_value, needed_qty = defaultdict(float), defaultdict(float)
    for s in e.basic_signals:
        bills[s['id']] += s['needed']*s['full_quote']
        values[s['id']] += s['needed']*e.goods[s['good']]['price']
        quote_value[s['good']] += s['needed']*s['full_quote']
        needed_qty[s['good']] += s['needed']
    mass = sum(values.values())
    affordability = sum(values[i]*min(1, incomes.get(i,0)/bill) for i,bill in bills.items() if bill>EPS)/mass
    desired = sum(s['missing']*e.goods[s['good']]['price'] for s in e.basic_signals)
    funded = sum(o['quantity']*e.goods[o['good']]['price'] for o in e.report['orders']
        if o['side']=='buy' and o['purpose']=='Базовые нужды')
    row.update(household_incoming=dict(incoming), household_outgoing=dict(outgoing),
        income_coverage_at_quotes=affordability, basic_bill=sum(bills.values()),
        funded_basic_share=funded/desired if desired>EPS else 1,
        basic_quotes={g:quote_value[g]/needed_qty[g] for g in needed_qty if needed_qty[g]>EPS},
        sector_balances=e.report['balances'], sector_flows=e.report['sector_flows'],
        retained_positive_profit=sum(max(0,f['profit']-f['dividends']) for f in e.enterprises.values()),
        price_changes=e.budget_price_changes,
        gold=e.gold())
    requested, unmet = defaultdict(float), defaultdict(float)
    for o in e.report['orders']:
        if o['side']=='buy':
            requested[o['good']] += o['quantity']
            unmet[o['good']] += o.get('unmet_solvent',0)
    row['funded_orders']=dict(requested)
    row['unmet_funded_orders']=dict(unmet)
    assert -EPS <= affordability <= 1+EPS and -EPS <= row['funded_basic_share'] <= 1+EPS
    return row


def run(args):
    if Economy.PROFIT_DISTRIBUTION_REVISION:
        raise ValueError('Доход от бизнеса уже включён в игру; историческое исследование: 9feffca.')
    if args.months < 1:
        raise ValueError('Нужен положительный горизонт')
    out, path = Path(args.out), Path(args.checkpoint)
    out.mkdir(parents=True, exist_ok=False)
    e = pickle.loads(path.read_bytes())
    e.__class__ = MoneyCycleEconomy
    e.variant = args.variant
    meta = dict(variant=args.variant, months=args.months, initial_month=e.meta['month'], seed=e.seed,
        checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        experiment_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        engine_sha256=hashlib.sha256(Path('modules/economy/v3.py').read_bytes()).hexdigest(), status='running')
    try:
        with (out/'monthly.jsonl').open('w') as target:
            for i in range(args.months):
                e.step(); r=record(e)
                assert all(r['invariants'].values())
                target.write(json.dumps(r,ensure_ascii=False)+'\n'); target.flush()
                if (i+1)%60==0:print(args.variant,e.meta['month'],round(r['coverage'],3),flush=True)
        if args.verify_state:
            expected=pickle.loads(Path(args.verify_state).read_bytes())
            for key in ('accounts','enterprises','meta'):
                assert getattr(e,key)==getattr(expected,key), key
            assert e.rng.getstate()==expected.rng.getstate()
            meta['observer_state_parity']=True
        e.__class__=Economy
        with (out/'final.pickle').open('wb') as target:pickle.dump(e,target)
        meta['status']='completed'
    except BaseException as exc:
        meta.update(status='failed',error=str(exc)); raise
    finally:
        (out/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint',required=True)
    p.add_argument('--out',required=True)
    p.add_argument('--variant',choices=('control','profit_income','budget_prices'),required=True)
    p.add_argument('--months',type=int,default=360)
    p.add_argument('--verify-state')
    run(p.parse_args())
