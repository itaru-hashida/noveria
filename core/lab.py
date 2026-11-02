"""Изолированные запуски существующего WorldService и чтение результатов."""
import hashlib
import json
import os
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.db import WorldDB
from core.events import EventBus
from core.lab_analytics import capture, temporal_anomalies
from core.lab_store import LabStore
from core.world_service import WorldService
from loader import load_world
from modules.economy import Economy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = ROOT / 'data/lab'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def engine_version():
    paths = [ROOT/p for p in ('modules/economy/v3.py', 'modules/economy/support.py', 'modules/economy/simulation.py', 'modules/geography/logic.py',
        'modules/population/logic.py', 'core/turn_service.py', 'core/world_service.py',
        'core/db.py', 'loader.py', 'content/economy.json', 'content/initial_world.json')]
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return dict(version=getattr(Economy, 'RULES_VERSION', 1), files=hashes, fingerprint=digest(hashes))


def create_experiment(name, months=120, seed=73129, source=None, detail='full', runs=DEFAULT_RUNS):
    from core.lab_export import git_revision
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 10000:
        raise ValueError('Число ходов должно быть от 1 до 10000')
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**32-1:
        raise ValueError('Seed должен быть целым числом от 0 до 2³²−1')
    if detail not in ('full', 'summary'):
        raise ValueError('Неизвестный уровень журнала')
    identity = uuid4().hex
    directory = Path(runs).resolve()/identity
    directory.mkdir(parents=True, exist_ok=False)
    config = dict(id=identity, name=name.strip() or identity, created_at=now(), seed=seed,
        source=str(Path(source).resolve()) if source else 'content/initial_world.json',
        months=months, completed=0, status='created', detail=detail, format_version=1,
        engine=engine_version(), git_commit=git_revision(), result_path=str(directory/'analytics.sqlite'),
        database=str(directory/'world.lbug'), parameters={}, pid=None)
    # Каталог резервируется через mkdir; каждое создание получает собственную БД.
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        store.save_metadata(config)
    return directory


def prepare_world(directory, config):
    initial = directory/'initial.lbug'
    if config['source'] == 'content/initial_world.json':
        content = json.loads((ROOT/config['source']).read_text(encoding='utf-8'))
        content['generation']['seed'] = config['seed']
        scenario = directory/'scenario.json'
        scenario.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
        with WorldDB(initial) as db:
            load_world(db, scenario)
            WorldService(db, EventBus()).initialize_economy()
    else:
        source = Path(config['source'])
        if not source.is_file():
            raise ValueError('Исходное сохранение не найдено')
        with WorldDB(source, read_only=True) as db, db.read_transaction():
            campaign = db.campaign()
            if not campaign or campaign.get('status') != 'ready':
                raise ValueError('Требуется завершённое состояние кампании')
            config['seed'] = campaign['seed']
            db.fork_to(initial)
    with WorldDB(initial) as db:
        service = WorldService(db, EventBus())
        service.initialize_economy()
        config['initial_month'] = Economy.load(db).meta['month']
        config['initial_state_hash'] = digest(dict(snapshot=db.snapshot(), tiles=db.tiles(),
            accounts=db.economy_records('EconomyAccount'), enterprises=db.economy_records('Enterprise'),
            economy=db.economy_records('EconomyMeta'), commands=db.pending_commands(), campaign=db.campaign()))
        db.fork_to(directory/'world.lbug')


