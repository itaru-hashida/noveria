"""Единственный владелец commit/revision для durable economy state.

Формат таблиц и сборка payload остаются в ``EconomyState`` как совместимый
слой. Этот объект владеет только transaction boundary: revision увеличивается
после успешной публикации, а при ошибке старый committed state не меняется.
"""

import json
import os
import shutil
from pathlib import Path

CURRENT_SCHEMA_VERSION = 5
SUPPORTED_MIGRATIONS = {4}


class NativeStorage:
    """Transaction boundary поверх checkpoint writer'а экономики."""

    @staticmethod
    def commit(state, path):
        path = NativeStorage.recover(path)
        NativeStorage.migrate(path)
        next_revision = state.committed_revision + 1
        state._save_checkpoint_payload(path, next_revision)
        state.committed_revision = next_revision
        return state.checkpoint_path

    @staticmethod
    def restore_revision(schema):
        value = schema.get('committed_revision', 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('Некорректная committed revision checkpoint')
        return value

    @staticmethod
    def recover(path):
        """Восстановить committed path после прерывания публикации.

        Валидный основной каталог authoritative. Если его нет, предыдущая
        committed-копия возвращается на место. staging всегда считается
        незавершённым и удаляется, чтобы не активировать частичный commit.
        """
        path = Path(path)
        staging = path.with_name(path.name + '.staging')
        backup = path.with_name(path.name + '.previous')
        if NativeStorage._valid(path):
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            return path
        if backup.is_dir() and NativeStorage._valid(backup):
            if path.exists():
                shutil.rmtree(path)
            os.replace(backup, path)
            shutil.rmtree(staging, ignore_errors=True)
            return path
        if path.exists() and not path.is_dir():
            path.unlink()
        if staging.exists():
            shutil.rmtree(staging)
        return path

    @staticmethod
    def migrate(path):
        """Мигрировать поддержанную старую schema до текущей версии."""
        path = Path(path)
        schema_path = path / 'schema.json'
        if not schema_path.is_file():
            return path
        data = json.loads(schema_path.read_text(encoding='utf-8'))
        version = data.get('schema_version')
        if version == CURRENT_SCHEMA_VERSION:
            return path
        if version not in SUPPORTED_MIGRATIONS:
            raise ValueError('Checkpoint собран неподдерживаемой версией схемы: %s' % version)
        staging = path.with_name(path.name + '.migration')
        backup = path.with_name(path.name + '.previous')
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(path, staging)
        migrated = json.loads((staging / 'schema.json').read_text(encoding='utf-8'))
        migrated['schema_version'] = CURRENT_SCHEMA_VERSION
        migrated.setdefault('committed_revision', 0)
        (staging / 'schema.json').write_text(
            json.dumps(migrated, ensure_ascii=False), encoding='utf-8')
        if not NativeStorage._valid(staging):
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError('Результат миграции checkpoint не прошёл проверку')
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(path, backup)
        try:
            os.replace(staging, path)
        except OSError:
            if not path.exists():
                os.replace(backup, path)
            raise
        return path

    @staticmethod
    def _valid(path):
        schema = Path(path) / 'schema.json'
        if not schema.is_file():
            return False
        try:
            data = json.loads(schema.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and isinstance(data.get('schema_version'), int)
