"""Шаг 4: сравнение Python project_candidate с native evaluate_project_candidate.

Обёртка перехватывает каждый вызов Economy.project_candidate в реальном
проходе project_requests, для одинакового packed state прогоняет Python и
native варианты и сравнивает поля, диагностику и RNG digest. Обёртка
возвращает результат Python-варианта, поэтому сам проход идёт ровно так же,
как без неё (RNG состояние восстанавливается).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402
import os  # noqa: E402
if os.environ.get('CANDIDATE_DEBUG_EXT'):
    from modules.economy.native import serial_step_dbg as serial_step  # noqa: E402
else:
    from modules.economy.native import serial_step  # noqa: E402
from modules.economy.native.update_prices_boundary import (  # noqa: E402
    pack_investment_state, build_candidate_support, build_candidate_dynamic,
)

STAGE_NAMES = {1: 'demand', 2: 'capacity', 3: 'margin', 4: 'route',
               5: 'funding_capacity', 6: 'payback'}
STAGE_KEYS = {
    'demand': ('demand', 'minimum'),
    'capacity': ('capacity', 'minimum'),
    'margin': ('margin', 'unit_cost', 'price'),
    'route': (),
    'funding_capacity': ('capacity', 'minimum'),
    'payback': ('investment', 'available', 'profit', 'horizon'),
}


class _LazySupplierRows:
    """Заполнить supplier-строки support вне `_ProjectBatch`.

    `build_candidate_support` теперь упаковывает только сырые поля фирм:
    `sup_price`/`sup_capacity` оставлены нулями, а значения для конкретного
    (товар, компонента) рынка считает Cython при первом запросе из
    `_ProjectBatch.ensure_suppliers`. В production это происходит внутри
    батча; здесь тот же контракт воспроизводится в Python-обёртке.

    Значения зависят только от фирмы (unit cost, net price, свободные
    деньги, труд и предел ресурса), а не от рынка-покупателя, поэтому один
    проход `ctx.suppliers(good)` закрывает все component-рынки товара.
    """

    def __init__(self, ctx, support, inv_meta):
        self.ctx = ctx
        self.support = support
        self.good_name = {i: g for g, i in inv_meta['good_index'].items()}
        self.account_name = {i: a for a, i in inv_meta['account_index'].items()}
        self._filled = set()
        self._values = {}
        # Native отбрасывает фирмы без собственных и владельческих денег
        # (`funds <= EPS`) независимо от all_goods, поэтому replica контекста
        # обязана вести себя так же.
        ctx.include_unfunded = False

    def _values_for(self, good_index):
        good = self.good_name[good_index]
        values = self._values.get(good)
        if values is None:
            values = {firm['id']: (price, capacity)
                      for firm, price, capacity in self.ctx.suppliers(good)}
            self._values[good] = values
        return values

    def ensure_suppliers(self, good, tile):
        support = self.support
        component = int(support['tile_component'][tile])
        key = int(good)*int(support['supplier_component_count'])+component
        if key in self._filled:
            return
        self._filled.add(key)
        values = self._values_for(good)
        start = int(support['sup_market_offset'][key])
        end = int(support['sup_market_offset'][key+1])
        for i in range(start, end):
            entry = values.get(self.account_name[int(support['sup_account'][i])])
            if entry is None:
                continue
            support['sup_price'][i] = entry[0]
            support['sup_capacity'][i] = entry[1]

    def touch_capacity(self, state, province, resource):
        """Не материализовать ledger: проход должен остаться Python-проходом.

        Production-батч касается `labor_reserved`/`resource_reserved`, создавая
        нулевые записи. Python-путь создаёт их сам ровно в тех же местах, а
        лишние нули меняют только длину плоских массивов, не значения, поэтому
        обёртка ledger не трогает и не сдвигает ход симуляции.
        """


def _native_diag_to_dicts(record, inv_meta):
    pid_name = {i: p for p, i in inv_meta['province_index'].items()}
    good_name = {i: g for g, i in inv_meta['good_index'].items()}
    out = []
    for pid, good, stage, v1, v2, v3, v4 in record['native']['diagnostics']:
        name = STAGE_NAMES[stage]
        keys = STAGE_KEYS[name]
        values = (v1, v2, v3, v4)[:len(keys)]
        row = dict(investor=record['investor_id'], good=good_name[good],
                   province_id=pid_name[pid], stage=name)
        row.update(dict(zip(keys, values)))
        out.append(row)
    return out


def _compare(record, engine):
    """Сравнить Python и native результаты. Вернуть (matched, first_diff)."""
    py = record['py']
    native = record['native']
    meta = record['meta']
    inv_meta = record['inv_meta']
    good_name = {i: g for g, i in inv_meta['good_index'].items()}
    tile_name = {i: t for t, i in meta['tile_index'].items()}
    pid_name = {i: p for p, i in inv_meta['province_index'].items()}
    state_name = {i: s for s, i in inv_meta['state_index'].items()}
    month = record['month']

    if (py is None) != (not native['candidate_present']):
        return False, 'candidate_present: py=%r native=%r' % (
            py is not None, native['candidate_present'])
    if py is None:
        # сравниваем диагностику (если Python её записывал) и RNG
        if record['diag_captured']:
            py_diag = record['py_diagnostics']
            native_diag = _native_diag_to_dicts(record, inv_meta)
            if py_diag != native_diag:
                return False, 'diagnostics: py=%r native=%r' % (py_diag, native_diag)
        if record['rng_after_py'] != record['rng_after_native']:
            return False, 'rng digest (no candidate)'
        return True, None

    checks = [
        ('good', py['good'], good_name[native['good']]),
        ('capacity', py['capacity'], native['capacity']),
        ('expected_price', py['expected_price'], native['expected_price']),
        ('expected_profit', py['expected_profit'], native['expected_profit']),
        ('expected_investment', py['expected_investment'], native['expected_investment']),
        ('expected_unit_cost', py['expected_unit_cost'], native['expected_unit_cost']),
        ('working_cash', py['working_cash'], native['working_cash']),
        ('location.tile_id', py['location']['tile_id'], tile_name[native['location_tile']]),
        ('location.province_id', py['location']['province_id'],
         pid_name[native['location_province']]),
        ('location.state_id', py['location']['state_id'],
         state_name[native['location_state']]),
        ('location.city_present', py['location'].get('city_id') is not None,
         native['city_present']),
        ('expansion', py['expansion'], native['expansion']),
        ('derived_demand', py['derived_demand'], native['derived_value']),
        ('dependency_expires', py['dependency_expires'], native['dependency_expires']),
        ('created', py['created'], native['created']),
        ('project_id', py['project_id'], '%s:%s:%s:%d' % (
            record['investor_id'], good_name[native['good']],
            pid_name[native['location_province']], month)),
        ('parent_project_id', py['parent_project_id'], record['parent_project_id']),
        ('chain_requirements', py['chain_requirements'],
         {good_name[g]: v for g, v in (native['chain'] or [])}),
        ('materials', py['materials'], {}),
    ]
    for name, py_v, native_v in checks:
        if isinstance(py_v, float) and isinstance(native_v, float):
            if py_v != native_v:  # точное побитовое сравнение
                return False, '%s: py=%r native=%r' % (name, py_v, native_v)
        elif py_v != native_v:
            return False, '%s: py=%r native=%r' % (name, py_v, native_v)
    if record['diag_captured']:
        py_diag = record['py_diagnostics']
        native_diag = _native_diag_to_dicts(record, inv_meta)
        if py_diag != native_diag:
            return False, 'diagnostics: py=%r native=%r' % (py_diag, native_diag)
    if record['rng_after_py'] != record['rng_after_native']:
        return False, 'rng digest'
    return True, None


def run_candidate_comparison(seed, months, extra_resource_calls=0):
    """Прогнать months месяцев, сравнивая Python и native на каждом вызове.

    extra_resource_calls: после прогона для первых N частных инвесторов
    выполнить синтетические вызовы project_candidate с derived_demand на
    ресурсные товары (resource candidate case) на том же packed state.
    """
    records = []
    state_cache = {}
    last_sells = {}
    fired_months = set()
    original = Economy.project_candidate

    def traced(self, investor, sells, **kwargs):
        last_sells['sells'] = sells
        derived = kwargs.get('derived_demand') or {}
        if len(derived) > 1:
            raise AssertionError('ожидался один derived good: %r' % derived)
        region = kwargs.get('demand_region')
        # packed state ДО Python-вызова (pack read-only)
        cache_key = (self.meta['month'], id(sells))
        if cache_key not in state_cache:
            inv_data, inv_meta = pack_investment_state(self, sells=sells)
            # Рыночный support теперь ленивый: в нём лежат сырые строки заявок
            # и одна матрица расстояний, поэтому любая пара (провинция, товар) —
            # включая синтетический derived-рынок — считается по требованию.
            # Готовой таблицы покрытия больше нет, и support собирается ровно
            # один раз на снимок sells.
            sup, smeta = build_candidate_support(self, sells, inv_data, inv_meta)
            state_cache[cache_key] = (inv_data, inv_meta, sup, smeta, {})
        inv_data0, inv_meta0, sup, smeta, lazy_state = state_cache[cache_key]
        # support сам по себе неизменяем: единственное, что Cython заполняет в
        # нём по ходу, — supplier-строки и рыночные кэши, поэтому между
        # вызовами одного прохода ведём только эти ложные кэши, не пересобирая
        # support.
        lazy_suppliers = lazy_state.get('suppliers')
        if lazy_suppliers is None:
            lazy_suppliers = _LazySupplierRows(smeta['ctx'], sup, inv_meta0)
            lazy_state['suppliers'] = lazy_suppliers
            # Тот же контракт, что и `native_project_requests`: Cython зовёт
            # `ensure_suppliers` из `_investment_demand_native` только когда
            # support несёт собственный батч.
            sup['batch'] = lazy_suppliers
        # inv_pack строится один раз на проход; между вызовами в проходе
        # меняются только ленивые профили (native читает их и ничего больше
        # из мутируемого); обновляем эти два массива из живых аккаунтов
        import numpy as _np
        inv_data = dict(inv_data0)
        live_accounts = list(self.accounts.values())
        inv_data['account_optimism'] = _np.float64(
            [a.get('optimism', _np.nan) for a in live_accounts])
        inv_data['account_risk_tolerance'] = _np.float64(
            [a.get('risk_tolerance', _np.nan) for a in live_accounts])
        inv_meta = inv_meta0
        account_idx = inv_meta['account_index'][investor['id']]
        dyn = build_candidate_dynamic(self, smeta, account_idx,
                                      kwargs.get('allowed_goods'))
        if derived:
            ((d_good, d_amount),) = derived.items()
            derived_good = inv_meta['good_index'][d_good]
        else:
            derived_good, d_amount = -1, 0.0
        region_idx = inv_meta['province_index'][region] if region else -1
        parent = kwargs.get('parent_project_id')
        # derived-пара вставляется в mk в строковом порядке Python
        if derived_good >= 0 and d_amount > 0.0 and region_idx >= 0:
            import numpy as _np
            pairs = list(zip(dyn['mk_province'].tolist(), dyn['mk_good'].tolist()))
            if (region_idx, derived_good) not in pairs:
                pid_name = {i: p for p, i in inv_meta['province_index'].items()}
                good_name = {i: g for g, i in inv_meta['good_index'].items()}
                pairs.append((region_idx, derived_good))
                pairs.sort(key=lambda pg: (pid_name[pg[0]], good_name[pg[1]]))
                dyn['mk_province'] = _np.int32([p for p, _ in pairs])
                dyn['mk_good'] = _np.int32([g for _, g in pairs])
                dyn['mk_count'] = len(pairs)

        rng_before = self.rng.getstate()
        diag_list = kwargs.get('diagnostics')
        diag_before = len(diag_list) if diag_list is not None else 0
        result_py = original(self, investor, sells, **kwargs)
        rng_after_py = self.rng.getstate()
        py_diag_slice = ([dict(d) for d in diag_list[diag_before:]]
                         if diag_list is not None else [])
        # снапшот кандидата: project_requests позже мутирует тот же dict
        result_snapshot = result_py
        if result_py is not None:
            result_snapshot = dict(result_py)
            result_snapshot['location'] = dict(result_py['location'])
            result_snapshot['materials'] = dict(result_py['materials'])
            result_snapshot['chain_requirements'] = dict(result_py['chain_requirements'])
        # откат RNG и прогон native на том же packed state
        self.rng.setstate(rng_before)
        result_native = serial_step.evaluate_project_candidate(
            inv_data, sup, dyn, account_idx, self.rng,
            derived_good, d_amount, region_idx, bool(parent))
        rng_after_native = self.rng.getstate()
        self.rng.setstate(rng_after_py)  # production-проход продолжается как в V3

        good_idx = inv_meta['good_index']
        tile_idx = smeta['tile_index']
        record = dict(
            seed=seed, month=self.meta['month'], investor_id=investor['id'],
            investor_kind=investor['kind'], py=result_snapshot, native=result_native,
            py_diagnostics=py_diag_slice,
            diag_captured=diag_list is not None,
            rng_after_py=rng_after_py, rng_after_native=rng_after_native,
            meta=dict(tile_index=tile_idx), inv_meta=inv_meta,
            parent_project_id=parent,
            expansion=result_native['expansion'],
            candidate_present=result_native['candidate_present'],
            good_resource=(inv_data['good_resource'][result_native['good']] >= 0
                           if result_native['candidate_present'] else False),
            stages={d[2] for d in result_native['diagnostics']},
            pj_nonzero=bool(len(dyn['pj_capacity'])),
            resource_market_seen=any(
                inv_data['good_resource'][g] >= 0 for g in dyn['mk_good']),
            resource_candidate_seen=any(
                inv_data['good_resource'][c[1]] >= 0
                for c in result_native['candidates']),
        )
        records.append(record)
        # синтетические resource-вызовы внутри due-прохода (index ещё жив),
        # с полным восстановлением RNG и ленивых профилей afterwards
        if extra_resource_calls and \
                self.meta['month'] % self.cfg['investment_interval'] == 0 \
                and self.meta['month'] not in fired_months:
            fired_months.add(self.meta['month'])
            rng_state = self.rng.getstate()
            saved_profiles = {}
            try:
                accounts = list(self.accounts.values())
                private_investors = [
                    a for a in accounts
                    if a['kind'] in ('private', 'merchant', 'treasury',
                                     'municipal')][:extra_resource_calls]
                resource_goods = [g for g, r in self.goods.items()
                                  if r['resource']]
                calls = []
                for account in private_investors:
                    for good in resource_goods:
                        resource = self.goods[good]['resource']
                        provinces = sorted({
                            t['province_id'] for t in self.tiles.values()
                            if t['province_id']
                            and self.owners.get(t['province_id']) == account['state_id']
                            and resource in t['resources']})
                        if not provinces:
                            continue
                        calls.append((account, good, provinces[0]))
                for account, good, province in calls:
                    saved_profiles.setdefault(
                        account['id'],
                        (account.get('optimism'), account.get('risk_tolerance')))
                    traced(self, account, sells,
                           derived_demand={good: 100.0},
                           demand_region=province)
            finally:
                self.rng.setstate(rng_state)
                for account_id, (opt, risk) in saved_profiles.items():
                    acc = self.accounts[account_id]
                    if opt is None:
                        acc.pop('optimism', None)
                    else:
                        acc['optimism'] = opt
                    if risk is None:
                        acc.pop('risk_tolerance', None)
                    else:
                        acc['risk_tolerance'] = risk
        return result_py

    Economy.project_candidate = traced
    try:
        tmp = workdir('candidate_compare')
        with tmp as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                for step in range(months):
                    service.advance_turn(command_id='cmp-%d-%d' % (seed, step),
                                         source='audit')
                engine = service.economy_state().engine
                results = []
                for record in records:
                    matched, first_diff = _compare(record, engine)
                    record['matched'] = matched
                    record['first_diff'] = first_diff
                    results.append(record)
                return results
            finally:
                db.close()
    finally:
        Economy.project_candidate = original
