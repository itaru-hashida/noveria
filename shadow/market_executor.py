"""MarketExecutor: единственный владелец ledger, резервов и порядка исполнения.

Живёт в родительском процессе отдельным потоком, работает параллельно пулу:
забирает заявки из FIFO по мере готовности предприятий. Деньги, запасы и
резервы меняются только здесь, worker-ы не трогают ничего.

Книги месяца (`books`):
  asks   — публичный спрос: домохозяйства, собственники, стройки. Изменяемые
           записи, исполнение уменьшает `remaining`.
  offers — публичное предложение: склады не-фирм плюс офферы, выставленные
           агентами в этом же месяце по FIFO.
"""
import queue
import threading

from modules.economy.support import EPS


class Ledger:
    """Деньги и запасы агентной модели. Меняется одной нитью, без блокировок.

    Деньги держим по **всем** счетам мира, не только по фирмам: агент платит
    домохозяйству или собственнику, и если этих счетов нет, золото исчезает.
    Запас — только по фирмам: агентная модель торгует складами предприятий.
    """

    def __init__(self, meta, arrays):
        self.meta = meta
        self.cash = dict(meta['balances'])
        # Склад ведётся по ВСЕМ счетам, а не только по фирмам: агент покупает и
        # у казны, и у муниципалитета, и у перевозчика. Если их склады не вести,
        # товар приходит покупателю, но не убывает у продавца — товар создаётся.
        self.stock = {account_id: {good: float(quantity) for good, quantity in stocks.items()
                                   if quantity > EPS}
                      for account_id, stocks in meta['all_stock'].items()}
        for row, firm in enumerate(meta['firms']):
            self.stock[firm] = {meta['goods'][column]: float(arrays['agent_stock'][row, column])
                                for column in range(meta['n_goods'])
                                if arrays['agent_stock'][row, column] > EPS}
        self.reserved_cash = {firm: 0.0 for firm in meta['firms']}
        self.reserved_stock = {firm: {} for firm in meta['firms']}
        self.flows = []
        self.trades = []

    def free_cash(self, firm):
        return max(0.0, self.cash[firm]-self.reserved_cash.get(firm, 0.0))

    def free_stock(self, firm, good):
        return max(0.0, self.stock.get(firm, {}).get(good, 0.0)
                   - self.reserved_stock.get(firm, {}).get(good, 0.0))

    def reserve_cash(self, firm, amount):
        self.reserved_cash[firm] = self.reserved_cash.get(firm, 0.0)+amount

    def release_cash(self, firm, amount):
        self.reserved_cash[firm] = max(0.0, self.reserved_cash.get(firm, 0.0)-amount)

    def reserve_stock(self, firm, good, quantity):
        self.reserved_stock.setdefault(firm, {})[good] = \
            self.reserved_stock.setdefault(firm, {}).get(good, 0.0)+quantity

    def release_stock(self, firm, good, quantity):
        self.reserved_stock.setdefault(firm, {})[good] = max(
            0.0, self.reserved_stock.setdefault(firm, {}).get(good, 0.0)-quantity)

    def pay(self, sender, recipient, amount, reason):
        if amount <= EPS:
            return 0.0
        amount = min(amount, self.cash[sender])
        self.cash[sender] -= amount
        self.cash[recipient] = self.cash.get(recipient, 0.0)+amount
        self.flows.append(dict(sender=sender, recipient=recipient, gold=amount, reason=reason))
        return amount

    def add_stock(self, firm, good, quantity):
        self.stock.setdefault(firm, {})[good] = self.stock.setdefault(firm, {}).get(good, 0.0)+quantity

    def take_stock(self, firm, good, quantity):
        stocks = self.stock.setdefault(firm, {})
        available = stocks.get(good, 0.0)
        taken = min(available, quantity)
        stocks[good] = available-taken
        if stocks[good] <= EPS:
            stocks.pop(good, None)
        return taken


