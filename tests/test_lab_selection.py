import json
import os
import zipfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

from core.lab import list_experiments
from core.lab_store import LabStore

ROOT = Path(__file__).resolve().parents[1]


def saved_run(root, identity, created_at, status='completed', archive=True):
    directory = root/identity
    directory.mkdir(exist_ok=True)
    meta = dict(id=identity, name='Одинаковое название', created_at=created_at, started_at=created_at,
        status=status, pid=os.getpid(), completed=1 if status == 'completed' else 0, months=1,
        detail='full', seed=1, parameters={})
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        store.save_metadata(meta)
    if archive:
        with zipfile.ZipFile(directory/'NOVERIA_ANALYSIS.zip', 'w') as result:
            result.writestr('manifest.json', json.dumps(dict(experiment_id=identity, mode='standard')))
    return directory


def selector(app):
    return next(s for s in app.selectbox if s.label == 'Открыть эксперимент')


def test_latest_date_wins_over_uuid_and_explicit_selection_survives(tmp_path, monkeypatch):
    old = saved_run(tmp_path, 'aaa-old', '2026-10-01T00:00:00+00:00')
    new = saved_run(tmp_path, 'zzz-new', '2026-10-30T00:00:00+00:00')
    assert [m['id'] for m in list_experiments(tmp_path)] == [new.name, old.name]
    monkeypatch.setenv('NOVERIA_LAB_RUNS', str(tmp_path))
    app = AppTest.from_file(ROOT/'lab.py', default_timeout=30).run()
    assert not app.exception
    assert selector(app).value == new.name
    assert any(str(new/'NOVERIA_ANALYSIS.zip') == c.value for c in app.caption)
    selector(app).set_value(old.name).run()
    assert not app.exception and selector(app).value == old.name
    assert any(str(old/'NOVERIA_ANALYSIS.zip') == c.value for c in app.caption)
    app.run()
    assert selector(app).value == old.name


def test_launch_selects_new_run_and_zip_appears_after_completion(tmp_path, monkeypatch):
    import ui.lab as ui
    old = saved_run(tmp_path, 'old', '2026-10-01T00:00:00+00:00')
    monkeypatch.setenv('NOVERIA_LAB_RUNS', str(tmp_path))
    calls = []
    def create(*args, **kwargs):
        return saved_run(tmp_path, 'new', '2026-10-30T00:00:00+00:00', status='running', archive=False)
    monkeypatch.setattr(ui, 'create_experiment', create)
    monkeypatch.setattr(ui, 'start_experiment', lambda directory: calls.append(directory))
    downloads = []
    original_download = ui.st.download_button
    def download(*args, **kwargs):
        downloads.append(kwargs.get('file_name'))
        return original_download(*args, **kwargs)
    monkeypatch.setattr(ui.st, 'download_button', download)
    app = AppTest.from_file(ROOT/'lab.py', default_timeout=30).run()
    assert selector(app).value == old.name
    next(b for b in app.button if b.label == 'Запустить эксперимент').click().run()
    assert not app.exception
    assert len(calls) == 1 and selector(app).value == 'new'
    new = tmp_path/'new'
    with LabStore(new/'analytics.sqlite', create=True) as store:
        meta = store.metadata()
        meta.update(status='completed', completed=1)
        store.save_metadata(meta)
    app.run()
    assert not app.exception and selector(app).value == 'new'
    assert all(str(old/'NOVERIA_ANALYSIS.zip') != c.value for c in app.caption)
    assert any('ещё не готов' in i.value for i in app.info)
    with zipfile.ZipFile(new/'NOVERIA_ANALYSIS.zip', 'w') as result:
        result.writestr('manifest.json', json.dumps(dict(experiment_id='new')))
    app.run()
    assert not app.exception
    assert any(str(new/'NOVERIA_ANALYSIS.zip') == c.value for c in app.caption)
    assert 'NOVERIA_ANALYSIS_new.zip' in downloads


def test_deep_link_and_mismatched_archive(tmp_path, monkeypatch):
    old = saved_run(tmp_path, 'old', '2026-10-01T00:00:00+00:00')
    saved_run(tmp_path, 'new', '2026-10-30T00:00:00+00:00')
    with zipfile.ZipFile(old/'NOVERIA_ANALYSIS.zip', 'w') as result:
        result.writestr('manifest.json', json.dumps(dict(experiment_id='some-other-run')))
    monkeypatch.setenv('NOVERIA_LAB_RUNS', str(tmp_path))
    app = AppTest.from_file(ROOT/'lab.py', default_timeout=30)
    app.query_params['experiment'] = old.name
    app.run()
    assert not app.exception and selector(app).value == old.name
    assert any('ID внутри ZIP' in e.value for e in app.error)
    assert not app.get('download_button')