def execute_experiment(directory):
    directory = Path(directory).resolve()
    # Даже два одновременно запущенных worker не смогут открыть одну БД на запись.
    descriptor = os.open(directory/'worker.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        config = store.metadata()
        config.update(status='running', pid=os.getpid(), started_at=now())
        store.save_metadata(config)
        try:
            prepare_world(directory, config)
            with WorldDB(directory/'world.lbug') as db:
                service = WorldService(db, EventBus())
                engine = Economy.load(db)
                initial = capture(engine, dict(month=0), config['detail'])
                # В исходном мире могли уже пройти месяцы: ось эксперимента начинается с нуля.
                store.append_month(0, initial, config)
                for step in range(1, config['months']+1):
                    if (directory/'cancel.request').exists():
                        config['status'] = 'cancelled'
                        break
                    service.advance_turn(command_id=f'lab-{config["id"]}-{step}', source='lab')
                    engine = Economy.load(db)
                    report = db.economy_month(engine.meta['month'])
                    data = capture(engine, report, config['detail'])
                    for rows in data.values():
                        for row in rows:
                            row['economy_month'] = engine.meta['month']
                    temporal_anomalies(store, step, data)
                    config.update(completed=step, updated_at=now())
                    store.append_month(step, data, config)
                else:
                    config['status'] = 'completed'
            config['finished_at'] = now()
            store.save_metadata(config)
        except BaseException as error:
            config.update(status='interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed',
                          error=f'{type(error).__name__}: {error}', finished_at=now())
            store.save_metadata(config)
            raise
    if config['status'] == 'completed':
        from core.lab_export import auto_export
        auto_export(directory)
    return config


def start_experiment(directory):
    directory = Path(directory).resolve()
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        config = store.metadata()
        if config['status'] != 'created':
            raise ValueError('Запуск уже был начат')
        config.update(status='starting', started_at=now())
        store.save_metadata(config)
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        with (directory/'worker.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-m', 'core.lab_worker', str(directory)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        return process
    except OSError as error:
        with LabStore(directory/'analytics.sqlite', create=True) as store:
            config.update(status='failed', error=str(error))
            store.save_metadata(config)
        raise


def process_alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def list_experiments(runs=DEFAULT_RUNS):
    results = []
    for path in sorted(Path(runs).glob('*/analytics.sqlite')):
        try:
            with LabStore(path) as store:
                config = store.metadata()
            if config.get('status') == 'running' and not process_alive(config.get('pid')):
                config = dict(config, status='interrupted', error='Процесс завершён без записи итогового статуса')
            if config.get('status') == 'starting' and (datetime.now(timezone.utc)-datetime.fromisoformat(config['started_at'])).total_seconds() > 60:
                config = dict(config, status='interrupted', error='Фоновый процесс не подтвердил запуск')
            results.append(dict(config, directory=str(path.parent)))
        except (OSError, ValueError, sqlite3.Error):
            continue
    return sorted(results, key=lambda item: (item.get('created_at') or '', item['id']), reverse=True)


def compare_experiments(directories, dataset='overview', **filters):
    if dataset not in ('overview', 'production', 'geography'):
        raise ValueError('Сравнивать можно агрегаты')
    if len(directories) < 2:
        raise ValueError('Выберите хотя бы два эксперимента')
    rows = []
    for directory in directories:
        with LabStore(Path(directory)/'analytics.sqlite') as store:
            meta = store.metadata()
            offset = 0
            while True:
                batch = store.query(dataset, limit=1000, offset=offset, **filters)
                if not batch:
                    break
                rows.extend(dict(row, experiment=meta['name'], experiment_id=meta['id']) for row in batch)
                offset += len(batch)
    return rows


def read_live(path, month=None):
    if not Path(path).is_file():
        raise ValueError('Сохранение не найдено; Live World не создаёт новые базы')
    with WorldDB(path, read_only=True) as db, db.read_transaction():
        campaign = db.campaign()
        if not campaign or campaign.get('status') != 'ready':
            raise ValueError('Нет завершённого состояния кампании')
        ids = [i for i in db.economy_month_ids() if i > 0]
        chosen = month if month in ids else ids[-1] if ids else None
        return dict(campaign=campaign, months=ids, report=db.economy_month(chosen) if chosen else None,
            snapshot=db.snapshot(), tiles=db.tiles(), accounts=db.economy_records('EconomyAccount'),
            enterprises=db.economy_records('Enterprise'))


def live_series(path, start, end, state_id=None, province_id=None, good=None):
    if not Path(path).is_file():
        raise ValueError('Сохранение не найдено')
    rows = []
    with WorldDB(path, read_only=True) as db, db.read_transaction():
        if db.campaign().get('status') != 'ready':
            raise ValueError('Нет завершённого состояния')
        for month in db.economy_month_ids():
            if month == 0 or not start <= month <= end:
                continue
            report = db.economy_month(month)
            provinces = [p for p in report.get('provinces', [])
                if (state_id is None or p['state_id'] == state_id) and
                   (province_id is None or p['province_id'] == province_id)]
            def quantity(mapping):
                return mapping.get(good, 0) if good else sum(mapping.values())
            trades = [t for t in report.get('trades', [])
                if (good is None or t['good'] == good) and
                   (state_id is None or state_id in (t['buyer_state'], t['seller_state'])) and
                   (province_id is None or province_id in (t['buyer_province'], t['seller_province']))]
            rows.append(dict(month=month, output=sum(quantity(p['production']) for p in provinces),
                consumed=sum(quantity(p['consumption']) for p in provinces),
                unmet=sum(quantity(p['deficits']) for p in provinces),
                household_gold=sum(p['household_gold'] for p in provinces),
                sold=sum(t['quantity'] for t in trades),
                international_volume=sum(t['quantity'] for t in trades if t['buyer_state'] != t['seller_state']),
                domestic_volume=sum(t['quantity'] for t in trades if t['buyer_state'] == t['seller_state'])))
    return rows
