"""Графики первой карты: питание, доходная доступность, распределение денег."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pilot',required=True);p.add_argument('--out',required=True)
    args=p.parse_args()
    labels={'control':'Обычные правила','profit_income':'Прибыль населению',
        'budget_prices':'Бюджетная реакция цен','owner_profit':'Прибыль собственникам'}
    colors={'control':'#777777','profit_income':'#168052','budget_prices':'#267bc0','owner_profit':'#c57a20'}
    fig,axes=plt.subplots(3,1,figsize=(11,9),sharex=True)
    def smooth(xs):return [sum(xs[max(0,i-11):i+1])/len(xs[max(0,i-11):i+1]) for i in range(len(xs))]
    for variant,label in labels.items():
        rows=[json.loads(l) for l in (Path(args.pilot)/variant/'monthly.jsonl').read_text().splitlines()]
        months=[r['month'] for r in rows]
        values=([100*r['coverage'] for r in rows],
            [100*r['income_coverage_at_quotes'] for r in rows],
            [100*r['household_cash']/r['gold'] for r in rows])
        for ax,vs in zip(axes,values):ax.plot(months,smooth(vs),label=label,color=colors[variant],linewidth=1.6)
    for ax,title in zip(axes,('Фактическое обеспечение базовой едой, %',
        'Какую долю корзины покрывает текущий доход по котировкам, %',
        'Доля всех денег мира у населения, %')):
        ax.set_title(title,fontsize=11,loc='left');ax.set_ylim(0,105)
        ax.axvspan(481,540,color='#dddddd',alpha=.35)
        ax.grid(alpha=.25);ax.spines[['top','right']].set_visible(False)
    axes[0].legend(loc='upper right',fontsize=8,ncol=2)
    axes[-1].set_xlabel('Месяц симуляции')
    fig.suptitle('Одна карта, одинаковое состояние на месяце 180, новые деньги не создаются',fontsize=12)
    fig.text(.02,.012,'Скользящее среднее до 12 месяцев; серым отмечено итоговое окно 481–540. Доходная доступность не гарантирует наличие товара.',fontsize=8)
    fig.tight_layout(rect=(0,.03,1,.96));fig.savefig(args.out)
    if Path(args.out).suffix.lower()=='.svg':
        path=Path(args.out)
        path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines())+'\n')
