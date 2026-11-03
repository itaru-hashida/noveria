"""Разведочный тест: сырьё без входов планируется по факту завершённых торгов."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import research_supply_response as supply


class PostTradeSupply(supply.SupplyEconomy):
    def produce(self, existing, sells):
        self.post_trade_plans = []
        if self.probe.variant == 'post_trade_raw':
            for f in existing:
                if (self.goods[f['good']]['inputs'] or f['status'] == 'closed'
                        or f.get('suspended') or f['available_from'] > self.meta['month']):
                    continue
                plan = f.get('production_plan', {})
                if plan.get('month') != self.meta['month']:
                    continue
                before = plan['output']
                # Базовый прогноз уже включает sold_now и остатки обеспеченных заявок.
                # Склад здесь действительно после продаж. Будущие события не читаются.
                outlook = self.production_outlook(f, sells)
                target = outlook['planned_output']
                project = self.accounts[f['id']].get('project', {})
                available = max(0, self.accounts[f['id']]['balance']
                    -project.get('working_cash', 0)-project.get('delivery', 0))
                wages = self.goods[f['good']]['labor']*self.cfg['wage']
                target = min(target, available/wages)
                f.update(outlook)
                plan.update(output=target, sale_price=f['planned_price'],
                    reason='Исследование: послеторговое восполнение сырья')
                self.post_trade_plans.append(dict(id=f['id'], good=f['good'], before=before, after=target))
        # Существующие общие ограничения труда/ресурсов и реальные выплаты неизменны.
        super().produce(existing, sells)


def run(args):
    original = supply.SupplyEconomy
    supply.SupplyEconomy = PostTradeSupply
    try:
        supply.run(args)
    finally:
        supply.SupplyEconomy = original
        path = Path(args.out)/'metadata.json'
        if path.exists():
            meta = json.loads(path.read_text())
            meta['post_trade_experiment_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            meta['selection'] = 'exploratory after observing hidden bid increases and sales after zero plans'
            path.write_text(json.dumps(meta, indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--months', type=int, default=60)
    args = p.parse_args()
    args.variant = 'post_trade_raw'
    args.verify_state = None
    args.warmup = 0
    run(args)
