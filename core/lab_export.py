"""Потоковый экспорт зафиксированной аналитики; симуляция не вызывается."""
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
ARCHIVE_NAME = 'NOVERIA_ANALYSIS.zip'
ROOT = Path(__file__).resolve().parents[1]
COMMON = ('format_version', 'experiment_id', 'month', 'economy_month')

SCHEMAS = {
    'monthly.csv': COMMON + ('record_kind', 'good', 'active_enterprises', 'closed_enterprises',
        'opened', 'closures', 'profit_gold', 'sales_gold', 'domestic_trade_gold',
        'international_trade_gold', 'freight_gold', 'balances_by_kind_json', 'output_quantity',
        'capacity_quantity', 'sold_quantity', 'stock_quantity', 'consumed_quantity',
        'needed_quantity', 'unmet_quantity', 'domestic_quantity', 'international_quantity',
        'wages_gold', 'turnover_gold', 'pending_projects', 'unmet_solvent_quantity', 'consumption_tax_gold',
        'dividends_gold', 'working_reserve_gold', 'project_reserve_gold', 'owner_consumption_budget_gold',
        'owner_consumption_spent_gold', 'owner_consumption_unspent_gold', 'owner_unfilled_orders',
        'owner_consumption_tax_gold', 'owner_consumption_freight_gold', 'owner_consumed_by_good_json',
        'dividends_by_owner_kind_json', 'money_shares_json'),
    'industries.csv': COMMON + ('good', 'producers', 'active', 'closed', 'opened', 'closures',
        'capacity_quantity', 'output_quantity', 'utilization', 'sold_quantity', 'stock_quantity',
        'finished_stock_quantity', 'stock_value_gold', 'sales_gold', 'expenses_gold',
        'production_cost_gold', 'profit_gold', 'needed_quantity', 'consumed_quantity', 'unmet_quantity',
        'average_sale_price', 'average_landed_price', 'average_ask_price', 'freight_gold',
        'domestic_quantity', 'international_quantity'),
    'enterprises.csv': COMMON + ('enterprise_id', 'owner_id', 'state_id', 'province_id', 'city_id',
        'tile_id', 'good', 'status', 'opened_economy_month', 'closed_economy_month', 'reason',
        'closure_reason', 'capacity_quantity', 'workers', 'output_quantity', 'sold_quantity',
        'bought_quantity', 'sales_gold', 'expenses_gold', 'production_cost_gold', 'cost_of_sales_gold',
        'tax_gold', 'profit_gold', 'balance_gold', 'purchase_gold', 'next_offer_price', 'unit_cost_gold',
        'loss_months', 'profitable_months', 'stocks_by_good_json', 'stock_values_by_good_json',
        'wages_gold', 'expected_margin_gold', 'reachable_demand_quantity', 'planned_output_quantity',
        'production_limit', 'dividends_gold', 'working_reserve_gold', 'project_reserve_gold', 'source_json'),
    'provinces.csv': COMMON + ('record_kind', 'state_id', 'province_id', 'good', 'population',
        'household_balance_gold', 'enterprise_balance_gold', 'investor_balance_gold',
        'treasury_balance_gold', 'balances_by_kind_json', 'production_quantity', 'consumed_quantity',
        'needed_quantity', 'unmet_quantity', 'coverage', 'imports_quantity', 'exports_quantity',
        'enterprise_count', 'stock_quantity'),
    'market_summary.csv': COMMON + ('state_id', 'province_id', 'good', 'side', 'journal_available',
        'order_count', 'requested_quantity', 'filled_quantity', 'unfilled_quantity', 'average_limit_price',
        'trade_count', 'trade_quantity', 'trade_gold', 'average_sale_price', 'average_landed_price',
        'freight_gold', 'source'),
    'money_summary.csv': COMMON + ('participant_kind', 'account_count', 'opening_balance_gold',
        'closing_balance_gold', 'inflow_gold', 'outflow_gold', 'net_flow_gold', 'journal_available'),
    'market_orders.csv': COMMON + ('source_ordinal', 'order_id', 'participant', 'state_id', 'province_id',
        'tile_id', 'good', 'side', 'quantity', 'remaining', 'filled', 'price', 'backing', 'purpose',
        'status', 'reason', 'expires', 'creation_index', 'source_json'),
    'market_trades.csv': COMMON + ('source_ordinal', 'trade_id', 'source_id', 'execution_index',
        'buyer', 'seller', 'buy_order', 'sell_order', 'buyer_state', 'seller_state', 'buyer_province',
        'seller_province', 'good', 'quantity', 'price', 'gold', 'freight', 'distance', 'path_json', 'source_json'),
    'money_flows.csv': COMMON + ('source_ordinal', 'flow_id', 'source_id', 'execution_index',
        'sender', 'recipient', 'sender_kind', 'recipient_kind', 'gold', 'reason',
        'buy_order', 'sell_order', 'good', 'quantity', 'source_json'),
}

