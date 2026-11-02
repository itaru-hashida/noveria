import pytest
from core.db import WorldDB
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world


@pytest.fixture
def service(tmp_path):
    with WorldDB(tmp_path / 'test.lbug') as db:
        load_world(db)
        yield WorldService(db, EventBus())
