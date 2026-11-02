"""Изолированный пакетный запуск текущей экономики через WorldService."""

def run_stress(service, months, destination, progress=None):
    if months not in (12,60,120):
        raise ValueError('Допустимы 12, 60 или 120 месяцев')
    from core.db import WorldDB
    from core.events import EventBus
    from core.world_service import WorldService
    service.db.fork_to(destination)
    with WorldDB(destination) as db:
        clone=WorldService(db,EventBus())
        for i in range(months):
            clone.advance_turn(command_id=f'stress-{db.meta()["turn"]}-{i}')
            if progress:
                progress(i+1)
        return db.economy_records('EconomyMonth')