# Временные реляционные проекции создаются только из SQLite и хранятся на диске.
FIELDS = {
    'overview': 'active closed opened closures output stock sold revenue profit unmet consumed wages turnover pending_projects unmet_solvent consumption_tax dividends working_reserve project_reserve owner_consumption_budget owner_consumption_spent owner_consumption_unspent owner_unfilled_orders owner_consumption_tax owner_consumption_freight',
    'production': 'state_id province_id good producers active closed opened closures capacity output utilization sold revenue profit expenses production_cost stock finished_stock needed consumed unmet',
    'enterprises': 'id owner_id state_id province_id city_id tile_id good status opened closed reason capacity workers output sold bought revenue expenses production_cost cost_of_sales tax profit balance purchase_gold price unit_cost loss_months profitable_months',
    'accounts': 'id kind state_id province_id balance',
    'stocks': 'entity_id state_id province_id good quantity value',
    'geography': 'state_id province_id good population household_gold enterprise_gold treasury_gold investor_gold production consumed needed unmet coverage imports exports enterprises',
    'orders': 'id participant state_id province_id tile_id good side quantity remaining filled price backing purpose status reason expires creation_index',
    'trades': 'id execution_index buyer seller buy_order sell_order buyer_state seller_state buyer_province seller_province good quantity price gold freight distance',
    'flows': 'id execution_index sender recipient gold reason',
}


class ExportValidationError(ValueError):
    pass


