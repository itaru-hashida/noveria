"""Разбор сохранённого cProfile: кто вызывает горячие функции и сколько это стоит.

Запуск:
    python audit/profile_callers.py audit/_runs/profile_73129_16months.prof
    python audit/profile_callers.py audit/_runs/profile_73129_16months.prof --callees project_requests

Скрипт только читает .prof, движок не запускает.
"""
import argparse
import pstats
from pathlib import Path

# Функции, по которым нужен разбор вызывающих сторон.
TARGETS = ('production_outlook', 'recover_enterprises', 'customer_quotes',
           'net_price', 'unit_cost', 'quoted_cost', 'reachable_demand',
           'input_requests', 'plan', 'landed', 'market_signal', 'project_requests')


def short(path):
    return path.replace('\\', '/').split('/')[-1]


def find(stats, target):
    """Все записи профиля с именем функции target: (fn, nc, tt, ct, callers)."""
    rows = []
    for fn, (cc, nc, tt, ct, callers) in stats.stats.items():
        if fn[2] == target:
            rows.append((fn, nc, tt, ct, callers))
    return rows


def print_callers(stats, target, top, stream=print):
    rows = find(stats, target)
    if not rows:
        stream('нет данных по %s' % target)
        return
    for fn, nc, tt, ct, _ in rows:
        stream('%s (%s): вызовов=%d tottime=%.3f cumtime=%.3f'
               % (target, short(fn[0]), nc, tt, ct))
    edges = []
    for fn, nc, tt, ct, callers in rows:
        for cfn, (ccc, cnc, ctt, cct) in callers.items():
            edges.append((cnc, ctt, cct, cfn, short(cfn[0])))
    for nc, tt, ct, caller, mod in sorted(edges, key=lambda r: -r[2])[:top]:
        stream('   %8d вызовов  tot=%7.3f  cum=%7.3f  из %s (%s)'
               % (nc, tt, ct, caller[2], mod))


def print_callees(stats, target, top, stream=print):
    """Кому передаёт время функция target: обратные рёбра профиля."""
    edges = []
    for fn, (cc, nc, tt, ct, callers) in stats.stats.items():
        for cfn, (ccc, cnc, ctt, cct) in callers.items():
            if cfn[2] != target:
                continue
            edges.append((cnc, ctt, cct, fn, short(fn[0])))
    if not edges:
        stream('нет данных по %s' % target)
        return
    for nc, tt, ct, callee, mod in sorted(edges, key=lambda r: -r[2])[:top]:
        stream('   %8d вызовов  tot=%7.3f  cum=%7.3f  в %s (%s)'
               % (nc, tt, ct, callee[2], mod))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('profile', type=Path)
    parser.add_argument('--top', type=int, default=6)
    parser.add_argument('--only', action='append', default=None,
                        help='разобрать только эти функции (можно повторять)')
    parser.add_argument('--callees', action='append', default=None,
                        help='показать, куда уходит время этих функций')
    args = parser.parse_args()

    stats = pstats.Stats(str(args.profile))
    print('файл: %s' % args.profile)
    print('всего вызовов: %d' % stats.total_calls)
    for target in (args.only or TARGETS):
        print()
        print('=' * 78)
        print_callers(stats, target, args.top)
    for target in (args.callees or []):
        print()
        print('=' * 78)
        print('куда уходит время %s:' % target)
        print_callees(stats, target, max(args.top, 12))


if __name__ == '__main__':
    main()

