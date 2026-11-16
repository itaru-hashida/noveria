"""Финальная матрица пакета: seed × месяцы × detail × Python/native.

Прогоняет реальный мир и записывает state/report/RNG digest, число и порядок
событий и сделок для Python-ветки (``native_mode='off'``) и native
(``native_mode='cython'``): два seed'а, три месяца, standard и deep.

Режимы:
    python scripts/verify_native_parity.py                  — матрица в консоль;
    python scripts/verify_native_parity.py --save X         — сохранить снимок;
    python scripts/verify_native_parity.py --compare X      — сверить со снимком;
    python scripts/verify_native_parity.py --compare-baseline
                                                            — сверить с baseline.

Эталонная матрица лежит в ``tests/fixtures/native_parity_baseline_5887c10.json``
и снята на ревизии ``5887c10`` до переноса блоков 1–3. ``--compare-baseline`` —
это и есть проверка переноса: обе ветки обязаны совпасть с ней побитово.

Процедура воспроизведения baseline (ровно то, чем он был получен):

    git worktree add -f <каталог> 5887c10
    cd <каталог>/modules/economy/native
    python setup.py build_ext --inplace
    cd <каталог>
    python scripts/verify_native_parity.py --save <снимок>.json
    git worktree remove <каталог> --force

Скрипт должен быть в этом worktree тем же (``git show 5887c10`` его не
содержит — он добавлен позже), поэтому файл копируется туда из текущей ветки.
Причина отдельного снимка: расхождение native и Python внутри одной ревизии —
свойство базовой линии, а не следствие переноса, поэтому сравнивать надо
снимки одной и той же ревизии.

Проверено на практике: процедура даёт ту же матрицу, что и закоммиченный
снимок. Закреплён канонический SHA-256 содержимого (``json.dumps`` с
``sort_keys`` и без пробелов) — он не зависит от переводов строк, поэтому
одинаков в Git-блобе с LF и в рабочей копии на Windows с CRLF:
``24d5950a631f5ace4d2c5d310033dee6c99ea4dad7ef5032b88cfc2f054e0a25``.
Этот же хеш закреплён в ``tests/test_native_parity_baseline.py``.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402

SEEDS = (17, 73129)
MONTHS = 3
DETAILS = ('standard', 'deep')
BASELINE = ROOT/'tests/fixtures/native_parity_baseline_5887c10.json'


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def state_digest(engine):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'rng': repr(engine.rng.getstate()),
    })


def run(seed, detail, native_mode):
    with workdir(f'parity_{seed}_{detail}_{native_mode}') as directory:
        db, service = sr.build_world(seed, directory, report_detail=detail)
        try:
            engine = service.economy_state().engine
            rows = []
            for _ in range(MONTHS):
                engine.step(native_mode=native_mode)
                engine.validate()
                rows.append(dict(
                    month=engine.meta['month'],
                    state=state_digest(engine),
                    report=digest(engine.report),
                    rng=digest(engine.rng.getstate()),
                    trades=len(engine.report['trades']),
                    trade_order=digest([t['trade_id'] for t in engine.report['trades']]),
                    trade_pairs=digest([(t['seller'], t['good'], t['quantity'],
                                         t['gold'], t['freight'])
                                        for t in engine.report['trades']]),
                    events=len(engine.report['events']),
                    event_order=digest([e.get('kind') for e in engine.report['events']]),
                    orders=len(engine.report['orders']),
                    flows=len(engine.report['flows']),
                ))
            return rows
        finally:
            db.close()


def collect():
    data = {}
    for seed in SEEDS:
        for detail in DETAILS:
            for mode in ('off', 'cython'):
                key = f'{seed}:{detail}:{mode}'
                data[key] = run(seed, detail, mode)
                last = data[key][-1]
                print(f'{key} state={last["state"][:12]} report={last["report"][:12]} '
                      f'rng={last["rng"][:12]} trades={last["trades"]} '
                      f'events={last["events"]}')
    return data


def main(argv):
    path = None
    compare = False
    if '--save' in argv:
        path = argv[argv.index('--save')+1]
    elif '--compare-baseline' in argv:
        path = BASELINE
        compare = True
    elif '--compare' in argv:
        path = argv[argv.index('--compare')+1]
        compare = True
    data = collect()
    if path and not compare:
        Path(path).write_text(json.dumps(data, indent=1, sort_keys=True),
                              encoding='utf-8')
        print(f'\nСнимок сохранён: {path}')
        return 0
    if compare:
        reference = json.loads(Path(path).read_text(encoding='utf-8'))
        failures = []
        for key in sorted(reference):
            for month, (left, right) in enumerate(zip(reference[key], data[key]), 1):
                if left != right:
                    failures.append((key, month, left, right))
        if failures:
            for key, month, left, right in failures:
                print(f'\nDIFF {key} month={month}')
                for field in left:
                    if left[field] != right[field]:
                        print(f'  {field}: baseline={left[field]} now={right[field]}')
            return 1
        print(f'\nPARITY OK: {len(reference)} прогонов совпали с {path} побитово')
        return 0
    # Без аргументов: печатаем только сравнение native и Python внутри ревизии.
    diffs = 0
    for seed in SEEDS:
        for detail in DETAILS:
            left = data[f'{seed}:{detail}:off']
            right = data[f'{seed}:{detail}:cython']
            for month, (python_row, native_row) in enumerate(zip(left, right), 1):
                equal = python_row == native_row
                if not equal:
                    diffs += 1
                print(f'seed={seed} detail={detail} month={month} '
                      f'python_vs_native={"OK" if equal else "DIFF"}')
    print(f'\nрасхождений python/native внутри ревизии: {diffs}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