def git_revision(root=ROOT):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True,
                                text=True, timeout=3, check=True)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class AnalysisExporter:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory/'analytics.sqlite'
        self.db = None
        self.meta = {}
        self.checks = []
        self.warnings = []

    def __enter__(self):
        self.db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('BEGIN')
        try:
            self.meta = json.loads(self.db.execute('SELECT data FROM metadata WHERE id=1').fetchone()[0])
            if self.meta.get('status') != 'completed':
                raise ValueError('Экспорт доступен только для завершённых экспериментов')
            self.counts = dict(self.db.execute('SELECT dataset, COUNT(*) FROM rows GROUP BY dataset'))
            for dataset, fields in FIELDS.items():
                columns = ','.join(f"json_extract(data,'$.{field}') AS {field}" for field in fields.split())
                self.db.execute(f'''CREATE TEMP TABLE {dataset} AS SELECT month, ordinal,
                    json_extract(data,'$.economy_month') AS economy_month,{columns},data AS raw
                    FROM main.rows WHERE dataset=?''', (dataset,))
                self.db.execute(f'CREATE INDEX temp.{dataset}_month ON {dataset}(month)')
            for dataset in ('accounts', 'orders', 'enterprises'):
                self.db.execute(f'CREATE INDEX temp.{dataset}_identity ON {dataset}(month,id)')
            self.db.execute('CREATE INDEX temp.trades_buy ON trades(month,buy_order)')
            self.db.execute('CREATE INDEX temp.trades_sell ON trades(month,sell_order)')
            self.db.execute('CREATE INDEX temp.stocks_location ON stocks(month,state_id,province_id,good)')
            self.db.execute('CREATE INDEX temp.production_location ON production(month,state_id,province_id,good)')
            self._views()
            return self
        except BaseException:
            self.db.close()
            raise

    def __exit__(self, *args):
        self.db.close()

    def _views(self):
        statements = '''
            CREATE TEMP VIEW account_totals AS SELECT month,kind,COUNT(*) AS account_count,
                CASE WHEN COUNT(balance)=COUNT(*) THEN SUM(balance) END AS balance FROM accounts GROUP BY month,kind;
            CREATE TEMP VIEW flow_sides AS
                SELECT f.month,a.kind,f.gold AS inflow,NULL AS outflow,1 AS incoming,0 AS outgoing FROM flows f
                    JOIN accounts a ON a.month=f.month AND a.id=f.recipient
                UNION ALL
                SELECT f.month,a.kind,NULL,f.gold,0,1 FROM flows f
                    JOIN accounts a ON a.month=f.month AND a.id=f.sender;
            CREATE TEMP VIEW flow_totals AS SELECT month,kind,
                CASE WHEN SUM(incoming)=0 THEN 0 WHEN COUNT(inflow)=SUM(incoming) THEN SUM(inflow) END AS inflow,
                CASE WHEN SUM(outgoing)=0 THEN 0 WHEN COUNT(outflow)=SUM(outgoing) THEN SUM(outflow) END AS outflow
                FROM flow_sides GROUP BY month,kind;
            CREATE TEMP VIEW trade_totals AS SELECT month,good,COUNT(*) AS trade_count,
                SUM(quantity) AS quantity,SUM(gold) AS gold,SUM(freight) AS freight,
                SUM(CASE WHEN buyer_state=seller_state THEN quantity ELSE 0 END) AS domestic,
                SUM(CASE WHEN buyer_state<>seller_state THEN quantity ELSE 0 END) AS international
                FROM trades GROUP BY month,good;
            CREATE TEMP VIEW industry_totals AS SELECT month,good,
                SUM(producers) AS producers,SUM(active) AS active,SUM(closed) AS closed,
                SUM(opened) AS opened,SUM(closures) AS closures,SUM(capacity) AS capacity,
                SUM(output) AS output,SUM(sold) AS sold,SUM(stock) AS stock,
                SUM(finished_stock) AS finished_stock,SUM(revenue) AS revenue,
                SUM(expenses) AS expenses,SUM(production_cost) AS production_cost,
                SUM(profit) AS profit,SUM(needed) AS needed,SUM(consumed) AS consumed,
                SUM(unmet) AS unmet FROM production GROUP BY month,good;
            CREATE TEMP VIEW order_market AS SELECT month,state_id,province_id,good,side,
                COUNT(*) AS order_count,SUM(quantity) AS requested,SUM(filled) AS filled,
                SUM(remaining) AS remaining,CASE WHEN COUNT(price)=COUNT(*) AND COUNT(quantity)=COUNT(*)
                THEN SUM(quantity*price)/NULLIF(SUM(quantity),0) END AS price
                FROM orders GROUP BY month,state_id,province_id,good,side;
            CREATE TEMP VIEW trade_market AS SELECT month,buyer_state AS state_id,
                buyer_province AS province_id,good,'buy' AS side,quantity,gold,freight FROM trades
                UNION ALL SELECT month,seller_state,seller_province,good,'sell',quantity,gold,freight FROM trades;
            CREATE TEMP VIEW trade_market_totals AS SELECT month,state_id,province_id,good,side,
                COUNT(*) AS trade_count,SUM(quantity) AS quantity,SUM(gold) AS gold,
                SUM(freight) AS freight FROM trade_market GROUP BY month,state_id,province_id,good,side;
        '''
        for statement in statements.split(';'):
            if statement.strip():
                if not any('VIEW '+name+' AS' in statement for name in ('flow_sides', 'trade_market')):
                    statement = statement.replace('CREATE TEMP VIEW ', 'CREATE TEMP TABLE ')
                if 'CREATE TEMP TABLE industry_totals AS' in statement:
                    for field in FIELDS['production'].split():
                        statement = statement.replace(f'SUM({field})',
                            f'CASE WHEN COUNT({field})=COUNT(*) THEN SUM({field}) END')
                if any('CREATE TEMP TABLE '+table+' AS' in statement for table in
                       ('trade_totals', 'order_market', 'trade_market_totals')):
                    for field in ('quantity', 'gold', 'freight', 'filled', 'remaining'):
                        statement = statement.replace(f'SUM({field})',
                            f'CASE WHEN COUNT({field})=COUNT(*) THEN SUM({field}) END')
                self.db.execute(statement)
        self.db.execute('''CREATE TEMP TABLE ask_totals AS SELECT month,good,
            CASE WHEN COUNT(price)=COUNT(*) AND COUNT(quantity)=COUNT(*)
            THEN SUM(quantity*price)/NULLIF(SUM(quantity),0) END AS price
            FROM orders WHERE side='sell' GROUP BY month,good''')
        self.db.execute('''CREATE TEMP TABLE stock_totals AS SELECT month,good,
            CASE WHEN COUNT(value)=COUNT(*) THEN SUM(value) END AS value
            FROM stocks GROUP BY month,good''')
        self.db.execute('''CREATE TEMP TABLE regional_accounts AS SELECT month,state_id,province_id,kind,
            CASE WHEN COUNT(balance)=COUNT(*) THEN SUM(balance) END AS balance
            FROM accounts GROUP BY month,state_id,province_id,kind''')
        self.db.execute('''CREATE TEMP TABLE stock_locations AS SELECT month,state_id,province_id,good,
            CASE WHEN COUNT(quantity)=COUNT(*) THEN SUM(quantity) END AS quantity
            FROM stocks GROUP BY month,state_id,province_id,good''')
        self.db.execute('''CREATE TEMP TABLE firm_locations AS SELECT month,state_id,province_id,good,
            CASE WHEN COUNT(output)=COUNT(*) THEN SUM(output) END AS output,
            CASE WHEN COUNT(profit)=COUNT(*) THEN SUM(profit) END AS profit,
            CASE WHEN COUNT(expenses)=COUNT(*) THEN SUM(expenses) END AS expenses
            FROM enterprises GROUP BY month,state_id,province_id,good''')
        for table, fields in [('account_totals', 'month,kind'), ('flow_totals', 'month,kind'),
            ('trade_totals', 'month,good'), ('industry_totals', 'month,good'),
            ('ask_totals', 'month,good'), ('stock_totals', 'month,good'),
            ('order_market', 'month,state_id,province_id,good,side'),
            ('trade_market_totals', 'month,state_id,province_id,good,side'),
            ('stock_locations', 'month,state_id,province_id,good'),
            ('firm_locations', 'month,state_id,province_id,good'),
            ('regional_accounts', 'month,state_id,province_id')]:
            self.db.execute(f'CREATE INDEX temp.{table}_key ON {table}({fields})')

    def rows(self, query, params=()):
        for row in self.db.execute(query, params):
            yield dict(row)

    def journal(self, dataset):
        return self.meta.get('detail') == 'full' or self.counts.get(dataset, 0) > 0

    def check(self, name, query, datasets=()):
        if any(not self.counts.get(d) for d in datasets):
            self.checks.append(dict(name=name, status='unavailable', reason='Нет исходного набора данных'))
            return
        cursor = self.db.execute(query)
        count, sample = 0, []
        for row in cursor:
            count += 1
            if len(sample) < 5:
                sample.append(dict(row))
        self.checks.append(dict(name=name, status='failed' if count else 'passed', failures=count, sample=sample))

    def validate(self):
        for dataset, fields in [('accounts', ('balance',)), ('orders', ('quantity', 'filled', 'remaining', 'price')),
            ('trades', ('quantity', 'gold', 'freight')), ('flows', ('gold',)), ('stocks', ('quantity', 'value'))]:
            condition = ' OR '.join(field+' IS NULL' for field in fields)
            missing = self.db.execute(f'SELECT COUNT(*) FROM {dataset} WHERE '+condition).fetchone()[0]
            if missing:
                self.warnings.append(f'{dataset}: в {missing} записях отсутствуют числовые поля; неизвестные значения не заменяются нулями.')
        self.check('month_coverage', '''SELECT month FROM overview GROUP BY month HAVING COUNT(*)<>1''', ('overview',))
        actual = [r[0] for r in self.db.execute('SELECT month FROM overview ORDER BY month')]
        expected = list(range(self.meta['completed']+1))
        self.checks.append(dict(name='completed_months', status='passed' if actual == expected else 'failed',
                               expected_count=len(expected), actual_count=len(actual)))
        self.check('account_ids', 'SELECT month,id FROM accounts GROUP BY month,id HAVING COUNT(*)<>1', ('accounts',))
        self.check('order_ids', 'SELECT month,id FROM orders GROUP BY month,id HAVING COUNT(*)<>1', ('orders',))
        self.check('nonnegative_balances', 'SELECT month,id,balance FROM accounts WHERE balance < -0.000001', ('accounts',))
        self.check('nonnegative_stocks', 'SELECT month,entity_id,good,quantity FROM stocks WHERE quantity < -0.000001', ('stocks',))
        for field in ('output', 'sold', 'stock', 'unmet', 'consumed', 'profit', 'revenue'):
            self.check('world_'+field, f'''SELECT o.month,o.{field} AS actual,SUM(p.{field}) AS expected
                FROM overview o JOIN production p ON p.month=o.month WHERE o.month>0
                GROUP BY o.month HAVING ABS(o.{field}-SUM(p.{field}))>0.000001+ABS(SUM(p.{field}))*0.000000001''', ('overview', 'production'))
        self.check('enterprise_balances', '''SELECT e.month,e.id,e.balance,a.balance AS expected FROM enterprises e
            JOIN accounts a ON a.month=e.month AND a.id=e.id
            WHERE ABS(e.balance-a.balance)>0.000001+ABS(a.balance)*0.000000001''', ('enterprises', 'accounts'))
        for field in ('output', 'profit', 'expenses'):
            self.check('firms_'+field, f'''SELECT p.month,p.state_id,p.province_id,p.good,p.{field},e.{field} AS expected
                FROM production p LEFT JOIN firm_locations e ON e.month=p.month AND e.state_id IS p.state_id
                AND e.province_id IS p.province_id AND e.good=p.good WHERE p.month>0 AND
                ABS(p.{field}-CASE WHEN e.month IS NULL THEN 0 ELSE e.{field} END)>
                0.000001+ABS(p.{field})*0.000000001''', ('production', 'enterprises'))
        self.check('stocks_by_location_and_good', '''SELECT p.month,p.state_id,p.province_id,p.good,p.stock,s.quantity
            FROM production p LEFT JOIN stock_locations s ON s.month=p.month AND s.state_id IS p.state_id
            AND s.province_id IS p.province_id AND s.good=p.good WHERE
            ABS(p.stock-CASE WHEN s.month IS NULL THEN 0 ELSE s.quantity END)>0.000001+ABS(p.stock)*0.000000001''', ('production', 'stocks'))
        self.check('province_population_not_duplicated', '''SELECT month,state_id,province_id FROM geography
            GROUP BY month,state_id,province_id HAVING MAX(population)<>MIN(population)
            OR MAX(household_gold)<>MIN(household_gold)''', ('geography',))
        self.check('household_balance_by_province', '''SELECT g.month,g.province_id,g.household_gold,
            a.balance AS expected FROM geography g JOIN
            (SELECT month,state_id,province_id,SUM(balance) AS balance FROM accounts
             WHERE kind='household' GROUP BY month,state_id,province_id) a
            ON a.month=g.month AND a.state_id IS g.state_id AND a.province_id IS g.province_id
            WHERE ABS(g.household_gold-a.balance)>0.000001+ABS(a.balance)*0.000000001''', ('geography', 'accounts'))
        self.check('order_quantities', '''SELECT month,id FROM orders WHERE
            quantity < 0 OR remaining < -0.000001 OR filled < -0.000001 OR
            ABS(quantity-remaining-filled)>0.000001+ABS(quantity)*0.000000001''', ('orders',))
        for side, link in [('buy', 'buy_order'), ('sell', 'sell_order')]:
            self.check(side+'_trade_links', f'''SELECT t.month,t.ordinal FROM trades t LEFT JOIN orders o
                ON o.month=t.month AND o.id=t.{link} WHERE o.id IS NULL OR o.side<>'{side}' OR o.good<>t.good
                OR o.participant<>t.{'buyer' if side == 'buy' else 'seller'}''', ('trades', 'orders'))
            self.check(side+'_filled_matches_trades', f'''SELECT o.month,o.id,o.filled,SUM(t.quantity) AS traded
                FROM orders o LEFT JOIN trades t ON t.month=o.month AND t.{link}=o.id WHERE o.side='{side}'
                GROUP BY o.month,o.id HAVING ABS(o.filled-COALESCE(SUM(t.quantity),0))>
                0.000001+ABS(o.filled)*0.000000001''', ('orders', 'trades'))
        self.check('trade_money', '''SELECT month,ordinal FROM trades WHERE quantity<0 OR freight<0 OR
            ABS(quantity*price-gold)>0.000001+ABS(gold)*0.000000001''', ('trades',))
        self.check('sales_by_good', '''SELECT p.month,p.good,p.sold,t.quantity FROM industry_totals p
            LEFT JOIN trade_totals t ON t.month=p.month AND t.good=p.good
            WHERE p.month>0 AND ABS(p.sold-CASE WHEN t.month IS NULL THEN 0 ELSE t.quantity END)>
            0.000001+ABS(p.sold)*0.000000001''', ('production', 'trades'))
        self.check('flow_participants', '''SELECT f.month,f.ordinal FROM flows f
            LEFT JOIN accounts s ON s.month=f.month AND s.id=f.sender
            LEFT JOIN accounts r ON r.month=f.month AND r.id=f.recipient
            WHERE s.id IS NULL OR r.id IS NULL OR f.gold<0''', ('flows', 'accounts'))
        self.check('money_conservation', '''SELECT month,SUM(balance) AS balance FROM accounts
            GROUP BY month HAVING ABS(SUM(balance)-(SELECT SUM(balance) FROM accounts WHERE month=0))>
            0.000001+ABS(SUM(balance))*0.000000001''', ('accounts',))
        self.check('money_by_kind', '''SELECT a.month,a.kind,a.balance,p.balance AS opening,
            f.inflow,f.outflow FROM account_totals a LEFT JOIN account_totals p
            ON p.month=a.month-1 AND p.kind=a.kind LEFT JOIN flow_totals f ON f.month=a.month AND f.kind=a.kind
            WHERE a.month>0 AND ABS(a.balance-COALESCE(p.balance,0)-COALESCE(f.inflow,0)+COALESCE(f.outflow,0))>
            0.000001+ABS(a.balance)*0.000000001''', ('accounts', 'flows'))
        if any(c['status'] == 'failed' for c in self.checks):
            failed = [c for c in self.checks if c['status'] == 'failed']
            raise ExportValidationError('Несогласованная аналитика: '+encoded(failed))
        self.warnings.append('Флаги инвариантов движка и предупреждения TurnService не сохранены в analytics.sqlite; доступны проверки экспортёра и исходные аномалии.')
        if not self.journal('orders'):
            self.warnings.append('Подробный журнал не сохранён; отсутствующие заявки, сделки, цены и потоки не восстановлены.')

    def industries(self):
        return self.rows('''SELECT p.*,p.capacity AS capacity_quantity,p.output AS output_quantity,
            p.sold AS sold_quantity,p.stock AS stock_quantity,p.finished_stock AS finished_stock_quantity,
            p.revenue AS sales_gold,p.expenses AS expenses_gold,p.production_cost AS production_cost_gold,
            p.profit AS profit_gold,p.needed AS needed_quantity,p.consumed AS consumed_quantity,p.unmet AS unmet_quantity,
            p.output/NULLIF(p.capacity,0) AS utilization,
            t.gold/NULLIF(t.quantity,0) AS average_sale_price,(t.gold+t.freight)/NULLIF(t.quantity,0) AS average_landed_price,
            t.freight AS freight_gold,t.domestic AS domestic_quantity,t.international AS international_quantity,
            a.price AS average_ask_price,s.value AS stock_value_gold
            FROM industry_totals p LEFT JOIN trade_totals t ON t.month=p.month AND t.good=p.good
            LEFT JOIN ask_totals a ON a.month=p.month AND a.good=p.good
            LEFT JOIN stock_totals s ON s.month=p.month AND s.good=p.good ORDER BY p.month,p.good''')

    def money(self):
        for row in self.rows('''SELECT a.month,a.kind AS participant_kind,a.account_count,
            p.balance AS opening_balance_gold,a.balance AS closing_balance_gold,
            f.inflow AS inflow_gold,f.outflow AS outflow_gold,f.kind AS flow_kind FROM account_totals a
            LEFT JOIN account_totals p ON p.month=a.month-1 AND p.kind=a.kind
            LEFT JOIN flow_totals f ON f.month=a.month AND f.kind=a.kind ORDER BY a.month,a.kind'''):
            available = self.journal('flows') and row['month'] > 0
            row['journal_available'] = available
            if available:
                # Полный журнал и отсутствие переводов данной стороны означают действительный ноль.
                if row['flow_kind'] is None:
                    row['inflow_gold'] = row['outflow_gold'] = 0
                if row['inflow_gold'] is not None and row['outflow_gold'] is not None:
                    row['net_flow_gold'] = row['inflow_gold']-row['outflow_gold']
            else:
                row['inflow_gold'] = row['outflow_gold'] = None
            yield row

    def monthly(self):
        for row in self.rows('SELECT * FROM overview ORDER BY month'):
            month = row['month']
            raw = json.loads(row['raw'])
            balances = dict(self.db.execute('SELECT kind,balance FROM account_totals WHERE month=? ORDER BY kind', (month,)))
            trades = dict(self.db.execute('''SELECT SUM(CASE WHEN buyer_state=seller_state THEN gold ELSE 0 END) AS domestic,
                SUM(CASE WHEN buyer_state<>seller_state THEN gold ELSE 0 END) AS international,
                SUM(freight) AS freight FROM trades WHERE month=?''', (month,)).fetchone())
            yield dict(month=month, economy_month=row['economy_month'], record_kind='world',
                active_enterprises=row['active'], closed_enterprises=row['closed'], opened=row['opened'],
                closures=row['closures'], profit_gold=row['profit'], sales_gold=row['revenue'],
                domestic_trade_gold=trades['domestic'], international_trade_gold=trades['international'],
                freight_gold=trades['freight'], balances_by_kind_json=balances or None,
                wages_gold=row['wages'], turnover_gold=row['turnover'], pending_projects=row['pending_projects'],
                unmet_solvent_quantity=row['unmet_solvent'], consumption_tax_gold=row['consumption_tax'],
                dividends_gold=row['dividends'], working_reserve_gold=row['working_reserve'],
                project_reserve_gold=row['project_reserve'], owner_consumption_budget_gold=row['owner_consumption_budget'],
                owner_consumption_spent_gold=row['owner_consumption_spent'],
                owner_consumption_unspent_gold=row['owner_consumption_unspent'],
                owner_unfilled_orders=row['owner_unfilled_orders'], owner_consumption_tax_gold=row['owner_consumption_tax'],
                owner_consumption_freight_gold=row['owner_consumption_freight'],
                owner_consumed_by_good_json=raw.get('owner_consumed'), money_shares_json=raw.get('money_shares'),
                dividends_by_owner_kind_json={kind: raw.get('dividends_'+kind)
                    for kind in ('private', 'merchant', 'treasury', 'municipal')}
                    if 'dividends' in raw else None)
        for row in self.industries():
            yield dict(row, record_kind='good')

    def enterprises(self):
        renames = dict(id='enterprise_id', opened='opened_economy_month', closed='closed_economy_month',
            capacity='capacity_quantity', output='output_quantity', sold='sold_quantity', bought='bought_quantity',
            revenue='sales_gold', expenses='expenses_gold', production_cost='production_cost_gold',
            cost_of_sales='cost_of_sales_gold', tax='tax_gold', profit='profit_gold', balance='balance_gold',
            price='next_offer_price', unit_cost='unit_cost_gold')
        for row in self.rows('SELECT * FROM enterprises ORDER BY month,id,ordinal'):
            raw = json.loads(row.pop('raw'))
            for old, new in renames.items():
                row[new] = row.pop(old)
            row['closure_reason'] = row['reason'] if row['status'] == 'closed' else None
            row.update(stocks_by_good_json=raw.get('stocks'), stock_values_by_good_json=raw.get('stock_values'), source_json=raw)
            row.update(wages_gold=raw.get('wages'), expected_margin_gold=raw.get('expected_margin'),
                reachable_demand_quantity=raw.get('reachable_demand'), planned_output_quantity=raw.get('planned_output'),
                production_limit=raw.get('production_limit'), dividends_gold=raw.get('dividends'),
                working_reserve_gold=raw.get('working_reserve'), project_reserve_gold=raw.get('project_reserve'))
            yield row

    def provinces(self):
        for region in self.rows('''SELECT month,state_id,province_id,MAX(population) AS population
            FROM geography WHERE province_id IS NOT NULL GROUP BY month,state_id,province_id ORDER BY month,state_id,province_id'''):
            balances = dict(self.db.execute('''SELECT kind,balance FROM regional_accounts
                WHERE month=? AND state_id IS ? AND province_id IS ? ORDER BY kind''',
                (region['month'], region['state_id'], region['province_id'])))
            region.update(record_kind='region', balances_by_kind_json=balances or None,
                household_balance_gold=balances.get('household'), enterprise_balance_gold=balances.get('enterprise'),
                treasury_balance_gold=balances.get('treasury'))
            investors = [balances[k] for k in ('private', 'merchant', 'municipal') if k in balances]
            region['investor_balance_gold'] = sum(investors) if investors and all(v is not None for v in investors) else None
            yield region
        yield from self.rows('''SELECT g.month,g.economy_month,g.state_id,g.province_id,g.good,'good' AS record_kind,
            g.production AS production_quantity,g.consumed AS consumed_quantity,g.needed AS needed_quantity,
            g.unmet AS unmet_quantity,g.coverage,g.imports AS imports_quantity,g.exports AS exports_quantity,
            g.enterprises AS enterprise_count,p.stock AS stock_quantity FROM geography g LEFT JOIN production p
            ON p.month=g.month AND p.state_id IS g.state_id AND p.province_id IS g.province_id AND p.good=g.good
            WHERE g.province_id IS NOT NULL ORDER BY g.month,g.state_id,g.province_id,g.good''')

    def market(self):
        for row in self.rows('''WITH keys AS (
            SELECT month,state_id,province_id,good,side FROM order_market UNION
            SELECT month,state_id,province_id,good,side FROM trade_market_totals UNION
            SELECT month,state_id,province_id,good,'sell' FROM production UNION
            SELECT month,state_id,province_id,good,'buy' FROM production)
            SELECT k.*,o.order_count,o.requested AS requested_quantity,o.filled AS filled_quantity,
            o.remaining AS unfilled_quantity,o.price AS average_limit_price,t.trade_count,
            t.quantity AS trade_quantity,t.gold AS trade_gold,t.freight AS freight_gold,
            t.gold/NULLIF(t.quantity,0) AS average_sale_price,
            (t.gold+t.freight)/NULLIF(t.quantity,0) AS average_landed_price,
            p.sold AS saved_sales,p.revenue AS saved_revenue
            FROM keys k LEFT JOIN order_market o ON o.month=k.month AND o.state_id IS k.state_id
             AND o.province_id IS k.province_id AND o.good=k.good AND o.side=k.side
            LEFT JOIN trade_market_totals t ON t.month=k.month AND t.state_id IS k.state_id
             AND t.province_id IS k.province_id AND t.good=k.good AND t.side=k.side
            LEFT JOIN production p ON p.month=k.month AND p.state_id IS k.state_id
             AND p.province_id IS k.province_id AND p.good=k.good
            ORDER BY k.month,k.state_id,k.province_id,k.good,k.side'''):
            available = self.journal('orders') and self.journal('trades') and row['month'] > 0
            row['journal_available'] = available
            row['source'] = 'orders_and_trades' if available else 'production_sales_only' if row['side'] == 'sell' else 'unavailable'
            if available:
                if row['order_count'] is None:
                    for key in ('order_count', 'requested_quantity', 'filled_quantity', 'unfilled_quantity'):
                        row[key] = 0
                if row['trade_count'] is None:
                    for key in ('trade_count', 'trade_quantity', 'trade_gold', 'freight_gold'):
                        row[key] = 0
            elif row['side'] == 'sell' and row['month'] > 0:
                row['trade_quantity'], row['trade_gold'] = row['saved_sales'], row['saved_revenue']
            yield row

    def raw_rows(self, dataset):
        for row in self.rows(f'SELECT * FROM {dataset} ORDER BY month,ordinal'):
            raw = json.loads(row.pop('raw'))
            row['source_ordinal'] = row['ordinal']
            row['source_json'] = raw
            if dataset == 'orders':
                row['order_id'] = row['id']
            elif dataset == 'trades':
                row['source_id'] = row['id']
                row['trade_id'] = f'{self.meta["id"]}:trade:{row["month"]}:{row["ordinal"]}'
                row['path_json'] = raw.get('path')
            else:
                row['source_id'] = row['id']
                row['flow_id'] = f'{self.meta["id"]}:flow:{row["month"]}:{row["ordinal"]}'
                row.update({key: raw.get(key) for key in ('buy_order', 'sell_order', 'good', 'quantity')})
                for role, identity in [('sender', row['sender']), ('recipient', row['recipient'])]:
                    account = self.db.execute('SELECT kind FROM accounts WHERE month=? AND id=?', (row['month'], identity)).fetchone()
                    row[role+'_kind'] = account[0] if account else None
            yield row

    def changes(self):
        lines = ['# Известные отличия экономических правил', '',
            f'Эксперимент: {self.meta["id"]}',
            f'Git commit запуска: {self.meta.get("git_commit") or "не сохранён"}', '',
            'Сведения берутся только из метаданных запуска. Текущее рабочее дерево не описывает исторический запуск.', '']
        changes = self.meta.get('rule_changes')
        if isinstance(changes, list) and changes:
            lines += ['Заявленные в метаданных изменения (не независимая проверка):', '']
            lines += ['- '+str(change) for change in changes]
        else:
            lines += ['Известные изменения правил в метаданных отсутствуют. Это не доказывает отсутствие изменений.',
                      'По одним хешам файлов нельзя восстановить содержание или экономический смысл различий.']
        if self.meta.get('control_git_commit'):
            lines += ['', 'Контрольный commit из метаданных: '+str(self.meta['control_git_commit'])]
        control, recorded = self.meta.get('control_git_commit'), self.meta.get('git_commit')
        if all(isinstance(ref, str) and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', ref)
               for ref in (control, recorded)):
            try:
                result = subprocess.run(['git', '-C', str(ROOT), 'diff', '--no-ext-diff',
                    control, recorded, '--', 'modules/economy/v3.py', 'modules/economy/support.py',
                    'modules/economy/simulation.py', 'content/economy.json'],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, check=True)
                lines += ['', 'Проверяемое различие файлов экономических правил между записанными commit:', '',
                          '```diff', result.stdout or '(различий этих файлов нет)', '```',
                          'Diff описывает файлы этих commit; незакоммиченные изменения исторического запуска по нему не восстановить.']
            except (OSError, subprocess.SubprocessError):
                lines += ['', 'Записанные commit недоступны в локальном Git; содержимое различий неизвестно.']
        return '\n'.join(lines)+'\n'

    def write_csv(self, archive, filename, rows):
        count = 0
        with archive.open(filename, 'w', force_zip64=True) as target:
            with io.TextIOWrapper(target, encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=SCHEMAS[filename], extrasaction='ignore')
                writer.writeheader()
                for original in rows:
                    row = dict(original, format_version=FORMAT_VERSION, experiment_id=self.meta['id'])
                    if row.get('economy_month') is None and self.meta.get('initial_month') is not None:
                        row['economy_month'] = self.meta['initial_month']+row['month']
                    for key, value in row.items():
                        if isinstance(value, float) and not math.isfinite(value):
                            raise ExportValidationError('Нечисловое значение: '+filename+'/'+key)
                    writer.writerow({key: encoded(value) if isinstance(value, (dict, list)) else
                                     str(value).lower() if isinstance(value, bool) else value
                                     for key, value in row.items()})
                    count += 1
        return count

    def write_diagnostics(self, archive):
        header = dict(format_version=FORMAT_VERSION, experiment_id=self.meta['id'],
            checks=self.checks, warnings=self.warnings, engine_invariants=None,
            engine_warnings=None, source_anomalies_available=bool(self.counts.get('diagnostics')))
        with archive.open('diagnostics.json', 'w', force_zip64=True) as target:
            target.write((encoded(header)[:-1]+',"anomalies":[').encode('utf-8'))
            first = True
            for (raw,) in self.db.execute("SELECT data FROM main.rows WHERE dataset='diagnostics' ORDER BY month,ordinal"):
                target.write(('' if first else ',').encode('utf-8'))
                target.write(encoded(json.loads(raw)).encode('utf-8'))
                first = False
            target.write(b']}')


