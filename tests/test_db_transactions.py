import pytest

from core.db import PostCommitCheckpointError, WorldDB


CHECKPOINT_FAILURE = (
    'Transaction committed successfully, but the post-commit checkpoint failed. '
    'The committed data is durable and will be recovered on restart: '
    'Buffer manager exception: Unable to allocate memory!'
)


def test_committed_checkpoint_failure_does_not_roll_back(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'committed.lbug'
    with WorldDB(path) as db:
        original = db._query
        queries = []

        def fail_after_commit(query, params=None):
            queries.append(query)
            result = original(query, params)
            if query == 'COMMIT':
                raise RuntimeError(CHECKPOINT_FAILURE)
            return result

        monkeypatch.setattr(db, '_query', fail_after_commit)
        with pytest.raises(PostCommitCheckpointError) as caught:
            with db.transaction():
                db.set_turn(2)
        assert caught.value.committed is True
        assert 'ROLLBACK' not in queries
        assert 'Транзакция отменена' not in caplog.text
    with WorldDB(path, read_only=True) as reopened:
        assert reopened.meta()['turn'] == 2


def test_commit_failure_before_durability_rolls_back(tmp_path, monkeypatch):
    with WorldDB(tmp_path / 'rollback.lbug') as db:
        original = db._query

        def fail_commit(query, params=None):
            if query == 'COMMIT':
                raise RuntimeError('commit failed before durability')
            return original(query, params)

        monkeypatch.setattr(db, '_query', fail_commit)
        with pytest.raises(RuntimeError, match='before durability'):
            with db.transaction():
                db.set_turn(2)
        assert db.meta()['turn'] == 1


def test_buffer_pool_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv('RPG_DB_BUFFER_POOL_MB', '192')
    with WorldDB(tmp_path / 'configured.lbug') as db:
        assert db.database.buffer_pool_size == 192 * 1024**2
    with WorldDB(tmp_path / 'explicit.lbug', buffer_pool_size=128 * 1024**2) as db:
        assert db.database.buffer_pool_size == 128 * 1024**2


@pytest.mark.parametrize('value', ['0', '-1', 'invalid', '1.5'])
def test_invalid_buffer_pool_setting_does_not_create_save(tmp_path, monkeypatch, value):
    monkeypatch.setenv('RPG_DB_BUFFER_POOL_MB', value)
    path = tmp_path / 'invalid.lbug'
    with pytest.raises(ValueError, match='RPG_DB_BUFFER_POOL_MB'):
        WorldDB(path)
    assert not path.exists()
