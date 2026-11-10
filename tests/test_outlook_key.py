"""Ключ кэша прогноза выпуска: чужая сделка не должна его сбрасывать.

Смысл правки: раньше ключ содержал глобальную версию книги и число всех
сделок месяца, поэтому расчёт хлебопекарни сбрасывался сделкой по рыбе.
Теперь ключ зависит только от товаров самого рецепта и от своих сделок.

Книга рынка (`sells`) передаётся одна на все вызовы, как в движке: её
идентичность — часть ключа, иначе каждый вызов создаёт новую и кэш не попадает.
"""
from test_economy_v3 import engine, firm  # noqa: F401


def cached(engine):
    """Сколько разных ключей лежит в кэше прогноза (растёт на промахе)."""
    return len(getattr(engine, '_production_outlook_cache', {}))


def test_foreign_good_trade_keeps_plan_cached(engine):
    bakery = firm(engine)
    sells = engine.sell_orders()
    engine.production_outlook(bakery, sells)
    assert cached(engine) == 1
    engine.mark_market_quotes_dirty('fish')
    engine.production_outlook(bakery, sells)
    assert cached(engine) == 1


def test_own_good_trade_recomputes(engine):
    bakery = firm(engine)
    sells = engine.sell_orders()
    engine.production_outlook(bakery, sells)
    engine.mark_market_quotes_dirty('bread')
    engine.production_outlook(bakery, sells)
    assert cached(engine) == 2


def test_own_trade_recomputes(engine):
    bakery = firm(engine)
    sells = engine.sell_orders()
    engine.production_outlook(bakery, sells)
    engine._trades_by_seller[bakery['id']] += 1
    engine.production_outlook(bakery, sells)
    assert cached(engine) == 2


def test_full_invalidation_recomputes(engine):
    bakery = firm(engine)
    sells = engine.sell_orders()
    engine.production_outlook(bakery, sells)
    engine.mark_market_quotes_dirty()
    engine.production_outlook(bakery, sells)
    assert cached(engine) == 2


def test_plan_does_not_change_on_foreign_trade(engine):
    """Главное: локальность ключа не меняет сам план выпуска."""
    bakery = firm(engine)
    sells = engine.sell_orders()
    plan = dict(engine.production_outlook(bakery, sells))
    engine.mark_market_quotes_dirty('fish')
    assert engine.production_outlook(bakery, sells) == plan