def export_analysis(directory, full=False):
    directory = Path(directory).resolve()
    destination = directory/ARCHIVE_NAME
    with AnalysisExporter(directory) as exporter:
        exporter.validate()
        descriptor, temporary = tempfile.mkstemp(prefix='.noveria-export-', suffix='.zip', dir=directory)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                counts = {}
                for filename, rows in [('monthly.csv', exporter.monthly()), ('industries.csv', exporter.industries()),
                    ('enterprises.csv', exporter.enterprises()), ('provinces.csv', exporter.provinces()),
                    ('market_summary.csv', exporter.market()), ('money_summary.csv', exporter.money())]:
                    counts[filename] = exporter.write_csv(archive, filename, rows)
                if full:
                    for filename, dataset in [('market_orders.csv', 'orders'), ('market_trades.csv', 'trades'), ('money_flows.csv', 'flows')]:
                        counts[filename] = exporter.write_csv(archive, filename, exporter.raw_rows(dataset))
                exporter.write_diagnostics(archive)
                archive.writestr('changes.md', exporter.changes())
                manifest = dict(format='NOVERIA_ANALYSIS', format_version=FORMAT_VERSION,
                    experiment_id=exporter.meta['id'], name=exporter.meta.get('name'), seed=exporter.meta.get('seed'),
                    git_commit=exporter.meta.get('git_commit'), initial_world_hash=exporter.meta.get('initial_state_hash'),
                    months=exporter.meta.get('months'), completed_months=exporter.meta.get('completed'),
                    initial_economy_month=exporter.meta.get('initial_month'), parameters=exporter.meta.get('parameters'),
                    status=exporter.meta['status'], engine=exporter.meta.get('engine'),
                    mode='full' if full else 'standard', exported_at=datetime.now(timezone.utc).isoformat(),
                    exporter_git_commit=git_revision(), source='analytics.sqlite', source_datasets=exporter.counts,
                    journal_available={d: exporter.journal(d) for d in ('orders', 'trades', 'flows')},
                    csv_columns={name: SCHEMAS[name] for name in counts}, csv_row_counts=counts,
                    null_policy='CSV empty cell / JSON null = unavailable; structural zero only for a complete journal',
                    market_definition='Order location; trades attributed separately to buyer and seller. Do not sum both sides.',
                    quantity_policy='Quantities are grouped by good. World and regional balance rows have no commodity quantities.',
                    month_zero='Initial snapshot; not a simulated month.', validation='passed', warnings=exporter.warnings)
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    error_file = directory/'NOVERIA_ANALYSIS.error.json'
    if error_file.exists():
        try:
            error_file.unlink()
        except OSError:
            logging.getLogger(__name__).warning('Архив создан, прежний журнал ошибки не удалён: %s', error_file)
    return destination


def auto_export(directory):
    """Ошибка экспорта не отменяет завершённую симуляцию и не меняет её метаданные."""
    try:
        return export_analysis(directory)
    except Exception as error:
        logging.getLogger(__name__).exception('Автоэкспорт не выполнен: %s', directory)
        path = Path(directory)/'NOVERIA_ANALYSIS.error.json'
        try:
            path.write_text(json.dumps(dict(error=str(error), format_version=FORMAT_VERSION,
                created_at=datetime.now(timezone.utc).isoformat()), ensure_ascii=False), encoding='utf-8')
        except OSError:
            logging.getLogger(__name__).exception('Не удалось записать ошибку экспорта')
        return None
