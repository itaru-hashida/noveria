"""Компактные факты о дефиците: ограничения не выдаются за доказанные причины."""
from collections import defaultdict

from .support import EPS


class FoodDiagnostics:
    def __init__(self, engine, households, sells):
        self.opening = {}
        for a in households:
            foods = {}
            for good, rate in engine.cfg['basic_needs'].items():
                need = engine.population(a)*rate
                held = engine.free_stock(a['id'], good)
                price = engine.quoted_cost(a, good, max(0, need-held), sells)[1]
                foods[good] = dict(need=need, held=held, quote=price)
            self.opening[a['id']] = dict(state=a['state_id'], province=a['province_id'],
                tile=a['tile_id'], cash=engine.free_cash(a['id']), balance=a['balance'],
                protected=a.get('protected', 0), reserved=a.get('reserved', 0), foods=foods)

    def clearing(self, engine, sells):
        """Снимок до снятия резервов, нового выпуска, зарплат и распределений."""
        self.market = {g: [dict(order=o['id'], seller=o['participant'], tile=o['tile_id'],
            ask=o['price'], offered=o['quantity'], remaining=o['remaining'])
            for o in sells.get(g, []) if o['status'] not in ('cancelled', 'expired')]
            for g in engine.cfg['basic_needs']}
        orders = defaultdict(list)
        for o in engine.report['orders']:
            if o['side'] == 'buy' and o['purpose'] == 'Базовые нужды':
                orders[o['participant'], o['good']].append(o)
        for identity, row in self.opening.items():
            a = engine.accounts[identity]
            row['cash_after_trade'] = max(0, a['balance']-a.get('protected', 0))
            for good, food in row['foods'].items():
                buys = orders[identity, good]
                food['filled'] = sum(o['quantity']-o['remaining'] for o in buys)
                live = [o for o in buys if not o.get('superseded_by_top_up')
                    and o['status'] not in ('cancelled', 'expired')]
                food['funded_unfilled'] = sum(min(o['remaining'], o['reserved']/o['price']) for o in live)
                food['orders'] = [o['id'] for o in buys]
                food['execution'] = [dict(order=o['id'], remaining=o['remaining'],
                    backing=o['reserved'], limit=o['price'], reason=o.get('reason', ''),
                    observed=o.get('match_observation', {}).get('reason_code'))
                    for o in live if o['remaining'] > EPS]
                quotes = [(q[0], o['remaining']) for o in self.market[good]
                    if o['remaining'] > EPS
                    and (q := engine.landed(o['tile'], row['tile'], good, o['ask']))]
                food['cheapest_remaining'] = min((p for p, _ in quotes), default=None)
                food['reachable_remaining'] = sum(q for _, q in quotes)

    def finish(self, engine):
        totals = {g: dict(need=0., consumed=0., deficit=0., unfunded=0.,
            funded_unfilled=0., accounting_unknown=0., root_unknown=0.) for g in self.market}
        households = []
        for identity, opening in self.opening.items():
            foods = {}
            for good, initial in opening['foods'].items():
                need = engine.accounts[identity]['needs'][good]
                deficit = max(0, need['needed']-need['consumed'])
                filled = initial['filled']
                requested = max(0, initial['need']-initial['held'])
                funded = initial['funded_unfilled']
                unfunded = max(0, requested-filled-funded)
                residual = deficit-unfunded-funded
                total = totals[good]
                for key, value in dict(need=need['needed'], consumed=need['consumed'],
                        deficit=deficit, unfunded=unfunded, funded_unfilled=funded,
                        accounting_unknown=abs(residual), root_unknown=deficit).items():
                    total[key] += value
                if deficit > EPS or abs(residual) > EPS:
                    foods[good] = dict(initial, consumed=need['consumed'], deficit=deficit,
                        unfunded=unfunded, accounting_residual=residual, root_unknown=deficit)
            if foods:
                households.append(dict(id=identity, **{k: v for k, v in opening.items() if k != 'foods'},
                    foods=foods))
        base = dict(schema=1, month=engine.meta['month'],
            rules={k: engine.meta.get(k) for k in ('rules_version', 'input_bid_revision',
                'stock_planning_revision', 'profit_distribution_revision', 'household_top_up_revision', 'basic_budget_revision')},
            totals=totals, households=households)
        if not households:
            return dict(base, market={}, producers=[], goods={}, cross_border=[])
        # Связи через товар: список поставщиков и рецепты хранятся один раз.
        required = set(self.market)
        pending = list(required)
        while pending:
            for good in engine.goods[pending.pop()]['inputs']:
                if good not in required:
                    required.add(good); pending.append(good)
        inputs = defaultdict(list)
        for o in engine.report['orders']:
            if o['side'] == 'buy' and o['purpose'] == 'Сырьё':
                inputs[o['participant']].append(dict(order=o['id'], good=o['good'],
                    quantity=o['quantity'], filled=o['quantity']-o['remaining'],
                    limit=o['price'], status=o.get('execution_status', o['status']), reason=o.get('reason', ''),
                    observed=o.get('match_observation', {}).get('reason_code')))
        producers = []
        for f in engine.enterprises.values():
            if f['good'] not in required:
                continue
            plan = f.get('production_plan', {})
            current = plan.get('month') == engine.meta['month']
            obs = f.get('production_observation', {})
            producers.append(dict(id=f['id'], good=f['good'], tile=f['tile_id'],
                state=f['state_id'], province=f['province_id'],
                status=f['status'], available_from=f['available_from'], capacity=f['capacity'],
                requested=plan.get('requested_output') if current else None,
                plan=plan.get('output') if current else None, output=f['output'],
                planning_reason=plan.get('reason') if current else None,
                free_cash=plan.get('free_cash') if current else None,
                forecast=plan.get('sales_forecast') if current else None,
                demand=plan.get('reachable_demand') if current else None,
                margin=plan.get('expected_margin') if current else None,
                binding={k: v for k, v in obs.get('constraints', {}).items() if v <= f['output']+EPS},
                constraints_observed=bool(obs), inputs=inputs[f['id']]))
        selected = {h['id'] for h in households}
        flows = defaultdict(lambda: dict(incoming=defaultdict(float), outgoing=defaultdict(float)))
        borders = defaultdict(float)
        for f in engine.report['flows']:
            if f['recipient'] in selected:
                flows[f['recipient']]['incoming'][f['reason']] += f['gold']
            if f['sender'] in selected:
                flows[f['sender']]['outgoing'][f['reason']] += f['gold']
            s, t = engine.accounts[f['sender']]['state_id'], engine.accounts[f['recipient']]['state_id']
            if s != t:
                borders[s, t, f.get('good')] += f['gold']
        for h in households:
            h['flows'] = dict(flows[h['id']])
        return dict(base, market=self.market, producers=producers,
            goods={g: dict(inputs=engine.goods[g]['inputs'],
                producers=[f['id'] for f in producers if f['good'] == g]) for g in sorted(required)},
            cross_border=[dict(sender=s, recipient=t, good=g, gold=q)
                for (s, t, g), q in borders.items()])
