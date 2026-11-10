"""Изолированный пакетный запуск текущей экономики через WorldService."""
import time


def run_stress(service, months, destination, progress=None):
    """Прогнать копию мира на отдельной БД, не трогая текущую кампанию.

    Копия — это структурный мир и живое `EconomyState` в памяти. Раньше копия
    делалась через `db.fork_to`, и экономика переезжала вместе с таблицами; теперь
    динамического хранения в Ladybug нет, поэтому состояние передаётся объектом,
    а отчёты собираются в памяти прогона.
    """
    if months not in (12, 60, 120):
        raise ValueError('Допустимы 12, 60 и 120 месяцев')
    from core.db import WorldDB
    from core.economy_state import EconomyState
    from core.events import EventBus
    from core.world_service import WorldService
    from copy import deepcopy

    service.db.fork_to(destination)
    reports = []
    with WorldDB(destination) as db:
        clone = WorldService(db, EventBus())
        # Копия движка забирает snapshot и tiles по ссылке, поэтому мир берётся
        # заново из форкнутой БД, а не из живого сервиса.
        world = dict(snapshot=db.snapshot(), tiles=db.tiles(), campaign=db.campaign())
        state = EconomyState.create(world, accounts=deepcopy(list(service.economy_state().engine.accounts.values())),
                                    enterprises=deepcopy(list(service.economy_state().engine.enterprises.values())),
                                    meta=deepcopy(service.economy_state().engine.meta),
                                    keep_reports=True)
        clone.attach_economy(state)
        # Первый отчёт пуст: `reset_report` создаётся конструктором движка.
        reports.append(deepcopy(state.engine.report))
        for i in range(months):
            clone.advance_turn(command_id=f'stress-{db.meta()["turn"]}-{i}')
            reports.append(deepcopy(state.last_report))
            if progress:
                progress(i + 1)
    return reports
