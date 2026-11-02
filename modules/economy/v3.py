"""Экономика V3: обеспеченные заявки, реальные расходы и один выпуск за месяц."""
import copy
import math
from collections import defaultdict

from .support import EPS, EconomyServices


OWNER_KINDS = ('private', 'merchant', 'treasury', 'municipal')
OWNER_BASKET = ('bread', 'fish', 'cloth')
OWNER_CONSUMPTION = 'Личное потребление собственника'
CONSUMPTION_PURPOSES = ('Базовые нужды', 'Дополнительные нужды', OWNER_CONSUMPTION)


class Economy(EconomyServices):
    RULES_VERSION = 3

    def reset_report(self):
        super().reset_report()
        self.report.update(rules_version=3, investment_decisions=[], dividends=[],
            owner_consumption=[], owner_consumed={})

    def open_enterprise(self, *args, **kwargs):
        e = super().open_enterprise(*args, **kwargs)
        e.update(cost_of_sales=0, tax=0, wages=0, unviable_months=0)
        return e

    def activate_rules(self):
        previous = self.meta.get('rules_version', self.meta.get('version', 1))
        self.meta.setdefault('previous_rules_version', previous)
        self.meta.update(version=3, rules_version=3, rng_seed=self.seed,
            rule_changes=['Обеспеченные заявки и один проход торговли существующими товарами',
                'Налог с конечных продаж вместо налога на прибыль',
                'Выпуск по доступному спросу и неотрицательной ожидаемой марже',
                'Сохранённые стройки и оплаченное собственное расширение',
                'Возврат капитала закрытых фирм и продажа их товарных остатков',
                'Дивиденды после резервов и рыночное потребление собственников'])
        self.cfg.setdefault('consumption_tax', 0.10)
        self.cfg.setdefault('dividend_profit_share', 0.50)
        self.cfg.setdefault('working_reserve_months', 1)
        self.cfg.setdefault('owner_consumption_rate', 0.05)
        if not 0 <= self.cfg['consumption_tax'] < 1:
            raise ValueError('Налог с потребительских продаж должен быть от 0 до 1')
        for key in ('dividend_profit_share', 'owner_consumption_rate'):
            if not math.isfinite(self.cfg[key]) or not 0 <= self.cfg[key] <= 1:
                raise ValueError('Некорректная доля: '+key)
        if not math.isfinite(self.cfg['working_reserve_months']) or self.cfg['working_reserve_months'] < 0:
            raise ValueError('Некорректный срок оборотного резерва')
        for a in self.accounts.values():
            a.setdefault('reserved', 0.0)
            a.setdefault('protected', 0.0)
            a.setdefault('stock_reserved', {})
        for e in self.enterprises.values():
            e.setdefault('available_from', e['opened']+1 if e['opened'] else 0)
            e.setdefault('sales_history', [])
            e.setdefault('sales_forecast', e['capacity']*self.rules['initial_sales_fraction'])
            e.setdefault('asset_value', sum(q*self.goods[g]['price'] for g, q in self.construction_bill(e['capacity']).items()))
            e.setdefault('unviable_months', 0)

    def free_cash(self, account):
        a = self.accounts[account]
        return max(0.0, a['balance']-a.get('reserved', 0)-a.get('protected', 0))

    def market_tile(self, account):
        return account.get('cell_id') or account['tile_id']

    def market_province(self, account):
        return self.tiles.get(self.market_tile(account), {}).get('province_id', account['province_id'])

    def project_cash_reserve(self, account, sells):
        a = self.accounts[account]
        project = a.get('project')
        if not project:
            return 0.0
        distance, _ = self.path(self.market_tile(a), project['location']['tile_id'])
        bill = self.construction_bill(project['capacity'])
        delivery = (sum(q*self.goods[g]['weight'] for g, q in bill.items())
            * distance*self.cfg['transport_rate']) if distance is not None else project.get('delivery', 0)
        needed = project['working_cash']+delivery
        for good, quantity in bill.items():
            # Купленные материалы и обеспеченные заявки не требуют повторного резерва денег.
            ordered = sum(o['remaining'] for o in self.report['orders']
                if o['participant'] == account and o['side'] == 'buy'
                and o['purpose'] == 'Строительные материалы' and o['good'] == good
                and o['status'] not in ('cancelled', 'expired') and o['reserved'] > EPS)
            missing = max(0, quantity-a['stock'].get(good, 0)-ordered)
            needed += missing*self.quoted_cost(a, good, missing, sells)[1]
        return needed

    def working_cash_reserve(self, e, sells):
        recipe = self.goods[e['good']]
        quantity = e['capacity']*self.cfg['working_reserve_months']
        needed = quantity*recipe['labor']*self.cfg['wage']
        for good, ratio in recipe['inputs'].items():
            missing = max(0, quantity*ratio-self.free_stock(e['id'], good))
            needed += missing*self.quoted_cost(self.accounts[e['id']], good, missing, sells)[1]
        return needed

    def cash_after_commitments(self, account, project_reserve, working_reserve=0):
        a = self.accounts[account]
        # protected уже содержит часть обязательств; повторно её не вычитаем.
        hold = max(a.get('protected', 0), project_reserve+working_reserve)
        return max(0, a['balance']-a.get('reserved', 0)-hold)

    def pay_dividends(self, sells):
        for e in self.enterprises.values():
            if e.get('dividend_month') == self.meta['month']:
                continue
            e.update(dividends=0.0, dividend_month=self.meta['month'])
            if e['status'] == 'closed' or self.accounts[e['owner_id']]['kind'] not in OWNER_KINDS:
                continue
            working = self.working_cash_reserve(e, sells)
            project = self.project_cash_reserve(e['id'], sells)
            available = self.cash_after_commitments(e['id'], project, working)
            limit = self.cfg['dividend_profit_share']*max(0, e['profit'])
            amount = min(limit, available)
            self.transfer(e['id'], e['owner_id'], amount, 'Дивиденды')
            self.accounts[e['id']].update(working_reserve=working, project_reserve=project)
            e.update(dividends=amount, working_reserve=working, project_reserve=project,
                dividend_available_cash=available)
            row = dict(enterprise=e['id'], owner=e['owner_id'],
                owner_kind=self.accounts[e['owner_id']]['kind'], profit=e['profit'], dividends=amount,
                profit_limit=limit, available_cash=available, working_reserve=working, project_reserve=project)
            self.report['dividends'].append(row)
            self.report['diagnostics'].append(dict(kind='dividend', **row))

    def owner_requests(self, sells):
        owners = [a for a in self.accounts.values() if a['kind'] in ('private', 'merchant')]
        for a in self.shuffled(owners):
            project = self.project_cash_reserve(a['id'], sells)
            available = self.cash_after_commitments(a['id'], project)
            budget = self.cfg['owner_consumption_rate']*available
            a.update(owner_free_cash=available, owner_consumption_budget=budget, project_reserve=project)
            row = dict(owner=a['id'], free_cash=available, project_reserve=project,
                budget=budget, spent=0.0, tax=0.0, freight=0.0, consumed={}, orders=[])
            self.report['owner_consumption'].append(row)
            for good in OWNER_BASKET:
                share = budget/len(OWNER_BASKET)
                price = self.reference_price(a, good, sells)
                quantity = share/price
                _, ceiling, _ = self.quoted_cost(a, good, quantity, sells)
                order = self.order(a['id'], good, share/ceiling, 'buy', ceiling, OWNER_CONSUMPTION)
                if order:
                    row['orders'].append(order['id'])

    def consume_owner_purchases(self):
        for row in self.report['owner_consumption']:
            a = self.accounts[row['owner']]
            for order in self.report['orders']:
                if order['id'] not in row['orders']:
                    continue
                filled = order['quantity']-order['remaining']
                quantity = max(0, filled-order.get('consumed_quantity', 0))
                if quantity > EPS:
                    self.use_stock(a['id'], order['good'], quantity)
                    order['consumed_quantity'] = filled
                    good = order['good']
                    row['consumed'][good] = row['consumed'].get(good, 0)+quantity
                    self.report['owner_consumed'][good] = self.report['owner_consumed'].get(good, 0)+quantity
                    self.report['consumed'][good] = self.report['consumed'].get(good, 0)+quantity
            trades = [t for t in self.report['trades'] if t['buy_order'] in row['orders']]
            row.update(spent=sum(t['gold']+t['freight'] for t in trades),
                tax=sum(t['tax'] for t in trades), freight=sum(t['freight'] for t in trades),
                unfilled={o['good']: o['remaining'] for o in self.report['orders'] if o['id'] in row['orders']})
            row['unused_budget'] = max(0, row['budget']-row['spent'])
            a.update(owner_consumption_spent=row['spent'], owner_consumed=copy.deepcopy(row['consumed']),
                owner_consumption_tax=row['tax'], owner_consumption_freight=row['freight'])

    def transfer(self, sender, recipient, amount, reason):
        if amount > self.free_cash(sender)+EPS:
            raise ValueError('Деньги зарезервированы или недостаточны: '+sender)
        return super().transfer(sender, recipient, amount, reason)

    def locked_material(self, account, good):
        return self.accounts[account].get('project', {}).get('materials', {}).get(good, 0)

    def free_stock(self, account, good):
        a = self.accounts[account]
        return max(0, a['stock'].get(good, 0)-a.get('stock_reserved', {}).get(good, 0)-self.locked_material(account, good))

    def use_stock(self, account, good, quantity):
        if quantity > self.free_stock(account, good)+EPS:
            raise ValueError('Товар уже закреплён за продажей или стройкой')
        return super().use_stock(account, good, quantity)

    def order(self, participant, good, quantity, side, price, purpose):
        if not math.isfinite(quantity) or not math.isfinite(price) or price <= 0 or quantity < 0:
            raise ValueError('Некорректная заявка')
        a = self.accounts[participant]
        if side == 'buy':
            if self.enterprises.get(participant, {}).get('status') == 'closed':
                return None
            quantity = min(quantity, self.free_cash(participant)/price)
        elif side == 'sell':
            quantity = min(quantity, self.free_stock(participant, good))
        else:
            raise ValueError('Неизвестная сторона заявки')
        if quantity <= EPS:
            return None
        o = dict(id=f'{self.meta["month"]}-{len(self.report["orders"])}', participant=participant,
            good=good, quantity=quantity, remaining=quantity, price=price, side=side,
            purpose=purpose, status='active', reason='', expires=self.meta['month'],
            final_consumption=side == 'buy' and purpose in CONSUMPTION_PURPOSES,
            state_id=a['state_id'], province_id=self.market_province(a), tile_id=self.market_tile(a),
            reserved=quantity*price if side == 'buy' else 0, backing=quantity*price if side == 'buy' else quantity)
        if side == 'buy':
            a['reserved'] = a.get('reserved', 0)+o['reserved']
        else:
            a.setdefault('stock_reserved', {})[good] = a.get('stock_reserved', {}).get(good, 0)+quantity
        self.report['orders'].append(o)
        return o

    def cancel_order(self, order, reason='Отмена заявки'):
        if order['status'] in ('cancelled', 'expired'):
            return
        filled = order['status'] == 'filled'
        a = self.accounts[order['participant']]
        if order['side'] == 'buy':
            a['reserved'] = max(0, a.get('reserved', 0)-order['reserved'])
            order['reserved'] = 0.0
        else:
            a['stock_reserved'][order['good']] = max(0, a['stock_reserved'].get(order['good'], 0)-order['remaining'])
        if not filled:
            order.update(status='cancelled', reason=reason)

    def landed(self, seller_tile, buyer_tile, good, price):
        distance, path = self.path(seller_tile, buyer_tile)
        if distance is None:
            return None
        freight = distance*self.goods[good]['weight']*self.cfg['transport_rate']
        return price+freight, freight, distance, path

    def offers(self, buyer, good, sells):
        options = []
        for sell in sells.get(good, []):
            if sell['participant'] == buyer['id'] or sell['remaining'] <= EPS or sell['status'] in ('cancelled', 'expired'):
                continue
            quote = self.landed(sell['tile_id'], self.market_tile(buyer), good, sell['price'])
            if quote:
                options.append((quote[0], sell, quote))
        return sorted(options, key=lambda item: (item[0], item[1]['id']))

    def reference_price(self, buyer, good, sells):
        offers = self.offers(buyer, good, sells)
        if offers:
            return offers[0][0]
        signal = self.market_signal(self.meta.get('market_history', []), buyer['state_id'], self.market_province(buyer), good)
        return signal['landed_price'] or self.goods[good]['price']

    def quoted_cost(self, buyer, good, quantity, sells):
        left, value, ceiling = quantity, 0.0, 0.0
        for price, sell, _ in self.offers(buyer, good, sells):
            q = min(left, sell['remaining'])
            value += q*price
            ceiling = max(ceiling, price)
            left -= q
            if left <= EPS:
                break
        if left > EPS:
            reference = self.reference_price(buyer, good, sells)
            value += left*reference
            ceiling = max(ceiling, reference)
        return value, max(ceiling, EPS), left <= EPS

    def match(self, buy, sells):
        if buy is None or buy['status'] in ('cancelled', 'expired', 'filled'):
            return
        buyer = self.accounts[buy['participant']]
        options = self.offers(buyer, buy['good'], sells)
        observation = self.observe_match(buy, sells, options)
        # Равная полная цена не даёт постоянного преимущества одному ID.
        ordered, index = [], 0
        while index < len(options):
            end = index+1
            while end < len(options) and math.isclose(options[index][0], options[end][0], rel_tol=1e-12, abs_tol=1e-12):
                end += 1
            ordered.extend(self.shuffled(options[index:end], key=lambda row: row[1]['id']))
            index = end
        for price, sell, quote in ordered:
            if price > buy['price']+EPS:
                continue
            q = min(buy['remaining'], sell['remaining'], buy['reserved']/price)
            if q <= EPS:
                continue
            seller = self.accounts[sell['participant']]
            freight, distance, path = quote[1:]
            # Освобождается только резерв исполненной части, остальной остаётся защищён.
            release = q*buy['price']
            buyer['reserved'] = max(0, buyer['reserved']-release)
            buy['reserved'] = max(0, buy['reserved']-release)
            seller['stock_reserved'][buy['good']] = max(0, seller['stock_reserved'][buy['good']]-q)
            value = self.use_stock(sell['participant'], buy['good'], q)
            self.add_stock(buy['participant'], buy['good'], q, q*price)
            gross = q*sell['price']
            tax = gross*self.cfg['consumption_tax'] if buy.get('final_consumption', False) else 0.0
            flow_start = len(self.report['flows'])
            trade_id = f'{self.meta["month"]}:trade:{len(self.report["trades"])}'
            self.transfer(buy['participant'], sell['participant'], gross-tax, 'Товар')
            self.transfer(buy['participant'], 'treasury:'+seller['state_id'], tax, 'Налог с конечной потребительской продажи')
            self.transfer(buy['participant'], 'carrier:'+seller['state_id'], q*freight, 'Перевозка')
            for flow in self.report['flows'][flow_start:]:
                flow.update(buy_order=buy['id'], sell_order=sell['id'], good=buy['good'], quantity=q,
                    trade_id=trade_id)
            firm = self.enterprises.get(sell['participant'])
            if firm:
                firm['revenue'] += gross
                firm['tax'] = firm.get('tax', 0)+tax
                firm['cost_of_sales'] = firm.get('cost_of_sales', 0)+value
            if buy['purpose'] == 'Строительные материалы':
                materials = buyer['project']['materials']
                materials[buy['good']] = materials.get(buy['good'], 0)+q
            buy['remaining'] = max(0, buy['remaining']-q)
            sell['remaining'] = max(0, sell['remaining']-q)
            self.report['trades'].append(dict(trade_id=trade_id, good=buy['good'], quantity=q, price=sell['price'],
                gold=gross, seller_gold=gross-tax, tax=tax, freight=q*freight, distance=distance, path=path,
                buyer=buy['participant'], seller=sell['participant'], buy_order=buy['id'], sell_order=sell['id'],
                buyer_state=buy['state_id'], seller_state=sell['state_id'],
                buyer_province=buy['province_id'], seller_province=sell['province_id'],
                purpose=buy['purpose'], final_consumption=buy.get('final_consumption', False)))
            sell['status'] = 'filled' if sell['remaining'] <= EPS else 'partial'
            if buy['remaining'] <= EPS:
                break
        buy['status'] = 'filled' if buy['remaining'] <= EPS else 'partial' if buy['remaining'] < buy['quantity'] else 'unfilled'
        self.finish_match_observation(buy, sells, observation)
        if buy['remaining'] > EPS:
            buy['reason'] = observation['reason_code']

    def observe_match(self, buy, sells, options):
        # Снимок снимается до исполнения, без изменения рынка и генератора случайных чисел.
        quotes = {sell['id']: quote for _, sell, quote in options}
        candidates = []
        for sell in sells.get(buy['good'], []):
            if sell['participant'] == buy['participant']:
                continue
            active = sell['status'] not in ('cancelled', 'expired') and sell['remaining'] > EPS
            quote = quotes.get(sell['id']) or self.landed(sell['tile_id'], buy['tile_id'], buy['good'], sell['price'])
            candidates.append(dict(sell_order=sell['id'], seller=sell['participant'],
                price=sell['price'], remaining_before=sell['remaining'], status_before=sell['status'],
                active=active, route_available=quote is not None,
                landed_price=quote[0] if quote else None, freight_per_unit=quote[1] if quote else None,
                distance=quote[2] if quote else None,
                physical_stock=self.accounts[sell['participant']]['stock'].get(buy['good'], 0)))
        observation = dict(version=1, match_index=sum('match_observation' in o for o in self.report['orders']),
            remaining_before=buy['remaining'], reserved_before=buy['reserved'], candidates=candidates)
        buy['match_observation'] = observation
        return observation

    def finish_match_observation(self, buy, sells, observation):
        by_id = {sell['id']: sell for sell in sells.get(buy['good'], [])}
        for row in observation['candidates']:
            row['remaining_after'] = by_id[row['sell_order']]['remaining']
        active = [r for r in observation['candidates'] if r['active'] and r['remaining_after'] > EPS]
        reachable = [r for r in active if r['route_available']]
        affordable = [r for r in reachable if r['landed_price'] <= buy['price']+EPS]
        holders = [dict(account=a['id'], quantity=a['stock'][buy['good']],
            stock_reserved=a.get('stock_reserved', {}).get(buy['good'], 0),
            project_locked=self.locked_material(a['id'], buy['good']))
            for a in self.accounts.values() if a['id'] != buy['participant'] and a['stock'].get(buy['good'], 0) > EPS]
        if buy['remaining'] <= EPS:
            code = 'filled'
        elif affordable:
            code = 'insufficient_order_reserve' if all(
                min(buy['remaining'], r['remaining_after'], buy['reserved']/r['landed_price']) <= EPS
                for r in affordable) else 'executable_not_matched'
        elif reachable:
            code = 'landed_price_above_limit'
        elif active:
            code = 'offer_exhausted' if any(r['route_available'] and r['remaining_after'] <= EPS
                and r['status_before'] not in ('cancelled', 'expired')
                for r in observation['candidates']) else 'no_route'
        elif any(r['remaining_before'] > EPS and r['active'] or
                 r['remaining_before'] <= EPS and by_id[r['sell_order']]['quantity'] > EPS
                 and r['status_before'] not in ('cancelled', 'expired')
                 for r in observation['candidates']):
            code = 'offer_exhausted'
        else:
            code = 'stock_not_offered' if holders else 'no_physical_stock'
        observation.update(reason_code=code, remaining_after=buy['remaining'], reserved_after=buy['reserved'])
        if buy['remaining'] > EPS:
            observation['stock_holders_after_match'] = holders

    def sell_orders(self):
        result = defaultdict(list)
        for a in self.shuffled(self.accounts.values()):
            e = self.enterprises.get(a['id'])
            if a['kind'] == 'household' or (e and e.get('suspended')):
                continue
            for good in sorted(a['stock']):
                if e and e['status'] != 'closed' and good != e['good']:
                    continue
                prices = a.setdefault('ask_prices', {})
                price = e['price'] if e and good == e['good'] else prices.setdefault(good, self.goods[good]['price'])
                o = self.order(a['id'], good, self.free_stock(a['id'], good), 'sell', price, 'Продажа запасов')
                if o:
                    result[good].append(o)
        return result

    def customer_quotes(self, e, orders=None):
        result = []
        orders = getattr(self, '_planning_orders', self.report.get('orders', [])) if orders is None else orders
        for o in orders:
            if o['side'] != 'buy' or o['good'] != e['good'] or o['participant'] == e['id'] or o['remaining'] <= EPS or o['status'] in ('cancelled', 'expired'):
                continue
            quote = self.landed(e['tile_id'], o['tile_id'], e['good'], e['price'])
            if quote:
                result.append((o, o['price']-quote[1]))
        return result

    def reachable_demand(self, e, sells):
        demand = 0.0
        for order, limit in self.customer_quotes(e):
            if limit+EPS < e['price']:
                continue
            buyer = self.accounts[order['participant']]
            landed = self.landed(e['tile_id'], order['tile_id'], e['good'], e['price'])[0]
            volume = min(order['remaining'], order['reserved']/landed)
            cheaper = sum(sell['remaining'] for price, sell, _ in self.offers(buyer, e['good'], sells)
                if sell['participant'] != e['id'] and price < landed-EPS)
            demand += max(0, volume-cheaper)
        return demand

    def unit_cost(self, e, sells):
        recipe = self.goods[e['good']]
        return recipe['labor']*self.cfg['wage']+sum(ratio*self.reference_price(e, good, sells) for good, ratio in recipe['inputs'].items())

    def net_price(self, e):
        # Для смешанного товара консервативно учитывается конечная продажа.
        consumer = e['good'] in (self.cfg['basic_needs'] | self.cfg['extra_needs'])
        return e['price']*(1-self.cfg['consumption_tax'] if consumer else 1)

    def plan(self, e, history=None, sells=None):
        sells = sells or defaultdict(list)
        observations = e.get('sales_history', [])[-3:]
        own = sum(h['sold'] for h in observations)/len(observations) if observations else e['sales_forecast']
        demand = self.reachable_demand(e, sells)
        sold_now = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == e['id'] and t['good'] == e['good'])
        forecast = max(own, sold_now+demand)
        stock = self.accounts[e['id']]['stock'].get(e['good'], 0)
        e.update(sales_forecast=forecast, reachable_demand=demand,
            expected_unit_cost=self.unit_cost(e, sells), expected_price=self.net_price(e))
        e['expected_margin'] = e['expected_price']-e['expected_unit_cost']
        target = max(0, min(e['capacity'], forecast-stock))
        e['production_limit'] = 'Готовые запасы' if stock >= forecast and forecast > EPS else 'Нет обеспеченного спроса' if forecast <= EPS else 'План'
        if e['expected_margin'] < -EPS:
            target, e['production_limit'] = 0, 'Отрицательная ожидаемая маржа'
        e['planned_output'] = target
        e['input_target_output'] = target
        return target

    def update_prices(self):
        subjects = [(e, e.get('sales_history', []), e, 'price') for e in self.enterprises.values()]
        for a in self.accounts.values():
            if a['kind'] == 'household':
                continue
            firm = self.enterprises.get(a['id'])
            for good, quantity in a['stock'].items():
                if quantity <= EPS or (firm and (firm['good'] == good or firm['status'] != 'closed')):
                    continue
                prices = a.setdefault('ask_prices', {})
                price = prices.setdefault(good, self.goods[good]['price'])
                seller = dict(id=a['id'], good=good, price=price, tile_id=self.market_tile(a))
                subjects.append((seller, a.get('stock_sales_history', {}).get(good, []), prices, good))
        for e, observations, target, price_key in subjects:
            if not observations:
                continue
            last, price = observations[-1], e['price']
            limits = [limit for o, limit in self.customer_quotes(e, self.meta.get('public_orders', [])) if limit > 0]
            competitors = []
            for other in self.enterprises.values():
                if other['id'] == e['id'] or other['good'] != e['good'] or self.accounts[other['id']]['stock'].get(e['good'], 0) <= EPS:
                    continue
                quote = self.landed(other['tile_id'], e['tile_id'], e['good'], other['price'])
                if quote:
                    competitors.append(quote[0])
            if limits and last['sold'] > EPS and max(limits) > price+EPS:
                price = min(price*(1+self.rules['price_step']), max(limits))
            elif limits and max(limits) < price-EPS:
                price = max(price*(1-self.rules['price_step']), max(limits))
            elif last['sold'] <= EPS and competitors and min(competitors) < price-EPS:
                price = max(price*(1-self.rules['price_step']), min(competitors))
            elif last['sold'] <= EPS and competitors and min(competitors) <= price+EPS:
                a = self.accounts[e['id']]
                signal = self.market_signal(self.meta.get('market_history', []), a['state_id'], a['province_id'], e['good'])
                if signal['volume'] > EPS:
                    price *= 1-self.rules['price_step']
            else:
                budgets = []
                for o in self.meta.get('price_feedback', []):
                    if o['good'] != e['good'] or not o.get('unfunded_need'):
                        continue
                    quote = self.landed(e['tile_id'], o['tile_id'], e['good'], price)
                    if quote:
                        budgets.append(o['budget_price']-quote[1])
                declining = len(observations) > 1 and last['sold']+EPS < observations[-2]['sold']
                if budgets and max(budgets) > 0 and max(budgets) < price-EPS and (declining or last['sold'] <= EPS):
                    price = max(price*(1-self.rules['price_step']), max(budgets))
            target[price_key] = min(self.rules['maximum_price'], max(self.rules['minimum_price'], price))

    def household_requests(self, households, needs, purpose, sells):
        for a in households:
            requests = []
            for good, rate in needs.items():
                needed = self.population(a)*rate
                quantity = max(0, needed-a['stock'].get(good, 0))
                _, price, _ = self.quoted_cost(a, good, quantity, sells)
                requests.append((good, needed, quantity, price))
            total = sum(q*price for _, _, q, price in requests)
            fraction = min(1, self.free_cash(a['id'])/total) if total > EPS else 0
            for good, needed, quantity, price in requests:
                a.setdefault('needs', {})[good] = dict(needed=needed, consumed=0, deficit=needed)
                order = self.order(a['id'], good, quantity*fraction, 'buy', price, purpose)
                if order:
                    order.update(need_quantity=quantity, budget_price=price*fraction, unfunded_need=fraction < 1-EPS)

    def depth(self, good):
        inputs = self.goods[good]['inputs']
        return 1+max((self.depth(g) for g in inputs), default=-1)

    def input_requests(self, e, sells):
        target = self.plan(e, sells=sells)
        a, recipe = self.accounts[e['id']], self.goods[e['good']]
        cash = self.free_cash(e['id'])
        requested = target
        first_order = len(self.report['orders'])

        def cash_needed(quantity):
            needed = quantity*recipe['labor']*self.cfg['wage']
            for good, ratio in recipe['inputs'].items():
                missing = max(0, quantity*ratio-self.free_stock(e['id'], good))
                needed += missing*self.quoted_cost(a, good, missing, sells)[1]
            return needed

        # Оплаченное ранее сырьё влияет на маржу, но не требует второй оплаты из кассы.
        if cash_needed(target) > cash:
            low, high = 0.0, target
            for _ in range(40):
                middle = (low+high)/2
                if cash_needed(middle) <= cash:
                    low = middle
                else:
                    high = middle
            target = low
        # Единственный окончательный план: выпуск не сможет увеличить его после закупок.
        e['planned_output'] = e['input_target_output'] = target
        if target+EPS < requested:
            e['production_limit'] = 'Деньги на выпуск'
        e['production_plan'] = dict(month=self.meta['month'], output=target,
            requested_output=requested, free_cash=cash, known_order_count=first_order,
            reachable_demand=e['reachable_demand'], sales_forecast=e['sales_forecast'],
            expected_margin=e['expected_margin'], reason=e['production_limit'])
        a['payroll_protected'] = target*recipe['labor']*self.cfg['wage']
        a['protected'] += a['payroll_protected']
        for good, ratio in recipe['inputs'].items():
            quantity = max(0, target*ratio-self.free_stock(e['id'], good))
            _, price, _ = self.quoted_cost(a, good, quantity, sells)
            self.order(e['id'], good, quantity, 'buy', price, 'Сырьё')
        e['production_plan']['input_orders'] = [o['id'] for o in self.report['orders'][first_order:]]

    def project_candidate(self, investor, sells):
        candidates = []
        history = self.meta.get('market_history', [])
        markets = {(o['province_id'], o['good']) for o in self.report['orders'] if o['side'] == 'buy' and o['state_id'] == investor['state_id'] and o['province_id']}
        markets |= {(r['province_id'], r['good']) for h in history for r in h['markets'] if r['state_id'] == investor['state_id'] and r['province_id']}
        expansion = self.enterprises.get(investor['id'])
        for pid, good in sorted(markets):
            if expansion and (good != expansion['good'] or pid != expansion['province_id']):
                continue
            loc = {k: expansion[k] for k in ('state_id', 'province_id', 'city_id', 'tile_id')} if expansion else self.location(pid)
            if loc['state_id'] != investor['state_id']:
                continue
            recipe = self.goods[good]
            if recipe['resource']:
                tiles = [t for t in self.tiles.values() if t['province_id'] == pid and recipe['resource'] in t['resources']]
                if not tiles:
                    continue
                loc.update(tile_id=expansion['tile_id'] if expansion else self.shuffled(tiles)[0]['id'], city_id=None)
            quote_firm = dict(id=investor['id'], good=good, price=expansion['price'] if expansion else self.goods[good]['price'], **loc)
            limits = [limit for _, limit in self.customer_quotes(quote_firm) if limit > 0]
            signal = self.market_signal(history, investor['state_id'], pid, good)
            price = max(limits) if limits else signal['price'] or self.goods[good]['price']
            quote_firm['price'] = price
            demand = max(0, self.reachable_demand(quote_firm, sells)-investor['stock'].get(good, 0))
            if expansion:
                demand = max(0, demand-expansion['capacity'])
            if demand < self.cfg['minimum_capacity']:
                continue
            capacity = demand
            labor = sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                if a['kind'] == 'household' and a['state_id'] == loc['state_id'] and a['province_id'] == pid)
            capacity = min(capacity, labor/recipe['labor'])
            if recipe['resource']:
                used = sum(e['capacity'] for e in self.enterprises.values() if e['status'] != 'closed' and e['province_id'] == pid and self.goods[e['good']]['resource'] == recipe['resource'])
                capacity = min(capacity, max(0, len(tiles)*self.cfg['extraction_per_tile']-used))
            if capacity < self.cfg['minimum_capacity']:
                continue
            unit = self.unit_cost(quote_firm, sells)
            margin = self.net_price(quote_firm)-unit
            if margin <= EPS:
                continue
            distance, _ = self.path(self.market_tile(investor), loc['tile_id'])
            if distance is None:
                continue
            bill = self.construction_bill(1)
            material = sum(q*self.quoted_cost(investor, g, q, sells)[0]/max(q, EPS) for g, q in bill.items())
            delivery = sum(q*self.goods[g]['weight'] for g, q in bill.items())*distance*self.cfg['transport_rate']
            available = self.free_cash(investor['id'])
            startup = 0 if expansion else self.cfg['startup_cash']
            capacity = min(capacity, max(0, available-startup)/max(material+delivery+unit, EPS))
            if capacity < self.cfg['minimum_capacity']:
                continue
            working = 0 if expansion else max(startup, capacity*unit)
            investment = capacity*(material+delivery)+working
            profit = capacity*margin
            self.profile(investor)
            horizon = self.cfg['investment_payback_months']*(0.5+investor['risk_tolerance'])
            if investment > available+EPS or profit*horizon < investment:
                continue
            candidates.append(dict(good=good, capacity=capacity, location=loc, materials={},
                working_cash=working, expected_price=price, expected_profit=profit,
                expected_investment=investment, expected_unit_cost=unit, expansion=bool(expansion),
                created=self.meta['month']))
        return max(candidates, key=lambda p: p['expected_profit']*self.cfg['investment_payback_months']-p['expected_investment']) if candidates else None

    def project_requests(self, sells):
        due = self.meta['month'] % self.cfg['investment_interval'] == 0
        investors = [a for a in self.accounts.values() if a['kind'] in ('private', 'merchant', 'treasury', 'municipal') or
            (a['kind'] == 'enterprise' and self.enterprises[a['id']]['status'] != 'closed' and self.enterprises[a['id']]['available_from'] <= self.meta['month'])]
        # Решения независимы; исполнение ещё не меняло ни одного предложения.
        choices = [(a, self.project_candidate(a, sells)) for a in self.shuffled(investors) if due and 'project' not in a]
        for a, project in choices:
            if project:
                a['project'] = project
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='started', **copy.deepcopy(project)))
        for a in self.shuffled(investors):
            project = a.get('project')
            if not project:
                continue
            good = project['good']
            loc = project['location']
            e = dict(id=a['id'], good=good, price=project['expected_price'], **loc)
            margin = self.net_price(e)-self.unit_cost(e, sells)
            # Отмена требует наблюдаемой отрицательной маржи, отсутствие одного материала не отменяет проект.
            signal = self.market_signal(self.meta.get('market_history', []), a['state_id'], loc['province_id'], good)
            absent = (len(self.meta.get('market_history', [])) >= 3 and self.meta['month']-project['created'] >= 3
                and signal['volume'] <= EPS and signal['unmet'] <= EPS and self.reachable_demand(e, sells) <= EPS)
            if due and (margin < -EPS or absent):
                del a['project']
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='cancelled', reason='Отрицательная маржа или устойчивое отсутствие спроса'))
                continue
            distance, _ = self.path(self.market_tile(a), loc['tile_id'])
            if distance is None:
                continue
            bill = self.construction_bill(project['capacity'])
            delivery = sum(q*self.goods[g]['weight'] for g, q in bill.items())*distance*self.cfg['transport_rate']
            project['delivery'] = delivery
            # Сначала склад инвестора, затем площадка: обе перевозки реальны и входят в оценку.
            a['protected'] = a.get('payroll_protected', 0)+project['working_cash']+delivery
            if a['protected']+a.get('reserved', 0) > a['balance']+EPS:
                a['protected'] = max(0, a['balance']-a.get('reserved', 0))
            for g, quantity in bill.items():
                held = project['materials'].get(g, 0)
                assign = min(max(0, quantity-held), max(0, a['stock'].get(g, 0)-held))
                release = max(0, assign-self.free_stock(a['id'], g))
                for offer in sells.get(g, []):
                    if offer['participant'] != a['id'] or offer['status'] in ('cancelled', 'expired'):
                        continue
                    take = min(release, offer['remaining'])
                    if take <= EPS:
                        continue
                    # С рынка снимается только закреплённая часть, свободный остаток остаётся в продаже.
                    offer['quantity'] -= take
                    offer['remaining'] -= take
                    offer['backing'] -= take
                    a['stock_reserved'][g] = max(0, a['stock_reserved'].get(g, 0)-take)
                    release -= take
                    if offer['remaining'] <= EPS:
                        self.cancel_order(offer, 'Материалы закреплены за собственной стройкой')
                assign = min(max(0, quantity-held), self.free_stock(a['id'], g))
                project['materials'][g] = held+assign
                missing = max(0, quantity-project['materials'][g])
                _, price, _ = self.quoted_cost(a, g, missing, sells)
                self.order(a['id'], g, missing, 'buy', price, 'Строительные материалы')

    def finish_projects(self):
        for a in list(self.accounts.values()):
            p = a.get('project')
            if not p:
                continue
            bill = self.construction_bill(p['capacity'])
            need_cash = p['working_cash']+p.get('delivery', 0)
            if any(p['materials'].get(g, 0)+EPS < q for g, q in bill.items()) or self.free_cash(a['id'])+a.get('protected', 0)+EPS < need_cash:
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='pending', **copy.deepcopy(p)))
                continue
            a.pop('project')
            a['protected'] = 0
            self.transfer(a['id'], 'carrier:'+a['state_id'], p.get('delivery', 0), 'Доставка строительных материалов на площадку')
            asset = p.get('delivery', 0)
            for g, q in bill.items():
                asset += self.use_stock(a['id'], g, q)
                self.report['construction_used'][g] = self.report['construction_used'].get(g, 0)+q
            if p['expansion']:
                e = self.enterprises[a['id']]
                e['capacity'] += p['capacity']
                e['asset_value'] += asset
                self.report['lifecycle'].append(dict(enterprise=e['id'], action='expanded', good=e['good'], capacity_added=p['capacity'], reason='Оплаченное выгодное расширение'))
            else:
                loc = p['location']
                key = (a['id'], loc['province_id'], loc['tile_id'], p['capacity'])
                self.construction_receipts[key] = dict(working_cash=p['working_cash'], materials=bill, asset_value=asset)
                e = self.open_enterprise(p['good'], loc, a['id'], p['capacity'], sales_estimate=p['capacity'])
                e['price'] = p['expected_price']
            self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='expanded' if p['expansion'] else 'built', enterprise=e['id']))

    def produce(self, existing, sells):
        labor, resources = defaultdict(float), defaultdict(float)
        for a in self.accounts.values():
            if a['kind'] == 'household':
                labor[a['state_id'], a['province_id']] += self.population(a)*self.cfg['labor_share']
        for t in self.tiles.values():
            for resource in t['resources']:
                resources[t['province_id'], resource] += self.cfg['extraction_per_tile']
        self.report['labor_available'] = [dict(state_id=s, province_id=p, workers=q) for (s, p), q in labor.items()]
        for e in self.shuffled(existing):
            if e['status'] == 'closed' or e.get('suspended') or e['available_from'] > self.meta['month']:
                continue
            plan = e.get('production_plan', {})
            planned = plan['output'] if plan.get('month') == self.meta['month'] else 0.0
            e['planned_output'] = planned
            recipe, a = self.goods[e['good']], self.accounts[e['id']]
            # Закупочные резервы уже сняты, проектный капитал остаётся защищён.
            project_hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a['protected'] = project_hold
            constraints = {'План': planned, 'Мощность': e['capacity'], 'Труд': labor[e['state_id'], e['province_id']]/recipe['labor'],
                'Деньги на зарплату': self.free_cash(e['id'])/(recipe['labor']*self.cfg['wage'])}
            if recipe['resource']:
                constraints['Природный ресурс'] = resources[e['province_id'], recipe['resource']]
            for g, ratio in recipe['inputs'].items():
                constraints['Сырьё: '+g] = self.free_stock(e['id'], g)/ratio
            e['production_observation'] = dict(month=self.meta['month'], constraints=dict(constraints),
                stocks_before=dict(a['stock']), free_cash_before=self.free_cash(e['id']),
                procurement_plan=copy.deepcopy(plan) if plan.get('month') == self.meta['month'] else None,
                expected_margin=e['expected_margin'], expected_unit_cost=e['expected_unit_cost'],
                expected_price=e['expected_price'])
            reason = min(constraints, key=constraints.get)
            q = max(0, constraints[reason])
            e['input_limited'] = any(k.startswith('Сырьё') and v+EPS < planned for k, v in constraints.items())
            if planned > EPS:
                e['production_limit'] = reason
            e['reason'] = e['production_limit']
            if q <= EPS:
                e['status'] = 'idle'
                continue
            workers = q*recipe['labor']
            labor[e['state_id'], e['province_id']] -= workers
            if recipe['resource']:
                resources[e['province_id'], recipe['resource']] -= q
            wages = workers*self.cfg['wage']
            self.distribute(e['id'], self.workforce(e), wages, 'Заработная плата')
            cost = wages
            for g, ratio in recipe['inputs'].items():
                cost += self.use_stock(e['id'], g, q*ratio)
                self.report['inputs_used'][g] = self.report['inputs_used'].get(g, 0)+q*ratio
            self.add_stock(e['id'], e['good'], q, cost)
            e.update(status='active', workers=workers, output=q, production_cost=cost, unit_cost=cost/q, wages=wages)
            self.report['production'][e['good']] = self.report['production'].get(e['good'], 0)+q

    def return_capital(self, e):
        a = self.accounts[e['id']]
        a['protected'] = 0
        self.transfer(e['id'], e['owner_id'], self.free_cash(e['id']), 'Возврат капитала закрытого предприятия')

    def close_enterprise(self, e, unpaid=0):
        for o in self.report['orders']:
            if o['participant'] == e['id'] and o['side'] == 'buy':
                self.cancel_order(o, 'Предприятие закрыто')
        self.accounts[e['id']].pop('project', None)
        e.update(status='closed', closed=self.meta['month'], closure_reason='Устойчивая отрицательная маржа без продаж', reason='Прекращение нежизнеспособной деятельности')
        self.return_capital(e)
        self.report['lifecycle'].append(dict(enterprise=e['id'], good=e['good'], action='closed', reason=e['reason']))

    def settle(self, enterprises, households):
        for e in enterprises:
            e['expenses'] = e['cost_of_sales']
            e['profit'] = e['revenue']-e['tax']-e['expenses']
            e['loss_months'] = e['loss_months']+1 if e['profit'] < -EPS else 0
            e['profitable_months'] = e['profitable_months']+1 if e['profit'] > EPS else 0
            sold = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == e['id'])
            if e['status'] == 'closed':
                self.return_capital(e)
                continue
            unviable = sold <= EPS and e.get('expected_margin', 0) < -EPS and e['available_from'] <= self.meta['month']
            e['unviable_months'] = e.get('unviable_months', 0)+1 if unviable else 0
            if e['unviable_months'] >= self.cfg['loss_months_to_close']:
                self.close_enterprise(e)
        for a in self.shuffled(self.accounts.values()):
            rate = {'carrier': self.cfg['carrier_distribution'], 'treasury': self.cfg['public_spending']}.get(a['kind'], 0)
            if rate:
                recipients = [h for h in households if h['state_id'] == a['state_id']]
                self.distribute(a['id'], recipients, min(a['balance']*rate, self.free_cash(a['id'])),
                    'Оплата общественных услуг' if a['kind'] == 'treasury' else 'Оплата транспортных услуг населению')

    def record_market(self):
        rows = defaultdict(lambda: dict(volume=0, trade_gold=0, freight=0, requested=0, unmet_solvent=0, limit_value=0, reserved_unmet=0))
        public = []
        for o in self.report['orders']:
            if o['side'] != 'buy' or o['status'] == 'cancelled':
                continue
            row = rows[o['state_id'], o['province_id'], o['good']]
            row['requested'] += o['quantity']
            row['limit_value'] += o['quantity']*o['price']
            row['unmet_solvent'] += min(o['remaining'], o['reserved']/o['price'])
            row['reserved_unmet'] += o['reserved']
            if o['remaining'] > EPS:
                public.append(copy.deepcopy(o))
            o['unmet_solvent'] = min(o['remaining'], o['reserved']/o['price'])
        for t in self.report['trades']:
            row = rows[t['buyer_state'], t['buyer_province'], t['good']]
            row['volume'] += t['quantity']
            row['trade_gold'] += t['gold']
            row['freight'] += t['freight']
        self.meta['market_history'] = (self.meta.get('market_history', [])+[dict(month=self.meta['month'], markets=[dict(state_id=s, province_id=p, good=g, **r) for (s, p, g), r in sorted(rows.items(), key=lambda x: tuple(v or '' for v in x[0]))])])[-3:]
        self.meta['public_orders'] = public
        self.meta['price_feedback'] = [copy.deepcopy(o) for o in self.report['orders']
            if o['side'] == 'buy' and o['status'] != 'cancelled' and o.get('unfunded_need')]

    def step(self):
        self.__dict__.pop('_planning_orders', None)
        self.activate_rules()
        self.accounts = dict(sorted(self.accounts.items()))
        self.enterprises = dict(sorted(self.enterprises.items()))
        self.meta['month'] += 1
        self.reset_report()
        self.report.update(rules_version=3, investment_decisions=[])
        self.sync_locations()
        before = self.total_stock()
        self.report['opening_balances'] = {a['id']: a['balance'] for a in self.accounts.values()}
        self.report['gold_before'] = self.gold()
        prior_cash = {a['id']: a['balance'] for a in self.accounts.values() if a['kind'] == 'household'}
        for a in self.accounts.values():
            hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a.update(reserved=0.0, protected=min(a['balance'], hold), payroll_protected=0.0, stock_reserved={})
        existing = list(self.enterprises.values())
        for e in existing:
            e.pop('production_observation', None)
            e.update(revenue=0, tax=0, expenses=0, profit=0, output=0, workers=0, wages=0, cost_of_sales=0,
                production_cost=0, input_limited=False, fixed_cost_due=0, fixed_cost_paid=0,
                dividends=0.0, working_reserve=0.0, project_reserve=0.0)
        self.update_prices()
        sells = self.sell_orders()
        households = self.shuffled(a for a in self.accounts.values() if a['kind'] == 'household')
        self.household_requests(households, self.cfg['basic_needs'], 'Базовые нужды', sells)
        self.household_requests(households, self.cfg['extra_needs'], 'Дополнительные нужды', sells)
        self.owner_requests(sells)
        # Строительные заявки реальны и обеспечены до расчёта потребностей в сырье.
        self.project_requests(sells)
        # Один проход от конечных товаров ко входам, без рекурсивного перепланирования.
        for e in sorted(self.shuffled(existing), key=lambda e: -self.depth(e['good'])):
            if e['status'] != 'closed' and not e.get('suspended') and e['available_from'] <= self.meta['month']:
                self.input_requests(e, sells)
        buys = [o for o in self.report['orders'] if o['side'] == 'buy']
        for buy in self.shuffled(buys):
            self.match(buy, sells)
        self.record_market()
        self._planning_orders = copy.deepcopy(self.report['orders'])
        planning_sells = copy.deepcopy(sells)
        # Снимок спроса сделан до поздних доходов; остатки заявок больше не блокируют кассу.
        for o in self.report['orders']:
            if o['side'] == 'buy':
                o['reserved_at_clearing'] = o['reserved']
            execution_status, execution_reason = o['status'], o['reason']
            self.cancel_order(o, 'Окончание торгов месяца')
            if o['status'] == 'cancelled':
                o['status'] = 'expired'
                o['execution_status'] = execution_status
                o['reason'] = execution_reason or 'Окончание торгов месяца'
        self.produce(existing, planning_sells)
        self.finish_projects()
        self.consume_owner_purchases()
        for a in households:
            for good, need in a.get('needs', {}).items():
                consumed = min(need['needed'], self.free_stock(a['id'], good))
                self.use_stock(a['id'], good, consumed)
                need.update(consumed=consumed, deficit=need['needed']-consumed)
                self.report['consumed'][good] = self.report['consumed'].get(good, 0)+consumed
        for e in self.enterprises.values():
            offers = [o for o in self.report['orders'] if o['side'] == 'sell' and o['participant'] == e['id'] and o['good'] == e['good']]
            sold = sum(o['quantity']-o['remaining'] for o in offers)
            e['sales_history'] = (e.get('sales_history', [])+[dict(month=self.meta['month'], sold=sold,
                offered=sum(o['quantity'] for o in offers), output=e['output'], input_limited=e.get('input_limited', False),
                additional_demand=e.get('reachable_demand', 0) > EPS)])[-3:]
        for a in self.accounts.values():
            for good in a['stock']:
                history = a.setdefault('stock_sales_history', {}).get(good, [])
                sold = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == a['id'] and t['good'] == good)
                a['stock_sales_history'][good] = (history+[dict(sold=sold)])[-3:]
            hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a['protected'] = min(a['balance'], hold)
        self.settle(list(self.enterprises.values()), households)
        self.pay_dividends(planning_sells)
        for a in self.accounts.values():
            a['project_reserve'] = self.project_cash_reserve(a['id'], planning_sells)
        self.report['diagnostics'].extend(self.report['investment_decisions'])
        self.report['diagnostics'].extend(dict(kind='owner_consumption', **copy.deepcopy(row))
            for row in self.report['owner_consumption'])
        self.summarize(prior_cash)
        self.report['account_flows'] = []
        sectors = defaultdict(float)
        incoming_by_account, outgoing_by_account = defaultdict(float), defaultdict(float)
        for f in self.report['flows']:
            incoming_by_account[f['recipient']] += f['gold']
            outgoing_by_account[f['sender']] += f['gold']
        for a in self.accounts.values():
            incoming = incoming_by_account[a['id']]
            outgoing = outgoing_by_account[a['id']]
            self.report['account_flows'].append(dict(id=a['id'], kind=a['kind'], incoming=incoming, outgoing=outgoing, net=incoming-outgoing))
        for f in self.report['flows']:
            sectors[self.accounts[f['sender']]['kind'], self.accounts[f['recipient']]['kind']] += f['gold']
        self.report['sector_flows'] = [dict(sender_kind=s, recipient_kind=r, gold=q) for (s, r), q in sorted(sectors.items())]
        self.report['gold_after'] = self.gold()
        self.report['balances'] = {kind: sum(a['balance'] for a in self.accounts.values() if a['kind'] == kind) for kind in sorted({a['kind'] for a in self.accounts.values()})}
        total_gold = self.gold()
        self.report['money_shares'] = {kind: balance/total_gold if total_gold else 0
            for kind, balance in self.report['balances'].items()}
        self.report['diagnostics'].extend(dict(kind='money_share', sector=kind, balance=balance,
            share=self.report['money_shares'][kind]) for kind, balance in self.report['balances'].items())
        self.report['stocks'] = self.total_stock()
        self.report['active_enterprises'] = sum(e['status'] == 'active' for e in self.enterprises.values())
        self.report['open_enterprises'] = sum(e['status'] != 'closed' for e in self.enterprises.values())
        self.meta['rng_state'] = self.rng.getstate()
        self.validate(before)
        return self.report

    def validate(self, before=None):
        super().validate(before)
        for a in self.accounts.values():
            if any(not math.isfinite(a.get(k, 0)) or a.get(k, 0) < -EPS for k in ('reserved', 'protected')) or a.get('reserved', 0)+a.get('protected', 0) > a['balance']+EPS:
                raise ValueError('Нарушен резерв средств')
            for g in a.get('stock_reserved', {}).keys() | a.get('project', {}).get('materials', {}).keys():
                q = a.get('stock_reserved', {}).get(g, 0)
                if q < -EPS or q+self.locked_material(a['id'], g) > a['stock'].get(g, 0)+EPS:
                    raise ValueError('Нарушен резерв товаров')


def initialize(service):
    if not service.db.campaign():
        return False
    with service.db.transaction():
        if service.db.economy_records('EconomyMeta'):
            return False
        engine = Economy.load(service.db)
        engine.bootstrap()
        engine.save(service.db)
        event = service.db.record_event(service.event('economy_initialized',
            dict(gold=engine.gold(), enterprises=len(engine.enterprises), rules_version=3), [], source='economy', summary='Начальное состояние экономики V3'))
    service.notify(event)
    return True


def monthly_phase(service, ctx):
    engine = Economy.load(service.db)
    if not engine.accounts:
        engine.bootstrap()
    report = engine.step()
    engine.save(service.db)
    ctx['changes'].append(f'Экономика V3: {report["active_enterprises"]} предприятий, {len(report["trades"])} сделок')
    event = service.db.record_event(service.event('economy_month',
        dict(month=report['month'], invariants=report['invariants'], trades=len(report['trades']), rules_version=3), [],
        source='economy', action_id=ctx['command_id'], summary=ctx['changes'][-1]))
    ctx['events'].append(event)
    return len(engine.enterprises)
