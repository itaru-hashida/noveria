"""Отделить типовые расхождения native/Python от числовых.

Сравнивает ``native_mode='off'`` и ``native_mode='cython'`` на одном месяце
и печатает:
  * пути, где значения равны, но различается тип;
  * пути, где различается само значение (это была бы настоящая регрессия).

Нужен только для локализации; проверок не заменяет.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402


def run(seed, mode, months=1, detail='standard'):
    with workdir(f'diff_{seed}_{mode}') as directory:
        db, service = sr.build_world(seed, directory, report_detail=detail)
        try:
            engine = service.economy_state().engine
            for _ in range(months):
                engine.step(native_mode=mode)
            return engine
        finally:
            db.close()


def walk(left, right, path, type_only, value_diff):
    if type(left) is not type(right):
        if left == right:
            type_only.append((path, type(left).__name__, type(right).__name__))
        else:
            value_diff.append((path, left, right))
        return
    if isinstance(left, dict):
        for key in left:
            walk(left[key], right.get(key), f'{path}.{key}', type_only, value_diff)
        for key in right:
            if key not in left:
                value_diff.append((f'{path}.{key}', 'missing', 'present'))
        return
    if isinstance(left, list):
        if len(left) != len(right):
            value_diff.append((f'{path}.len', len(left), len(right)))
            return
        for index, (one, two) in enumerate(zip(left, right)):
            walk(one, two, f'{path}[{index}]', type_only, value_diff)
        return
    if left != right:
        value_diff.append((path, left, right))


def report(engine):
    """Отчёт без кэшей месяцев: сравниваем именно результаты месяца."""
    return {k: v for k, v in engine.report.items()}


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 17
    python_engine = run(seed, 'off')
    native_engine = run(seed, 'cython')
    type_only, value_diff = [], []
    walk({a['id']: a for a in python_engine.accounts.values()},
         {a['id']: a for a in native_engine.accounts.values()},
         'accounts', type_only, value_diff)
    walk({e['id']: e for e in python_engine.enterprises.values()},
         {e['id']: e for e in native_engine.enterprises.values()},
         'enterprises', type_only, value_diff)
    walk(report(python_engine), report(native_engine), 'report', type_only, value_diff)
    print(f'seed={seed} '
          f'типовых расхождений={len(type_only)} числовых={len(value_diff)}')
    for row in type_only[:8]:
        print('  TYPE ', row)
    for row in value_diff[:12]:
        print('  VALUE', row)
    return 1 if value_diff else 0


if __name__ == '__main__':
    sys.exit(main())
