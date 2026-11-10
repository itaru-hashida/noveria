"""Хранение состояния, начальный мир и общие операции экономики V3."""
import copy
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import networkx as nx

from modules.geography import topology

CONFIG_PATH = Path(__file__).resolve().parents[2] / 'content/economy.json'
EPS = 1e-7
PLANNING_DEFAULTS = dict(price_step=0.05, minimum_price=1e-6, maximum_price=1e9, initial_sales_fraction=0.5)
# Дороги обычно не меняются между месяцами, а WorldService заново загружает
# Economy. Кэш процесса переживает такую загрузку, но разделён отпечатком карты.
ROUTE_TREE_CACHE = {}


def tuples(value):
    return tuple(tuples(v) for v in value) if isinstance(value, (list, tuple)) else value


def _identity(value):
    """Заглушка вместо deepcopy, когда владение состоянием уже передано."""
    return value


class EconomyServices:
    def _initialize_state(self, snapshot, tiles, accounts=None, enterprises=None, meta=None,
                          copy_state=True):
        self.entities = {e['id']: e for e in snapshot['entities']}
        self.owners = {r['target']: r['source'] for r in snapshot['links'] if r['kind'] == 'owns'}
        self.tiles = {t['id']: t for t in tiles}
        self.graph = topology(tiles)
        self.paths = {}
        self._route_cache_key = tuple(sorted((tile['id'], tile.get('cost'),
            bool(tile.get('road')), bool(tile.get('passable', True)))
            for tile in self.tiles.values()))
        self._route_graph_shape = (self.graph.number_of_nodes(), self.graph.number_of_edges())
        self._route_shape_checked_month = None
        self._route_components = None
        self._route_component_count = None
        # Загрузка из БД получает свежие объекты (json.loads на каждую запись) и
        # забирает их во владение: повторное deepcopy только удваивало время.
        # Тесты и внешние вызовы, передающие собственные структуры, копируют.
        clone = copy.deepcopy if copy_state else _identity
        self.accounts = {a['id']: clone(a) for a in accounts or []}
        self.enterprises = {e['id']: clone(e) for e in enterprises or []}
        self.meta = clone(meta) if meta else dict(
            id='game', version=1, month=0, config=json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
        self.cfg = self.meta['config']
        self.goods = self.cfg['goods']
        self.reset_report()

    @classmethod
    def _load_state(cls, db):
        metadata = db.economy_records('EconomyMeta')
        # economy_records декодирует каждую запись заново, поэтому объекты уже
        # никому не принадлежат: движок забирает их без повторного deepcopy.
        return cls(db.snapshot(), db.tiles(), db.economy_records('EconomyAccount'),
                   db.economy_records('Enterprise'), metadata[0] if metadata else None,
                   copy_state=False)

    def reset_report(self):
        self.report = dict(id=str(self.meta['month']), month=self.meta['month'], orders=[], trades=[],
                           flows=[], lifecycle=[], diagnostics=[], production={}, consumed={},
                           inputs_used={}, equipment_wear={}, construction_used={}, provinces=[], prices={}, demography=[])

    def location(self, territory):
        e = self.entities[territory]
        if e['kind'] == 'Province':
            city = self.entities[e['properties']['capital_id']]
            return dict(state_id=self.owners[territory], province_id=territory,
                        city_id=None, tile_id=city['properties']['tile_id'])
        return dict(state_id=self.owners[territory], province_id=e['properties']['province_id'],
                    city_id=territory, tile_id=e['properties']['tile_id'])

    def account(self, identity, kind, balance, **location):
        self.accounts[identity] = dict(id=identity, kind=kind, balance=float(balance),
                                       stock={}, values={}, **location)
        return self.accounts[identity]

    def transfer(self, sender, recipient, amount, reason):
        if not math.isfinite(amount) or amount < -EPS:
            raise ValueError('Некорректный денежный перевод')
        if amount <= 0:
            return
        a, b = self.accounts[sender], self.accounts[recipient]
        if amount > a['balance'] + EPS:
            raise ValueError('Недостаточно золота для перевода')
        amount = min(amount, a['balance'])
        a['balance'] -= amount
        b['balance'] += amount
        self.report['flows'].append(dict(sender=sender, recipient=recipient, gold=amount, reason=reason))

    def add_stock(self, account, good, quantity, value=0):
        a = self.accounts[account]
        a['stock'][good] = a['stock'].get(good, 0) + quantity
        a['values'][good] = a['values'].get(good, 0) + value

    def use_stock(self, account, good, quantity):
        a = self.accounts[account]
        available = a['stock'].get(good, 0)
        if quantity < -EPS or quantity > available + EPS:
            raise ValueError('Двойное расходование товара')
        quantity = min(quantity, available)
        value = a['values'].get(good, 0) * quantity / available if available > 0 else 0
        a['stock'][good] = max(0, available - quantity)
        a['values'][good] = max(0, a['values'].get(good, 0) - value)
        return value

    def total_stock(self):
        return {g: sum(a['stock'].get(g, 0) for a in self.accounts.values()) for g in self.goods}

    def gold(self):
        return sum(a['balance'] for a in self.accounts.values())

    def _seed_state(self):
        if self.accounts:
            return
        populations = defaultdict(int)
        for e in self.entities.values():
            if e['kind'] != 'SocialGroup' or 'owner_id' not in e['properties']:
                continue
            p = e['properties']; loc = self.location(p['owner_id'])
            a = self.account(e['id'], 'household', p['population'] * self.cfg['household_gold_per_capita'], **loc)
            a['group_id'] = e['id']
            populations[loc['state_id']] += p['population']
        for state, population in populations.items():
            loc = dict(state_id=state, province_id=None, city_id=None,
                       tile_id=self.entities[self.entities[state]['properties']['capital_id']]['properties']['tile_id'])
            self.account('treasury:'+state, 'treasury', population * self.cfg['treasury_gold_per_capita'], **loc)
            self.account('merchant:'+state, 'merchant', population * self.cfg['owner_gold_per_capita'] / 2, **loc)
            self.account('private:'+state, 'private', population * self.cfg['owner_gold_per_capita'] / 2, **loc)
            self.account('carrier:'+state, 'carrier', 0, **loc)
        for e in self.entities.values():
            if e['kind'] == 'City':
                loc = self.location(e['id'])
                self.account('municipal:'+e['id'], 'municipal', 0, **loc)
                self.transfer('treasury:'+loc['state_id'], 'municipal:'+e['id'], self.cfg['startup_cash'] * self.cfg['seed_municipal_funding_factor'],
                              'Начальное финансирование администрации из центра')
        # Только начальное состояние содержит заданные фонды и товарные резервы.
        for pid in sorted(e['id'] for e in self.entities.values() if e['kind'] == 'Province'):
            land = [t for t in self.tiles.values() if t['province_id'] == pid]
            loc = self.location(pid)
            for good, recipe in self.goods.items():
                if not recipe['resource'] or not recipe.get('seed_initial', True):
                    continue
                candidates = [t for t in land if recipe['resource'] in t['resources']]
                if not candidates:
                    continue
                fraction = recipe.get('seed_share', 1)
                capacity = len(candidates) * self.cfg['extraction_per_tile'] * fraction
                tile = min(candidates, key=lambda t: (not t['road'], t['cost'], t['id']))
                owner = recipe.get('extraction_owner', 'private')+':'+loc['state_id']
                self.open_enterprise(good, dict(loc, tile_id=tile['id'], city_id=None), owner, capacity, initial=True)
        for city in sorted((e for e in self.entities.values() if e['kind'] == 'City'), key=lambda e: e['id']):
            loc = self.location(city['id']); pid = loc['province_id']
            population = sum(self.population(a) for a in self.accounts.values()
                             if a['kind'] == 'household' and a['province_id'] == pid)
            resources = {r for t in self.tiles.values() if t['province_id'] == pid for r in t['resources']}
            products = ['flour', 'bread']
            if 'лес' in resources:
                products.append('planks')
            if 'железо' in resources:
                products.extend(['iron', 'tools'])
            if 'зерно' in resources:
                products.append('cloth')
            cities = sum(e['kind'] == 'City' and e['properties']['province_id'] == pid for e in self.entities.values())
            for good in products:
                need = population * (self.cfg['basic_needs'].get(good, self.cfg['extra_needs'].get(good, self.cfg['seed_intermediate_need_per_capita']))) / cities
                capacity = max(self.cfg['minimum_capacity'], need * self.cfg['seed_processor_capacity_factor'])
                owner = 'municipal:'+city['id'] if good == 'bread' else 'merchant:'+loc['state_id']
                self.open_enterprise(good, loc, owner, capacity, initial=True)
        self.meta['initial_gold'] = self.gold()
        self.meta['initial_stock'] = self.total_stock()
        self.meta['initial_enterprises'] = len(self.enterprises)
        self.meta['initial_allocations'] = {a['id']: a['balance'] for a in self.accounts.values()}
        self.meta['initial_reserves'] = {a['id']: a['stock'] for a in copy.deepcopy(list(self.accounts.values())) if a['stock']}
        self.report['initialization'] = True
        self.validate()

    def population(self, account):
        return self.entities[account['group_id']]['properties']['population'] if account['kind'] == 'household' else 0

    def equipment_requirements(self, recipe, output):
        return {good: ratio*output for good, ratio in recipe.get('equipment', {}).items()}

    def equipment_wear(self, recipe, output):
        return {good: quantity*self.goods[good].get('durable', {}).get('wear_rate', 0)
                for good, quantity in self.equipment_requirements(recipe, output).items()}

    def workforce(self, loc):
        return [a for a in self.accounts.values() if a['kind'] == 'household'
                and a['state_id'] == loc['state_id'] and a['province_id'] == loc['province_id']]

    def _create_enterprise(self, good, loc, owner, capacity, initial=False):
        if self.accounts[owner]['state_id'] != loc['state_id']:
            raise ValueError('Иностранное владение запрещено')
        identity = f'firm-{len(self.enterprises)+1:05d}'
        a = self.account(identity, 'enterprise', 0, **loc)
        self.transfer(owner, identity, self.cfg['startup_cash'], 'Капитал предприятия')
        e = dict(id=identity, good=good, owner_id=owner, capacity=capacity, workers=0,
                 status='active', loss_months=0, profitable_months=0, opened=self.meta['month'],
                 closed=None, revenue=0, expenses=0, profit=0, output=0,
                 price=self.goods[good]['price'], reason='Начальное хозяйство' if initial else 'Спрос и ожидаемая окупаемость',
                 **loc)
        self.enterprises[identity] = e
        if initial:
            months = self.cfg['initial_reserve_months']
            self.add_stock(identity, good, capacity * months, capacity * months * e['price'])
            for g, ratio in self.goods[good]['inputs'].items():
                q = capacity * ratio * months
                self.add_stock(identity, g, q, q * self.goods[g]['price'])
            for g, q in self.equipment_requirements(self.goods[good], capacity).items():
                self.add_stock(identity, g, q, q * self.goods[g]['price'])
            for g, q in self.cfg['construction'].items():
                reserve=q*self.cfg['seed_construction_reserve_factor']
                self.add_stock(owner, g, reserve, reserve * self.goods[g]['price'])
        self.report['lifecycle'].append(dict(enterprise=identity, action='opened', good=good,
                                             **loc, reason=e['reason']))
        return e

    def path(self, start, end):
        # ``number_of_edges()`` NetworkX проходит весь граф. Проверять форму
        # на каждом расчёте доставленной цены превращало путь в O(E). Дороги
        # штатно инвалидируют кэш явно; эта страховка для редактора нужна лишь
        # раз в месяц.
        if self._route_shape_checked_month != self.meta.get('month'):
            if (self.graph.number_of_nodes(), self.graph.number_of_edges()) != self._route_graph_shape:
                self.invalidate_routes()
            self._route_shape_checked_month = self.meta.get('month')
        if start not in self.paths:
            trees = ROUTE_TREE_CACHE.setdefault(self._route_cache_key, {})
            tree = trees.get(start)
            if tree is None:
                tree = nx.single_source_dijkstra(self.graph, start, weight='weight') if start in self.graph else ({}, {})
                trees[start] = tree
            self.paths[start] = tree
        distances, paths = self.paths[start]
        return (distances.get(end), paths.get(end, []))

    def route_component(self, tile_id):
        """Числовой ID физически связной части транспортного графа."""
        if self._route_shape_checked_month != self.meta.get('month'):
            if (self.graph.number_of_nodes(), self.graph.number_of_edges()) != self._route_graph_shape:
                self.invalidate_routes()
            self._route_shape_checked_month = self.meta.get('month')
        if self._route_components is None:
            components = {}
            component_count = 0
            for component_id, nodes in enumerate(nx.connected_components(self.graph)):
                component_count = component_id+1
                for node in nodes:
                    components[node] = component_id
            self._route_components = components
            self._route_component_count = component_count
        return self._route_components.get(tile_id)

    def invalidate_routes(self):
        """Сбросить маршруты после изменения дороги или ручной правки графа."""
        self.paths = {}
        self._route_components = None
        self._route_component_count = None
        self.__dict__.pop('_route_order_components', None)
        self.__dict__.pop('_customer_quotes_cache', None)
        self.__dict__.pop('_market_books', None)
        # Изменённый вручную graph может не совпадать с данными tiles. Нельзя
        # записывать его дерево под ключом исходной карты.
        self._route_cache_key = ('runtime', id(self.graph), self.meta.get('month', 0))
        self._route_graph_shape = (self.graph.number_of_nodes(), self.graph.number_of_edges())
        self._route_shape_checked_month = self.meta.get('month')

    def distribute(self, sender, recipients, amount, reason):
        total = sum(self.population(a) for a in recipients)
        if total:
            amount = min(amount, self.accounts[sender]['balance'])
            for a in recipients:
                self.transfer(sender, a['id'], amount * self.population(a) / total, reason)

    def _validate_state(self, before=None):
        for a in self.accounts.values():
            if not math.isfinite(a['balance']) or a['balance'] < -EPS:
                raise ValueError('Нарушен денежный баланс: '+a['id'])
            if any(not math.isfinite(v) or v < -EPS for v in a['stock'].values()):
                raise ValueError('Нарушен товарный баланс: '+a['id'])
        if abs(self.gold() - self.meta['initial_gold']) > max(1e-5, self.gold()*1e-10):
            raise ValueError('Нарушено сохранение золота')
        hired = defaultdict(float)
        used_resources = defaultdict(float)
        for e in self.enterprises.values():
            if e['status'] == 'closed':
                continue
            hired[e['state_id'], e['province_id']] += e['workers']
            if self.accounts[e['owner_id']]['state_id'] != e['state_id']:
                raise ValueError('Иностранный собственник')
            resource = self.goods[e['good']]['resource']
            if resource:
                tile = self.tiles[e['tile_id']]
                if resource not in tile['resources'] or tile['province_id'] != e['province_id']:
                    raise ValueError('Производство без местного ресурса')
                used_resources[e['province_id'], resource] += e.get('output', 0)
        for (state, pid), count in hired.items():
            available = sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                            if a['kind']=='household' and a['state_id']==state and a['province_id']==pid)
            if count > available + EPS:
                raise ValueError('Двойной наём работников')
        for row in self.report.get('construction_labor', []):
            key = (row['state_id'], row['province_id'])
            hired[key] += row['workers']
            available = sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                            if a['kind']=='household' and a['state_id']==key[0] and a['province_id']==key[1])
            if hired[key] > available + EPS:
                raise ValueError('Двойной наём работников на стройку')
        for (pid, resource), quantity in used_resources.items():
            available = sum(self.cfg['extraction_per_tile'] for t in self.tiles.values()
                            if t['province_id']==pid and resource in t['resources'])
            if quantity > available + EPS:
                raise ValueError('Двойное использование природного ресурса')
        if before is not None:
            after = self.total_stock()
            for good in self.goods:
                expected = before[good]+self.report['production'].get(good,0)-self.report['consumed'].get(good,0)-self.report['inputs_used'].get(good,0)-self.report['equipment_wear'].get(good,0)-self.report['construction_used'].get(good,0)
                if abs(after[good]-expected) > max(1e-5, abs(expected)*1e-10):
                    raise ValueError('Нарушен баланс товара: '+good)
            balances=dict(self.report['opening_balances'])
            for flow in self.report['flows']:
                balances[flow['sender']]=balances.get(flow['sender'],0)-flow['gold']
                balances[flow['recipient']]=balances.get(flow['recipient'],0)+flow['gold']
            for identity,a in self.accounts.items():
                if abs(a['balance']-balances.get(identity,0))>1e-5:
                    raise ValueError('Проводки не совпадают со счётом: '+identity)
            for trade in self.report['trades']:
                path=trade['path']
                if not path or any(not self.graph.has_edge(a,b) for a,b in zip(path,path[1:])):
                    raise ValueError('Сделка без допустимого маршрута')
                distance=sum(self.graph[a][b]['weight'] for a,b in zip(path,path[1:]))
                freight=distance*self.goods[trade['good']]['weight']*self.cfg['transport_rate']*trade['quantity']
                if abs(freight-trade['freight'])>1e-5 or abs(trade['gold']-trade['price']*trade['quantity'])>1e-5:
                    raise ValueError('Неверный расчёт сделки или доставки')
        self.report['invariants'] = dict(gold=True, goods=True, nonnegative=True, labor=True, resources=True, ownership=True, ledger=True, logistics=True)

    def sync_locations(self):
        # Политическое владение и новые страты читаются из текущего мира каждый месяц.
        for e in self.entities.values():
            if e['kind']=='SocialGroup' and 'owner_id' in e['properties']:
                loc = self.location(e['properties']['owner_id'])
                if e['id'] not in self.accounts:
                    a = self.account(e['id'], 'household', 0, **loc)
                    a['group_id'] = e['id']
                self.accounts[e['id']].update(loc)
        for e in self.enterprises.values():
            state = self.owners[e['city_id'] or e['province_id']]
            if state != e['state_id']:
                # Передача территории не создаёт иностранную собственность: капитал остаётся прежнему владельцу.
                self.report['diagnostics'].append(dict(enterprise=e['id'], reason='Смена государства: производство приостановлено до решения о собственности'))
                e['suspended'] = True
            else:
                e['suspended'] = False

    def market_summary_index(self):
        """Один индекс месяца вместо сканов заказов и сделок на каждую провинцию."""
        index = dict(orders=defaultdict(list), buyer_trades=defaultdict(list),
            seller_trades=defaultdict(list), province_trades=defaultdict(list),
            good_trades=defaultdict(list), households=defaultdict(list),
            firms=defaultdict(list), stocks=defaultdict(list))
        for order in self.report['orders']:
            index['orders'][order.get('province_id'), order.get('good'), order.get('side')].append(order)
        for trade in self.report['trades']:
            index['buyer_trades'][trade['buyer_province'], trade['good']].append(trade)
            index['seller_trades'][trade['seller_province'], trade['good']].append(trade)
            index['province_trades'][trade['buyer_province']].append(trade)
            if trade['seller_province'] != trade['buyer_province']:
                index['province_trades'][trade['seller_province']].append(trade)
            index['good_trades'][trade['good']].append(trade)
        for account in self.accounts.values():
            if account['kind'] == 'household':
                index['households'][account['province_id']].append(account)
            for good, quantity in account['stock'].items():
                index['stocks'][account['province_id'], good].append(quantity)
        for firm in self.enterprises.values():
            index['firms'][firm['province_id']].append(firm)
        return index

    def summarize(self, prior_cash):
        index = self.market_summary_index()
        for good in self.goods:
            trades = index['good_trades'][good]
            volume=sum(t['quantity'] for t in trades)
            self.report['prices'][good]=sum(t['gold'] for t in trades)/volume if volume else None
        for pid in sorted(e['id'] for e in self.entities.values() if e['kind']=='Province'):
            households = index['households'][pid]
            population=sum(self.population(a) for a in households)
            deficits={g:sum(a.get('needs',{}).get(g,{}).get('deficit',0) for a in households) for g in self.cfg['basic_needs']|self.cfg['extra_needs']}
            for a in households:
                basic=sum(a.get('needs',{}).get(g,{}).get('consumed',0) for g in self.cfg['basic_needs'])
                needed=sum(a.get('needs',{}).get(g,{}).get('needed',0) for g in self.cfg['basic_needs'])
                # Благосостояние — наблюдаемое покрытие базовой корзины, а не заданный тренд.
                a['welfare']=min(100,max(0,100*basic/needed)) if needed else 100
            imports=[t for t in index['province_trades'][pid] if t['buyer_province']==pid and t['seller_province']!=pid]
            exports=[t for t in index['province_trades'][pid] if t['seller_province']==pid and t['buyer_province']!=pid]
            firms = index['firms'][pid]
            demography = self.report.get('demography_by_province', {}).get(pid, {})
            self.report['provinces'].append(dict(province_id=pid,state_id=self.owners[pid],population=population,
                household_gold=sum(a['balance'] for a in households),
                gold_change=sum(a['balance']-prior_cash.get(a['id'],0) for a in households),
                gold_per_capita=sum(a['balance'] for a in households)/population if population else 0,
                welfare=sum(a['welfare']*self.population(a) for a in households)/population if population else 0,
                deficits=deficits, production={g:sum(e['output'] for e in firms if e['good']==g) for g in self.goods},
                consumption={g:sum(a.get('needs',{}).get(g,{}).get('consumed',0) for a in households) for g in self.goods},
                import_volume=sum(t['quantity'] for t in imports),export_volume=sum(t['quantity'] for t in exports),
                transport=sum(t['freight'] for t in index['province_trades'][pid] if t['buyer_province']==pid),
                prices={g:sum(t['gold']+t['freight'] for t in index['buyer_trades'][pid, g])/sum(t['quantity'] for t in index['buyer_trades'][pid, g]) if index['buyer_trades'][pid, g] else None for g in self.goods},
                population_before=demography.get('population_before', population),
                population_change=population-demography.get('population_before', population),
                births=demography.get('births', 0), deaths=demography.get('deaths', 0),
                baseline_deaths=demography.get('baseline_deaths', 0),
                hunger_deaths=demography.get('hunger_deaths', 0),
                migration_in=demography.get('migration_in', 0),
                migration_out=demography.get('migration_out', 0),
                food_stress=demography.get('food_stress', 0),
                market_food_shortfall=demography.get('market_food_shortfall', 0),
                protected_by_subsistence=demography.get('protected_by_subsistence', 0),
                reason='Изменение денег: зарплаты и доходы минус покупки и доставка; дефицит: остаток неисполненной корзины'))

    def save(self, db, include_month=None):
        # Игра по умолчанию none и не пишет EconomyMonth: это тяжёлая запись
        # полного отчёта месяца, нужная аудиту и лаборатории, а не ходу.
        if include_month is None:
            include_month = self.meta.get('report_detail', 'none') != 'none'
        for institution in getattr(self, 'pending_institutions', []):
            db.create_entity(institution['id'], 'Institution', institution['name'], institution['properties'])
            db.put_link(institution['id'], institution['properties']['state_id'], 'institution_of')
            db.put_link(institution['id'], institution['properties']['city_id'], 'located_in')
        self.pending_institutions = []
        db.store_economy('EconomyAccount',list(self.accounts.values()))
        db.store_economy('Enterprise',list(self.enterprises.values()))
        db.store_economy('EconomyMeta',[self.meta])
        if include_month:
            db.store_economy('EconomyMonth',[self.report])
        db.update_social_groups({a['group_id']: self.entities[a['group_id']]['properties']
            for a in self.accounts.values() if a['kind'] == 'household' and a['group_id'] in self.entities})

    def __init__(self, *args, **kwargs):
        self._initialize_state(*args, **kwargs)
        self.rules = dict(PLANNING_DEFAULTS, **self.cfg.get('mvp_reform', {}))
        self.seed = self.meta.get('rng_seed', 0)
        self.rng = random.Random(self.seed)
        if self.meta.get('rng_state') is not None:
            self.rng.setstate(tuples(self.meta['rng_state']))
        self.construction_receipts = {}
        self.pending_institutions = []

    @classmethod
    def load(cls, db):
        engine = cls._load_state(db)
        if 'rng_state' not in engine.meta:
            engine.seed = (db.campaign() or {}).get('seed', 0)
            engine.rng.seed(engine.seed)
        return engine

    def shuffled(self, values, key=lambda item: item['id']):
        # ID задаёт каноническую нумерацию, а не приоритет: очередь переставляет RNG.
        result = sorted(values, key=key)
        self.rng.shuffle(result)
        return result

    def bootstrap(self):
        self._seed_state()
        self.activate_rules()

    def profile(self, investor):
        if 'optimism' not in investor:
            investor['optimism'] = self.rng.uniform(0.7, 1.3)
        if 'risk_tolerance' not in investor:
            investor['risk_tolerance'] = self.rng.uniform(0.1, 0.9)

    def market_signal(self, history, state, province, good):
        """Средние показатели рынка по провинции и товару за всю историю.

        Функция чистая: результат зависит только от `history` и ключа. Внутри
        месяца история не меняется, а вызов идёт из циклов по фирмам и
        инвесторам — без кэша каждый вызов заново обходит все месяцы и все
        строки рынка (за прогон это десятки тысяч полных обходов).
        """
        cache = self.__dict__.get('_market_signal_cache')
        if cache is None:
            cache = self._market_signal_cache = {}
        key = (state, province, good)
        entry = cache.get(key)
        # Сверяем сам список истории, а не только ключ: месяц пересобирает
        # market_history новым объектом, и прежняя запись тогда недействительна.
        if entry is not None and entry[0] is history:
            return dict(entry[1])
        rows = [r for month in history for r in month['markets']
            if r['state_id'] == state and r['province_id'] == province and r['good'] == good]
        if rows:
            volume = sum(r['volume'] for r in rows)
            requested = sum(r['requested'] for r in rows)
            signal = dict(volume=volume/len(history), unmet=sum(r['unmet_solvent'] for r in rows)/len(history),
                price=sum(r['trade_gold'] for r in rows)/volume if volume else None,
                landed_price=sum(r['trade_gold']+r['freight'] for r in rows)/volume if volume else None,
                limit_price=sum(r['limit_value'] for r in rows)/requested if requested else None)
        else:
            signal = dict(volume=0, unmet=0, price=None, landed_price=None, limit_price=None)
        cache[key] = (history, signal)
        # Отдаём копию: вызывающий получает собственный словарь, как и без кэша.
        return dict(signal)

    def construction_bill(self, capacity):
        return {g: q*capacity/self.cfg['minimum_capacity'] for g, q in self.cfg['construction'].items()}

    def open_enterprise(self, good, loc, owner, capacity, initial=False, sales_estimate=None):
        if self.accounts[owner]['state_id'] != loc['state_id']:
            raise ValueError('Иностранное владение запрещено')
        key = (owner, loc['province_id'], loc['tile_id'], capacity)
        receipt = None if initial else self.construction_receipts.get(key)
        if not initial and receipt is None:
            raise ValueError('Сначала требуется оплаченное строительство')
        e = self._create_enterprise(good, loc, owner, capacity, initial)
        e.update(available_from=0 if initial else self.meta['month']+1,
            initial_sales_estimate=capacity*self.rules['initial_sales_fraction'] if sales_estimate is None else sales_estimate,
            sales_history=[])
        e['sales_forecast'] = e['initial_sales_estimate']
        if receipt:
            self.transfer(owner, e['id'], receipt['working_cash']-self.cfg['startup_cash'], 'Дополнительный оборотный капитал')
            e.update(status='building', asset_value=receipt['asset_value'], construction_materials=receipt['materials'],
                     asset_valuation_source='Оплаченные материалы и доставка')
            del self.construction_receipts[key]
        return e

    def validate(self, before=None):
        self._validate_state(before)
        labor, resources = defaultdict(float), defaultdict(float)
        for e in self.enterprises.values():
            if e['output'] > e['capacity']+EPS or e['output'] < -EPS:
                raise ValueError('Выпуск за пределами мощности')
            labor[e['state_id'], e['province_id']] += e['workers']
            resource = self.goods[e['good']]['resource']
            if resource:
                resources[e['province_id'], resource] += e['output']
        for (state, pid), used in labor.items():
            available = sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                if a['kind'] == 'household' and a['state_id'] == state and a['province_id'] == pid)
            if used > available+EPS:
                raise ValueError('Двойной наём, включая закрывшиеся предприятия')
        for (pid, resource), used in resources.items():
            available = sum(self.cfg['extraction_per_tile'] for t in self.tiles.values()
                if t['province_id'] == pid and resource in t['resources'])
            if used > available+EPS:
                raise ValueError('Двойной расход природного ресурса')

