"""Пакетный запуск той же лаборатории без Streamlit."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lab import create_experiment, execute_experiment, start_experiment

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='Эксперимент')
    parser.add_argument('--months', type=int, default=120)
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--runs', type=Path, default=Path('data/lab'))
    parser.add_argument('--detail', choices=['full', 'summary'], default='full')
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args()
    directory = create_experiment(args.name, args.months, args.seed, args.source, args.detail, args.runs)
    print(directory, flush=True)
    if args.background:
        start_experiment(directory)
    else:
        print(execute_experiment(directory))
