"""Focused-проверка эталонной матрицы parity, лежащей в репозитории.

``tests/fixtures/native_parity_baseline_5887c10.json`` снят на ревизии
``5887c10`` до переноса блоков 1–3. Тест проверяет сам файл (полнота матрицы,
форма записей) и, если собранные расширения доступны, — что текущая ветка
совпадает с ним побитово в обеих ветках: Python (``native_mode='off'``) и
native (``native_mode='cython'``).

Процедура воспроизведения файла описана в docstring
``scripts/verify_native_parity.py`` и повторена здесь, чтобы утверждение о
побитовом совпадении можно было проверить независимо.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import verify_native_parity as parity  # noqa: E402

BASELINE = ROOT/'tests/fixtures/native_parity_baseline_5887c10.json'
# SHA-256 канонического JSON (sort_keys, без пробелов, UTF-8). Хеш байтов
# файла здесь не годится: Git отдаёт LF, а рабочая копия на Windows может быть
# CRLF, поэтому байтовый хеш зависел бы от платформы и настроек checkout.
BASELINE_CANONICAL_SHA256 = \
    '24d5950a631f5ace4d2c5d310033dee6c99ea4dad7ef5032b88cfc2f054e0a25'
SEEDS = (17, 73129)
DETAILS = ('standard', 'deep')
MODES = ('off', 'cython')
FIELDS = {'month', 'state', 'report', 'rng', 'trades', 'trade_order',
          'trade_pairs', 'events', 'event_order', 'orders', 'flows'}


def load_baseline():
    return json.loads(BASELINE.read_text(encoding='utf-8'))


def canonical_hash(data):
    """Хеш содержимого матрицы, независимый от переводов строк и отступов."""
    text = json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def test_baseline_content_hash_matches_documented_reproduction():
    """Содержимое не подменено: канонический хеш совпадает с задокументированным."""
    assert BASELINE.exists(), 'эталонная матрица обязана лежать в репозитории'
    assert canonical_hash(load_baseline()) == BASELINE_CANONICAL_SHA256


def test_baseline_is_committed_with_full_matrix():
    """Файл в репозитории покрывает всю матрицу, а не один прогон."""
    assert BASELINE.exists(), 'эталонная матрица обязана лежать в репозитории'
    baseline = load_baseline()
    expected = {f'{seed}:{detail}:{mode}'
                for seed in SEEDS for detail in DETAILS for mode in MODES}
    assert set(baseline) == expected
    for key, rows in baseline.items():
        assert len(rows) == parity.MONTHS, key
        assert [row['month'] for row in rows] == [1, 2, 3], key
        for row in rows:
            assert set(row) == FIELDS, key


def test_baseline_digests_are_well_formed():
    baseline = load_baseline()
    for key, rows in baseline.items():
        for row in rows:
            for field in ('state', 'report', 'rng', 'trade_order', 'trade_pairs',
                          'event_order'):
                assert len(row[field]) == 64, (key, field)
                int(row[field], 16)


def test_baseline_records_pre_existing_native_python_gap():
    """Расхождение native/Python — свойство базовой линии, а не переноса.

    В baseline обе ветки различаются по state/report, но совпадают по RNG,
    числу и порядку сделок и событий. Числовое расхождение развело бы и
    сделки; здесь расходятся только типы внутри состояния.
    """
    baseline = load_baseline()
    for seed in SEEDS:
        for detail in DETAILS:
            python_rows = baseline[f'{seed}:{detail}:off']
            native_rows = baseline[f'{seed}:{detail}:cython']
            for python_row, native_row in zip(python_rows, native_rows):
                assert python_row['state'] != native_row['state']
                assert python_row['rng'] == native_row['rng']
                assert python_row['trades'] == native_row['trades']
                assert python_row['trade_order'] == native_row['trade_order']
                assert python_row['trade_pairs'] == native_row['trade_pairs']
                assert python_row['events'] == native_row['events']


def test_current_branch_matches_committed_baseline():
    """Текущая ветка совпадает с baseline побитово в обеих режимах.

    Это и есть проверка переноса: если бы блоки 1–3 сдвинули траекторию хотя
    бы на один бит, дайджесты обеих веток разошлись бы с baseline.
    """
    baseline = load_baseline()
    for key in sorted(baseline):
        seed, detail, mode = key.split(':')
        rows = parity.run(int(seed), detail, mode)
        assert rows == baseline[key], key


def test_compare_baseline_mode_passes_without_manual_paths():
    """``--compare-baseline`` проходит на текущей ветке без ручных путей."""
    assert parity.BASELINE == BASELINE
    assert parity.main(['--compare-baseline']) == 0


def test_mixed_sales_history_preserves_month_five_orders_and_trades(tmp_path):
    from modules.economy.native import monthly_boundary

    if not monthly_boundary.NATIVE_MONTHLY_DRIVER:
        pytest.skip('Cython-драйвер месяца не собран')

    rows = {}
    for mode in MODES:
        directory = tmp_path / mode
        directory.mkdir()
        db, service = parity.sr.build_world(24963, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            for _ in range(5):
                report = engine.step(native_mode=mode)
            order = next(o for o in report['orders'] if o['id'] == '5-1363')
            trade = next(t for t in report['trades'] if t['buy_order'] == order['id'])
            assert order['quantity'] == trade['quantity'] == 6.459613333333334
            rows[mode] = (parity.digest(report['orders']), parity.digest(report['trades']),
                          engine.rng.getstate())
        finally:
            db.close()
    assert rows['off'] == rows['cython']
