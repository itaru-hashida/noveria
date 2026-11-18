"""Python-сторона границы месяца: публичные хуки поверх native-оркестрации.

Между Python API/modding и native-месяцем ровно один вызов —
``monthly_boundary.monthly_step``. Всё, что от месяца остаётся Python, собрано
здесь: сброс резервов месяца, защита кассы, резерв проектов, истечение заявок
и диагностика отчёта. Ядро (``monthly_driver``) вызывает эти хуки вместо
прямой записи в поля счетов и заявок, поэтому единственная граница
Python→Cython на месяц — сам ``monthly_step``, а обратных переходов ровно
столько, сколько хуков.

Границы, которые модуль соблюдает:

  * состояние не хранится: объект держит только ссылку на живой движок и не
    заводит второго ledger'а, копии счетов, склада или отчёта;
  * ``balance``/``reserved``/``protected`` меняются только через ``CashStore``,
    резерв склада — только через ``StockStore``, ``remaining``/``status``/
    ``price`` заявок — только через ``OrderStore``;
  * ``report``, поля-спутники ``payroll_protected``/``operating_protected``,
    ``project_reserve`` и тексты причин — это поля отчёта и представления, а
    не деньги; их пишет Python-хук;
  * порядок вызовов и порядок обхода задаёт ядро месяца: хук его не
    переставляет, RNG не трогает и не создаёт заявок/переводов сам.

Модуль не импортирует ``modules.economy.v3`` и ``modules.economy.support``:
движок приходит аргументом, EPS хукам не нужен, поэтому циклического импорта
нет.
"""


class MonthlyHooks(object):
    """Хуки месяца: побочные эффекты API-слоя, без собственного состояния.

    Ядро получает один объект на движок и вызывает методы в том же порядке,
    в каком прежний Python-код писал эти поля. Ни один метод не считает
    экономику — он только фиксирует уже посчитанный результат через
    существующие границы хранилищ.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    # --- пролог месяца ---------------------------------------------------
    def reset_month_reserves(self, accounts):
        """Сбросить резервы месяца: деньги — через CashStore, склад — через StockStore.

        Порядок на счёт прежний: посчитать ``hold``, обнулить денежный резерв,
        выставить защиту кассы, обнулить резерв склада. ``protected`` — такое
        же денежное поле счёта, как ``reserved``, поэтому его пишет тот же
        владелец: ``min(balance, hold)`` считается здесь, а значение уходит в
        колонку. ``balance`` читается из владельца, иначе политика опиралась бы
        на dict в обход него.
        """
        engine = self.engine
        cash = engine.cash_store
        for account in accounts.values():
            hold = account.get('project', {}).get('working_cash', 0)+account.get('project', {}).get('delivery', 0)
            cash.set_reserved_cash(account['id'], 0.0)
            cash.set_protected_cash(account['id'],
                min(cash.get_balance(account['id']), hold))
            account.update(payroll_protected=0.0, operating_protected=0.0)
            engine.stock_store.reset_reserved(account['id'])

    # --- производство и расчёты ------------------------------------------
    def protect_balances(self, accounts):
        """Пересчитать защиту кассы после закупок месяца."""
        engine = self.engine
        cash = engine.cash_store
        for account in accounts.values():
            hold = account.get('project', {}).get('working_cash', 0)+account.get('project', {}).get('delivery', 0)
            cash.set_protected_cash(account['id'],
                min(cash.get_balance(account['id']), hold))

    def apply_project_reserves(self, accounts, planning_sells):
        """Записать резерв под проекты: расчёт остаётся за ``project_cash_reserve``."""
        engine = self.engine
        for account in accounts.values():
            account['project_reserve'] = engine.project_cash_reserve(account['id'], planning_sells)

    # --- хвост рынка ------------------------------------------------------
    def expire_orders(self):
        """Истечь незакрытые заявки месяца, не забирая владение order state.

        ``status`` меняется только через ``OrderStore``: после ``cancel_order``
        отменённая заявка помечается ``expired`` хранилищем, а
        ``execution_status``/``reason`` — поля отчёта, их пишет хук. Снимок
        ``reserved_at_clearing`` фиксирует резерв до поздних доходов.
        """
        engine = self.engine
        store = engine.order_store
        for order in engine.report['orders']:
            if order['side'] == 'buy':
                order['reserved_at_clearing'] = order['reserved']
            execution_status, execution_reason = order['status'], order['reason']
            engine.cancel_order(order, 'Окончание торгов месяца')
            if order['status'] == 'cancelled':
                store.set_status(order, 'expired')
                store.set_metadata(order, 'execution_status', execution_status)
                store.set_metadata(
                    order, 'reason', execution_reason or 'Окончание торгов месяца')

    # --- отчёт ------------------------------------------------------------
    def extend_diagnostics(self, detail):
        """Дописать диагностику месяца теми же строками и в том же порядке."""
        if detail == 'none':
            return
        import copy
        engine = self.engine
        engine.report['diagnostics'].extend(engine.report['investment_decisions'])
        engine.report['diagnostics'].extend(dict(kind='owner_consumption', **copy.deepcopy(row))
            for row in engine.report['owner_consumption'])
