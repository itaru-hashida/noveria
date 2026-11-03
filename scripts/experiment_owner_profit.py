"""Дополнительный контроль: полная доступная прибыль прежним собственникам."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy
from scripts import research_money_cycle as research


class OwnerProfitEconomy(research.MoneyCycleEconomy):
    def pay_dividends(self,sells):
        previous=self.cfg['dividend_profit_share']
        try:
            self.cfg['dividend_profit_share']=1
            Economy.pay_dividends(self,sells)
        finally:
            self.cfg['dividend_profit_share']=previous


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint',required=True);p.add_argument('--out',required=True)
    p.add_argument('--months',type=int,default=360)
    args=p.parse_args();args.variant='owner_profit';args.verify_state=None
    research.MoneyCycleEconomy=OwnerProfitEconomy
    try:
        research.run(args)
    finally:
        path=Path(args.out)/'metadata.json'
        if path.exists():
            meta=json.loads(path.read_text())
            meta['owner_experiment_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            meta['selection']='recipient and payout-rate ablation; added before pilot outcomes'
            path.write_text(json.dumps(meta,indent=2)+'\n')
