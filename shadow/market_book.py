"""Книга рынка поверх снимка: read-only представление для `plan()`.

Отдельный слой нужен, чтобы планировщик не знал про NumPy-раскладку и не мог
случайно записать в снимок. Всё, что открыто агенту, — эти методы.
"""
import numpy as np

from modules.economy.support import EPS


class MarketBook:
    """Публичный рынок одного месяца. Меняется только версией снимка."""

    def __init__(self, arrays, goods, tiles, provinces, n_goods, n_tiles, n_provinces):
        self.good_inputs = arrays['good_inputs']
        self.good_labor = arrays['good_labor']
        self.good_equip = arrays['good_equip']
        self.good_price = arrays['good_price']
        self.good_weight = arrays['good_weight']
        self.good_taxed = arrays['good_taxed']
        self.agent_good = arrays['agent_good']
        self.agent_tile = arrays['agent_tile']
        self.agent_province = arrays['agent_province']
        self.agent_capacity = arrays['agent_capacity']
        self.agent_price = arrays['agent_price']
        self.agent_stock = arrays['agent_stock']
        self.agent_balance = arrays['agent_balance']
        self.agent_budget = arrays['agent_budget']
        self.agent_history = arrays['agent_history']
        self.distance = arrays['distance']
        self.reachable = arrays['reachable']
        self.signal_volume = arrays['signal_volume']
        self.signal_unmet = arrays['signal_unmet']
        self.signal_landed = arrays['signal_landed']
        self.goods = goods
        self.tiles = tiles
        self.provinces = provinces
        self.n_goods, self.n_tiles, self.n_provinces = n_goods, n_tiles, n_provinces
        self.offers = self._group(arrays, 'offer', n_goods)
        self.asks = self._rows(arrays, 'ask')
        self.offers_by_good = {}
        for offer in self.offers:
            self.offers_by_good.setdefault(offer.good, []).append(offer)

    @staticmethod
    def _group(arrays, prefix, n_goods):
        rows = MarketBook._rows(arrays, prefix)
        return rows

    @staticmethod
    def _rows(arrays, prefix):
        count = len(arrays[prefix+'_price'])
        rows = []
        for index in range(count):
            rows.append(_Row(
                agent=int(arrays[prefix+'_agent'][index]) if prefix+'_agent' in arrays else -1,
                seller=int(arrays[prefix+'_seller'][index]) if prefix+'_seller' in arrays else -1,
                tile=int(arrays[prefix+'_tile'][index]),
                good=int(arrays[prefix+'_good'][index]),
                price=float(arrays[prefix+'_price'][index]),
                remaining=float(arrays[prefix+'_remaining'][index]),
                reserved=float(arrays[prefix+'_reserved'][index]) if prefix+'_reserved' in arrays else 0.0,
            ))
        return rows

    def reference_price(self, tile, good, rate):
        """Лучшая доставленная цена предложения, иначе исторический сигнал.

        Аналог `Economy.reference_price`: агент не знает, кто продавец, ему
        нужна только цена, которую он заплатит на своей площадке.
        """
        weight = self.good_weight[good]
        distance_row = self.distance[tile]
        best = None
        for offer in self.offers_by_good.get(good, ()):
            if offer.remaining <= EPS or not self.reachable[offer.tile, tile]:
                continue
            landed = offer.price+distance_row[offer.tile]*weight*rate
            if best is None or landed < best:
                best = landed
        if best is not None:
            return best
        province = self.tiles[tile]['province_id'] if isinstance(self.tiles[tile], dict) else None
        landed = self.signal_landed[good, 0]
        if landed is not None and landed >= 0:
            return float(landed)
        return float(self.good_price[good])


class _Row:
    __slots__ = ('agent', 'seller', 'tile', 'good', 'price', 'remaining', 'reserved')

    def __init__(self, agent, seller, tile, good, price, remaining, reserved):
        self.agent, self.seller, self.tile = agent, seller, tile
        self.good, self.price, self.remaining, self.reserved = good, price, remaining, reserved