class MarketExecutor(threading.Thread):
    """Последовательный исполнитель FIFO-очереди намерений."""

    def __init__(self, ledger, intents, books, meta, outcomes, weights, rate):
        super().__init__(name='MarketExecutor', daemon=True)
        self.ledger = ledger
        self.intents = intents
        self.books = books
        self.meta = meta
        self.outcomes = outcomes
        self.weights = weights        # товар → вес для расчёта доставки
        self.rate = rate
        self.phase_done = threading.Event()
        self.finished = threading.Event()
        self.sequence = 0
        # Книги собираются из публичных строк снимка заранее: исполнитель
        # меняет только remaining, сами строки — его собственность.
        self.books['asks'] = {}
        for row in meta['ask_rows']:
            self.books['asks'].setdefault(row['good_index'], []).append(dict(row))
        self.books['offers'] = {}
        for row in meta['offer_rows']:
            # Публичные офферы принадлежат счетам мира (склады, казна): продавец —
            # строковый id, а не номер агента. Флаг нужен, чтобы не спутать.
            entry = dict(row)
            entry['seller_kind'] = 'account'
            self.books['offers'].setdefault(row['good_index'], []).append(entry)
        self.books.setdefault('distance', {})
        for row in range(meta['n_tiles']):
            self.books['distance'][row] = {column: 0.0 for column in range(meta['n_tiles'])}

    def run(self):
        while not (self.phase_done.is_set() and self.intents.empty()):
            try:
                item = self.intents.get(timeout=0.05)      # long polling
            except queue.Empty:
                continue
            self.sequence += 1
            self._execute(item)
            self.intents.task_done()
        self.finished.set()

    def _execute(self, item):
        agent, side, good_index, quantity, price, purpose, planned, reason = item
        if side == 'sell':
            self._sell(agent, good_index, quantity, price, reason)
        else:
            self._buy(agent, good_index, quantity, price, reason)

    def _distance(self, from_tile, to_tile):
        return self.books['distance'][from_tile][to_tile]
    def _sell(self, agent, good_index, quantity, price, reason):
        """Продажа: сначала публичный спрос по доставленной цене, остаток — оффер."""
        good = self.meta['goods'][good_index]
        firm = self.meta['firms'][agent]
        available = min(quantity, self.ledger.free_stock(firm, good))
        if available <= EPS:
            self._outcome(agent, 'sell', good_index, quantity, 0.0, 0.0,
                          'Нет свободного запаса')
            return
        self.ledger.reserve_stock(firm, good, available)
        weight = self.weights[good_index]
        tile = self.meta['agent_tile_index'][agent]

        filled, gold = 0.0, 0.0
        for ask in self.books['asks'].get(good_index, ()):
            if available-filled <= EPS:
                break
            if ask['remaining'] <= EPS:
                continue
            freight = self._distance(tile, ask['tile_index'])*weight*self.rate
            landed = price+freight
            if landed > ask['price']+EPS:
                continue          # покупателю дороже его предела
            affordable = min(ask['remaining'], ask['reserved']/max(landed, EPS))
            take = min(available-filled, affordable)
            if take <= EPS:
                continue
            cost = take*landed
            ask['remaining'] -= take
            ask['reserved'] = max(0.0, ask['reserved']-cost)
            filled += take
            gold += cost
            self.ledger.release_stock(firm, good, take)
            self.ledger.take_stock(firm, good, take)
            buyer = ask['buyer']
            # Покупатель обязан получить товар на свой склад. Раньше он только
            # платил, а товар исчезал: это дало ложную убыль 3 493 хлеба.
            self.ledger.add_stock(buyer, good, take)
            self.ledger.pay(buyer, firm, cost, 'Продажа агента')
            self.ledger.trades.append(dict(buyer=buyer, seller=firm, good=good,
                                           quantity=take, price=landed,
                                           sequence=self.sequence))

        left = available-filled
        self.ledger.release_stock(firm, good, available-filled)
        if left > EPS:
            # Непроданный остаток выставляется офером: его увидят следующие.
            self.books['offers'].setdefault(good_index, []).append(
                dict(seller=agent, seller_kind='agent', tile=tile,
                     tile_index=tile, price=price, remaining=left))
        self.ledger.release_stock(firm, good, available)
        status = 'Исполнено' if filled+EPS >= available else 'Частично исполнено'
        if filled <= EPS:
            status = 'Выставлено без спроса'
        self._outcome(agent, 'sell', good_index, quantity, filled, gold, status)

    def _buy(self, agent, good_index, quantity, price, reason):
        """Покупка: деньги резервируются, затем исполнение по книге офферов."""
        good = self.meta['goods'][good_index]
        firm = self.meta['firms'][agent]
        budget = min(quantity*price, self.ledger.free_cash(firm))
        if budget <= EPS:
            self._outcome(agent, 'buy', good_index, quantity, 0.0, 0.0, 'Не хватило денег')
            return
        self.ledger.reserve_cash(firm, budget)

        filled, spent = 0.0, 0.0
        left = quantity
        for offer in self.books['offers'].get(good_index, ()):
            if left <= EPS:
                break
            if offer['remaining'] <= EPS:
                continue
            take = min(left, offer['remaining'])
            cost = take*offer['price']
            if cost > budget-spent+EPS:
                take = (budget-spent)/offer['price']
                cost = take*offer['price']
            if take <= EPS:
                break
            offer['remaining'] -= take
            left -= take
            filled += take
            spent += cost
            if offer.get('seller_kind') == 'account':
                # Публичный склад мира: товар обязан УБЫТЬ у продавца, иначе он
                # создаётся. Первый прогон дал +0,216 tools именно из-за этого.
                seller = offer['seller']
                self.ledger.take_stock(seller, good, take)
                self.ledger.pay(firm, seller, cost, 'Покупка сырья')
                self.ledger.trades.append(dict(buyer=firm, seller=seller,
                                               good=good, quantity=take,
                                               price=offer['price'],
                                               sequence=self.sequence))
                self.ledger.add_stock(firm, good, take)
                continue
            seller = self.meta['firms'][offer['seller']]
            self.ledger.release_stock(seller, good, take)
            self.ledger.take_stock(seller, good, take)
            self.ledger.pay(firm, seller, cost, 'Покупка сырья')
            self.ledger.add_stock(firm, good, take)
            self.ledger.trades.append(dict(buyer=firm, seller=seller, good=good,
                                           quantity=take, price=offer['price'],
                                           sequence=self.sequence))

        self.ledger.release_cash(firm, budget-spent)
        if filled <= EPS:
            status = 'Нет предложения'
        else:
            status = 'Исполнено' if filled+EPS >= quantity else 'Частично исполнено'
        self._outcome(agent, 'buy', good_index, quantity, filled, spent, status)

    def _outcome(self, agent, side, good_index, requested, filled, gold, reason):
        firm = self.meta['firms'][agent]
        good = self.meta['goods'][good_index]
        self.outcomes.put(dict(agent=firm, agent_index=agent, side=side, good=good,
                               requested=requested, filled=filled, gold=gold,
                               reason=reason, sequence=self.sequence,
                               cash_after=self.ledger.cash[firm],
                               stock_after=self.ledger.stock[firm].get(good, 0.0)))
