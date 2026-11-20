"""Единственный владелец commit/revision для durable economy state.

Формат таблиц и сборка payload остаются в ``EconomyState`` как совместимый
слой. Этот объект владеет только transaction boundary: revision увеличивается
после успешной публикации, а при ошибке старый committed state не меняется.
"""


class NativeStorage:
    """Transaction boundary поверх checkpoint writer'а экономики."""

    @staticmethod
    def commit(state, path):
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
