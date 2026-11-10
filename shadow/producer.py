"""Производство агентной модели: выпуск по реально купленным входам.

Отдельный шаг после клиринга, а не внутри него. Порядок тот же, что в V3:
сначала торги, потом выпуск по тому, что реально лежит на складе. Поэтому
пекарня, которой не хватило муки, выпускает меньше, и это видно в отчёте.

Ограничения берутся физические, как в `Economy.produce`:

    план → мощность → труд провинции → природный ресурс → купленное сырьё

Зарплата платится рабочим-домохозяйствам провинции; сырьё и оборудование
списываются со склада по средней стоимости.
"""
from collections import defaultdict

from modules.economy.support import EPS


class Producer:
    """Один шаг выпуска на ledger. Меняет только его, никого больше."""

    def __init__(self, ledger, meta, arrays, wage, labor_share):
        self.ledger = ledger
        self.meta = meta
        self.arrays = arrays
        self.wage = wage
        self.labor_share = labor_share
        self.produced = defaultdict(float)
        self.consumed = defaultdict(float)
        self.rows = []

    def run(self, plans):
        """`plans`: agent → (output, reason). Возвращает отчёт по агентам."""
        goods = self.meta['goods']
        labor = defaultdict(float)
        # Доступный труд: доля населения провинции, как в V3.
        for province, population in self.meta['province_population'].items():
            labor[province] = population*self.labor_share
        resources = defaultdict(float)
        for province, resource, quantity in self.meta['resource_limits']:
            resources[province, resource] = quantity

        for agent, (planned, reason) in sorted(plans.items()):
            good = self.meta['agent_good_index'][agent]
            province = self.meta['agent_province_name'][agent]
            firm = self.meta['firms'][agent]
            stocks = self.ledger.stock.setdefault(firm, {})
            recipe_labor = float(self.arrays['good_labor'][good])
            if planned <= EPS:
                continue

            constraint = planned
            if planned > float(self.arrays['agent_capacity'][agent]):
                constraint = float(self.arrays['agent_capacity'][agent])
            if recipe_labor > EPS:
                constraint = min(constraint, labor[province]/recipe_labor)
            resource = self.meta['good_resource'][good]
            if resource:
                constraint = min(constraint, resources.get((province, resource), 0.0))
            for input_good in range(self.meta['n_goods']):
                ratio = float(self.arrays['good_inputs'][good, input_good])
                if ratio > EPS:
                    constraint = min(constraint, stocks.get(goods[input_good], 0.0)/ratio)
            for equip_good in range(self.meta['n_goods']):
                ratio = float(self.arrays['good_equip'][good, equip_good])
                if ratio > EPS:
                    constraint = min(constraint, stocks.get(goods[equip_good], 0.0)/ratio)
            output = max(0.0, constraint)
            if output <= EPS:
                self.rows.append(dict(agent=firm, good=goods[good], planned=planned,
                                      output=0.0, reason='Нет физической возможности: '
                                                         'не хватает входа или труда'))
                continue

            workers = output*recipe_labor
            if recipe_labor > EPS:
                labor[province] -= workers
            if resource:
                resources[province, resource] -= output
            wages = workers*self.wage
            self._pay_wages(firm, province, wages)
            for input_good in range(self.meta['n_goods']):
                ratio = float(self.arrays['good_inputs'][good, input_good])
                if ratio > EPS:
                    self.ledger.take_stock(firm, goods[input_good], output*ratio)
                    self.consumed[goods[input_good]] += output*ratio
            for equip_good in range(self.meta['n_goods']):
                ratio = float(self.arrays['good_equip'][good, equip_good])
                if ratio > EPS:
                    wear = output*ratio
                    self.ledger.take_stock(firm, goods[equip_good], wear)
                    self.consumed[goods[equip_good]] += wear
            self.ledger.add_stock(firm, goods[good], output)
            self.produced[goods[good]] += output
            self.rows.append(dict(agent=firm, good=goods[good], planned=planned,
                                  output=output, reason=reason))
        return self.rows

    def _pay_wages(self, firm, province, amount):
        """Зарплата уходит домохозяйствам провинции пропорционально населению."""
        households = self.meta['households_by_province'].get(province) or []
        if not households or amount <= EPS:
            return
        total = sum(row[1] for row in households)
        if total <= EPS:
            return
        for account_id, population in households:
            self.ledger.pay(firm, account_id, amount*population/total, 'Заработная плата')
