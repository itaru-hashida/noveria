"""Явная сборка приложения и регистрация модулей."""
import atexit
import logging
import os
from pathlib import Path

from core.db import WorldDB
from core.events import EventBus
from core.logging_setup import setup_logging
from core.world_service import WorldService
from loader import load_world
from modules import demo, diplomacy, economy, geography, population, social, world

MODULES = {'world': world, 'social': social, 'diplomacy': diplomacy, 'demo': demo,
           'geography': geography, 'population': population, 'economy': economy}


def create_application(path=None):
    setup_logging()
    db = WorldDB(path or os.environ.get('NOWERIA_DB', str(Path(__file__).parent / 'data/world.lbug')))
    try:
        load_world(db)
        bus = EventBus()
        for event_type in ('parameter_changed', 'territory_transferred', 'ruler_changed', 'diplomacy_changed',
                           'entity_created', 'relation_changed', 'turn_advanced', 'command_scheduled',
                           'economy_initialized', 'economy_month'):
            bus.subscribe(event_type, lambda event: logging.getLogger('game').info('Действие завершено: %s', event['id']))
        atexit.register(db.close)
        return WorldService(db, bus)
    except BaseException:
        db.close()
        raise


if __name__ == '__main__':
    from ui.app import run
    run()
