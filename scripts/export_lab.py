"""Экспорт существующего завершённого эксперимента без запуска симуляции."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lab_export import export_analysis


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path, help='Каталог с analytics.sqlite')
    parser.add_argument('--full', action='store_true', help='Добавить исходные заявки, сделки и переводы')
    args = parser.parse_args()
    print(export_analysis(args.experiment, full=args.full))
