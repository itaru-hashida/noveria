"""Независимый интерфейс исследования экономики Noveria."""
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import plotly.express as px
import streamlit as st

from core.lab import (DEFAULT_RUNS, ROOT, create_experiment, list_experiments,
                      live_series, read_live, start_experiment)
from core.lab_import import import_csv
from core.lab_export import ARCHIVE_NAME, export_analysis
from core.lab_store import LabStore, csv_bytes

SECTIONS = ['Overview', 'Production', 'Enterprises', 'Market Board', 'Geography', 'Diagnostics']


def table(rows, key='table'):
    if not rows:
        st.info('Нет данных для выбранных условий. Отсутствующие показатели неизвестны.')
        return
    display = [{k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                for k, v in row.items()} for row in rows]
    links = {k: st.column_config.LinkColumn(k) for k in ('История предприятия', 'Рынок месяца')
             if k in display[0]}
    st.dataframe(display, width='stretch', hide_index=True, column_config=links)
    st.download_button('Скачать показанные строки CSV', csv_bytes(rows), key=key, file_name=key+'.csv')


def chart(rows, preferred, key, color=None):
    if not rows:
        return
    frame = pd.DataFrame(rows)
    metrics = [c for c in preferred if c in frame and pd.to_numeric(frame[c], errors='coerce').notna().any()]
    if not metrics:
        st.info('Этот источник не содержит выбранных показателей.')
        return
    metric = st.selectbox('Показатель', metrics, key=key+'-metric')
    st.plotly_chart(px.line(frame, x='month', y=metric, color=color, markers=True), width='stretch', key=key)


def filters(store, dataset, prefix):
    result = {}
    for key, label in [('state_id', 'Государство'), ('province_id', 'Провинция'), ('good', 'Товар')]:
        result[key] = st.selectbox(label, [None]+store.choices(dataset, key),
            format_func=lambda value: value or 'Все', key=prefix+key)
    return result


def paged(store, dataset, start, end, selected, key):
    page = int(st.number_input('Страница (по 200 строк)', min_value=1, value=1, key=key+'-page'))
    rows = store.query(dataset, start=start, end=end, limit=200, offset=(page-1)*200, **selected)
    if dataset == 'diagnostics':
        experiment = st.query_params.get('experiment')
        for row in rows:
            common = dict(experiment=experiment, month=row['month'])
            row['Рынок месяца'] = '?'+urlencode(dict(common, section='Market Board'))
            if row.get('entity_id'):
                row['История предприятия'] = '?'+urlencode(dict(common, section='Enterprises', entity=row['entity_id']))
    table(rows, key)
    if st.button('Подготовить полный CSV по фильтрам', key=key+'-export'):
        destination = store.path.parent/(key+'-export.csv')
        store.export_csv(destination, dataset, start=start, end=end, **selected)
        st.session_state[key+'-file'] = str(destination)
    saved = st.session_state.get(key+'-file')
    if saved and Path(saved).resolve().parent != store.path.resolve().parent:
        # Ссылка на выгрузку прошлого эксперимента не должна переходить в новый.
        st.session_state.pop(key+'-file', None)
        saved = None
    if saved and Path(saved).is_file():
        # Выгрузка читается только по явному запросу пользователя.
        st.caption('Полная выгрузка: '+saved)
        with Path(saved).open('rb') as stream:
            st.download_button('Скачать полный CSV', stream, file_name=Path(saved).name, key=key+'-download')


@st.fragment(run_every='2s')
def progress_panel(runs):
    metas = list_experiments(runs)
    signature = []
    for meta in metas:
        files = []
        for filename in (ARCHIVE_NAME, 'NOVERIA_ANALYSIS.error.json'):
            path = Path(meta['directory'])/filename
            try:
                stat = path.stat()
                files.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                files.append(None)
        signature.append((meta['id'], meta['status'], tuple(files)))
    previous = st.session_state.get('lab-runs-signature')
    st.session_state['lab-runs-signature'] = signature
    if previous is not None and previous != signature:
        # Отдельный fragment обновляет всю страницу при завершении расчёта/экспорта.
        st.rerun(scope='app')
    for meta in metas:
        if meta['status'] in ('running', 'starting'):
            st.progress(min(1.0, meta['completed']/meta['months']),
                text=f'{meta["name"]}: {meta["completed"]} / {meta["months"]} месяцев · {meta["status"]}')
            if st.button('Остановить после текущего месяца', key='cancel-'+meta['id']):
                (Path(meta['directory'])/'cancel.request').touch()
                st.info('Остановка запрошена. Завершённые месяцы сохранены.')
        elif meta['status'] in ('failed', 'interrupted'):
            st.warning(f'{meta["name"]}: {meta["status"]}. {meta.get("error", "")}')


def experiment_page(runs):
    with st.expander('Новый эксперимент', expanded=True):
        with st.form('create-experiment'):
            name = st.text_input('Название', 'Контрольная экономика')
            scenario = st.selectbox('Исходный мир', ['Новый мир из сценария', 'Копия сохранения'])
            source = st.text_input('Путь исходного сохранения', os.environ.get('NOWERIA_DB', str(ROOT/'data/world.lbug')))
            seed = int(st.number_input('Seed нового мира', min_value=0, max_value=2**32-1, value=73129))
            months = int(st.number_input('Количество ходов', min_value=1, max_value=10000, value=120))
            detail = st.selectbox('Подробность архива', ['full', 'summary'],
                format_func=lambda x: 'Полный журнал рынка' if x == 'full' else 'Агрегаты и история предприятий')
            st.caption('Копия наследует seed и ожидающие распоряжения сохранения. Seed нового мира меняет географию. Формулы экономики общие с игрой.')
            submit = st.form_submit_button('Запустить эксперимент')
        if submit:
            try:
                directory = create_experiment(name, months, seed, source if scenario == 'Копия сохранения' else None, detail, runs)
                start_experiment(directory)
                st.session_state['lab-experiment-id'] = directory.name
                st.query_params['experiment'] = directory.name
                for key in ('section', 'entity', 'month'):
                    if key in st.query_params:
                        del st.query_params[key]
                st.success('Эксперимент запущен. Можно просматривать другие результаты.')
            except (ValueError, OSError) as error:
                st.error(str(error))
    progress_panel(runs)
    st.button('Обновить список и результаты')
    metas = list_experiments(runs)
    if not metas:
        st.info('Сохранённых экспериментов пока нет.')
        return
    identities = [m['id'] for m in metas]
    requested = st.query_params.get('experiment')
    if st.session_state.get('lab-experiment-id') not in identities:
        st.session_state['lab-experiment-id'] = requested if requested in identities else identities[0]
    selected = st.selectbox('Открыть эксперимент', identities,
        key='lab-experiment-id',
        format_func=lambda i: next(f'{m["name"]} · {m.get("created_at", "")} · {i[:8]} · {m["status"]} · {m["completed"]}/{m["months"]}' for m in metas if m['id'] == i))
    st.query_params['experiment'] = selected
    meta = next(m for m in metas if m['id'] == selected)
    st.caption(f'Выбран эксперимент: {meta["name"]} · ID {meta["id"]}')
    with st.expander('Сведения о запуске'):
        st.json(meta)
    if meta['status'] == 'completed':
        with st.expander('Архив для анализа'):
            error_file = Path(meta['directory'])/'NOVERIA_ANALYSIS.error.json'
            if error_file.is_file():
                try:
                    st.warning('Предыдущий автоэкспорт не выполнен: '+json.loads(error_file.read_text(encoding='utf-8'))['error'])
                except (OSError, ValueError, KeyError):
                    st.warning('Есть журнал ошибки предыдущего автоэкспорта: '+str(error_file))
            full = st.checkbox('Добавить исходные заявки, сделки и денежные переводы', key='analysis-full')
            if st.button('Создать NOVERIA_ANALYSIS.zip'):
                try:
                    with st.spinner('Проверка и экспорт сохранённой аналитики…'):
                        archive = export_analysis(meta['directory'], full=full)
                    st.success('Архив готов: '+str(archive))
                except (ValueError, OSError, sqlite3.Error) as error:
                    st.error(str(error))
            archive = Path(meta['directory'])/ARCHIVE_NAME
            if archive.is_file():
                try:
                    with zipfile.ZipFile(archive) as contents:
                        manifest = json.loads(contents.read('manifest.json'))
                    if manifest.get('experiment_id') != meta['id']:
                        raise ValueError('ID внутри ZIP не соответствует выбранному эксперименту. Пересоздайте архив.')
                    st.caption(f'ZIP эксперимента {meta["name"]} · {meta["id"]}')
                    st.caption(str(archive))
                    with archive.open('rb') as stream:
                        st.download_button('Скачать NOVERIA_ANALYSIS.zip', stream,
                            file_name=f'NOVERIA_ANALYSIS_{meta["id"]}.zip', mime='application/zip',
                            key=f'analysis-zip-{meta["id"]}-{archive.stat().st_mtime_ns}')
                except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                    st.error('Архив недоступен: '+str(error))
            else:
                st.info('ZIP выбранного эксперимента ещё не готов. Он появится здесь после автоэкспорта; при необходимости создайте его кнопкой выше.')
    with LabStore(Path(meta['directory'])/'analytics.sqlite') as store:
        analytics_page(store, meta)


def analytics_page(store, meta):
    requested_section = st.query_params.get('section', 'Overview')
    section = st.selectbox('Раздел', SECTIONS,
        index=SECTIONS.index(requested_section) if requested_section in SECTIONS else 0)
    completed = meta.get('completed', 0)
    start, end = (0, completed)
    if completed > 0:
        start, end = st.slider('Период эксперимента', 0, completed, (0, completed))
    dataset = dict(Overview='production', Production='production', Enterprises='enterprises',
        Geography='geography', Diagnostics='diagnostics', **{'Market Board': 'orders'})[section]
    selected = filters(store, dataset, 'analysis-'+dataset)
    if meta.get('detail') == 'legacy':
        st.info(meta['missing'])
    if section == 'Overview':
        if meta.get('engine', {}).get('version') == 3 and st.button('Проверить сходимость V3'):
            from core.economy_convergence import assess_convergence
            st.json(assess_convergence(store))
        if all(value is None for value in selected.values()):
            series = store.series('overview', start, end)
        else:
            series = store.series('production', start, end, **selected)
        chart(series, ['output', 'sold', 'stock', 'active', 'closed', 'opened', 'closures', 'unmet',
            'consumed', 'profit', 'international_volume', 'domestic_volume', 'household_gold',
            'enterprise_gold', 'treasury_gold', 'private_gold', 'merchant_gold', 'wages', 'turnover',
            'pending_projects', 'unmet_solvent', 'consumption_tax', 'dividends', 'working_reserve',
            'project_reserve', 'owner_consumption_budget', 'owner_consumption_spent', 'owner_consumption_unspent',
            'owner_unfilled_orders', 'owner_consumption_tax', 'owner_consumption_freight',
            'dividends_private', 'dividends_merchant', 'dividends_treasury', 'dividends_municipal',
            'money_share_household', 'money_share_enterprise', 'money_share_private', 'money_share_merchant',
            'money_share_treasury', 'money_share_municipal', 'money_share_carrier'], 'overview-chart')
        table(series, 'overview-series')
        if st.checkbox('Показать счета и запасы'):
            selected_accounts = {k: v for k, v in selected.items() if k != 'good'}
            paged(store, 'accounts', end, end, selected_accounts, 'accounts')
            paged(store, 'stocks', end, end, selected, 'stocks')
    elif section == 'Production':
        st.caption('Мощность измерена на конец месяца; расширения и сокращения происходят после производства. Денежные издержки производства и себестоимость продаж показаны отдельно.')
        chart(store.series(dataset, start, end, **selected), ['output', 'capacity', 'sold', 'stock', 'unmet', 'production_cost'], 'production-chart')
        paged(store, dataset, start, end, selected, 'production')
        initial = Path(meta['directory'])/'initial.lbug'
        if initial.is_file() and st.checkbox('Показать производственные цепочки'):
            from core.db import WorldDB
            with WorldDB(initial, read_only=True) as db, db.read_transaction():
                config = db.economy_records('EconomyMeta')[0]['config']
            edges = [dict(сырьё=g, продукт=good, количество=ratio)
                for good, recipe in config['goods'].items() for g, ratio in recipe['inputs'].items()]
            table(edges, 'recipes')
    elif section == 'Enterprises':
        identities = store.choices(dataset, 'entity_id')
        requested_entity = st.query_params.get('entity')
        chosen = st.multiselect('Предприятия для сравнения', identities,
            default=[requested_entity] if requested_entity in identities else [])
        if chosen:
            rows = []
            for identity in chosen:
                rows.extend(dict(r, enterprise=identity) for r in store.series(dataset, start, end, **dict(selected, entity_id=identity)))
            chart(rows, ['output', 'sold', 'profit', 'balance', 'revenue', 'expenses', 'dividends',
                'working_reserve', 'project_reserve'], 'firm-chart', 'enterprise')
            table(rows, 'firm-series')
            for identity in chosen:
                st.write(identity)
                paged(store, dataset, start, end, dict(selected, entity_id=identity), 'firm-'+identity)
        else:
            paged(store, dataset, start, end, selected, 'firms')
    elif section == 'Market Board':
        if meta.get('detail') != 'full':
            st.info('Подробный журнал не включён в этот архив. Данные не восстанавливаются расчётным путём.')
            return
        try:
            requested_month = int(st.query_params.get('month', end))
        except ValueError:
            requested_month = end
        month = int(st.number_input('Ход рынка', min_value=start, max_value=end, value=max(start, min(end, requested_month))))
        entity = st.selectbox('Участник заявки', [None]+store.choices('orders', 'entity_id'))
        status = st.selectbox('Статус заявки', [None, 'filled', 'partial', 'unfilled', 'active'])
        paged(store, 'orders', month, month, dict(selected, entity_id=entity, status=status), 'orders')
        st.caption('creation_index — создание заявки; execution_index — порядок исполненных сделок. buy_order и sell_order связывают сделки с заявками. Причины отказов отражают ответ движка.')
        order_id = st.text_input('ID заявки для просмотра исполнений')
        if order_id:
            # Запрос одного месяца ограничен страницей, ссылки проверяются внутри SQL.
            rows = store.order_trades(month, order_id)
            table(rows, 'order-trades')
        if st.checkbox('Показать сделки и денежные переводы'):
            st.caption('Фильтры сделок относятся к покупателю, переводов — к отправителю.')
            paged(store, 'trades', month, month, selected, 'trades')
            paged(store, 'flows', month, month, {k: v for k, v in selected.items() if k != 'good'}, 'flows')
    elif section == 'Geography':
        chart(store.series(dataset, start, end, **selected), ['production', 'consumed', 'unmet', 'imports', 'exports'], 'geography-chart')
        st.caption('Население и денежные остатки повторяются в строках товаров; их нельзя складывать по товарам.')
        paged(store, dataset, start, end, selected, 'geography')
        if st.checkbox('Показать карту'):
            from core.db import WorldDB
            from ui.map_view import map_figure
            initial = Path(meta['directory'])/'initial.lbug'
            if initial.is_file():
                with WorldDB(initial, read_only=True) as db, db.read_transaction():
                    snapshot, tiles = db.snapshot(), db.tiles()
                st.plotly_chart(map_figure(tiles, snapshot, selected_state=selected['state_id']), width='stretch')
            else:
                st.info('Карта не сохранена в этом источнике.')
    else:
        st.caption('Пороговые наблюдения MVP: запасы > 3 месячных продаж, разброс продаж > 10 раз, падение выпуска > 50%, дефицит 3 месяца. Это факты и корреляции, а не доказанные причины. ID и месяц связывают наблюдение с другими разделами.')
        paged(store, dataset, start, end, selected, 'diagnostics')
        paged(store, 'lifecycle', start, end, selected, 'lifecycle')


def comparison_page(runs):
    metas = [m for m in list_experiments(runs) if m['completed'] > 0]
    chosen = st.multiselect('Эксперименты для сравнения', [m['id'] for m in metas],
        format_func=lambda i: next(m['name']+' · '+i[:8] for m in metas if m['id'] == i))
    if len(chosen) < 2:
        st.info('Выберите два или более запуска.')
        return
    dataset = st.selectbox('Сравнить', ['overview', 'production', 'geography'])
    rows = []
    with LabStore(Path(next(m['directory'] for m in metas if m['id'] == chosen[0]))/'analytics.sqlite') as store:
        selected = filters(store, dataset, 'comparison')
    for identity in chosen:
        meta = next(m for m in metas if m['id'] == identity)
        with LabStore(Path(meta['directory'])/'analytics.sqlite') as store:
            rows.extend(dict(r, experiment=meta['name']+' · '+identity[:8]) for r in store.series(dataset, **selected))
    chart(rows, ['output', 'production', 'sold', 'stock', 'active', 'closed', 'opened', 'closures',
        'unmet', 'consumed', 'imports', 'exports', 'profit'], 'comparison-chart', 'experiment')
    st.caption('Ось — месяцы от начала эксперимента. Пустые значения неизвестны. Последние точки могут относиться к разным месяцам; для итоговой разницы выбран общий месяц.')
    frame = pd.DataFrame(rows)
    if not frame.empty:
        by_experiment = [frame[frame['experiment'] == label] for label in frame['experiment'].unique()]
        common = set.intersection(*(set(f['month']) for f in by_experiment))
        if common:
            final = frame[frame['month'] == max(common)]
            st.write(f'Общий месяц: {max(common)}')
            table(final.to_dict('records'), 'comparison-final')
            numeric = final.select_dtypes(include='number').columns.drop('month', errors='ignore')
            delta = final.copy()
            for column in numeric:
                delta[column] = final[column]-final.iloc[0][column]
            table(delta.to_dict('records'), 'comparison-delta')
    table(rows, 'comparison-series')


def live_page():
    path = st.text_input('Игровое сохранение', os.environ.get('NOWERIA_DB', str(ROOT/'data/world.lbug')))
    st.caption('Только чтение завершённых транзакций. Если LadybugDB занята другим процессом, остановите игру и повторите чтение. Открытый файл базы не копируется.')
    try:
        live = read_live(path)
        if not live['months']:
            st.info('В сохранении ещё нет завершённых экономических месяцев.')
            return
        month = st.selectbox('Завершённый месяц', live['months'], index=len(live['months'])-1)
        if month != live['report']['month']:
            live = read_live(path, month)
    except (RuntimeError, ValueError, OSError) as error:
        st.error('Сохранение недоступно для чтения: '+str(error))
        return
    report = live['report']
    section = st.selectbox('Раздел Live World', SECTIONS)
    state = st.selectbox('Государство', [None]+sorted({a['state_id'] for a in live['accounts']}))
    province = st.selectbox('Провинция', [None]+sorted({a['province_id'] for a in live['accounts'] if a['province_id'] and (state is None or a['state_id'] == state)}))
    good = st.selectbox('Товар', [None]+sorted(report.get('stocks', {})))
    def fits(row):
        return (state is None or row.get('state_id') == state) and (province is None or row.get('province_id') == province) and (good is None or row.get('good', good) == good)
    if section in ('Overview', 'Production', 'Geography'):
        period = (live['months'][0], live['months'][-1])
        if period[0] != period[1]:
            period = st.slider('Период Live World', period[0], period[1], period)
        if st.button('Построить динамику'):
            try:
                st.session_state['live-series'] = (path, period, state, province, good,
                    live_series(path, *period, state, province, good))
            except (RuntimeError, ValueError, OSError) as error:
                st.error(str(error))
        saved = st.session_state.get('live-series')
        if saved and saved[:5] == (path, period, state, province, good):
            chart(saved[5], ['output', 'consumed', 'unmet', 'household_gold', 'sold', 'international_volume', 'domestic_volume'], 'live-chart')
        def selected_goods(row):
            return {k: ({good: v.get(good)} if good and isinstance(v, dict) else v) for k, v in row.items()}
        table([selected_goods(p) for p in report.get('provinces', []) if fits(p)], 'live-regions')
        table([selected_goods(r) for r in report.get('locations', []) if fits(r)], 'live-balances')
        st.caption('Производство, потребление, дефицит и запасы внутри строк разбиты по товарам.')
        if section == 'Geography':
            from ui.map_view import map_figure
            st.plotly_chart(map_figure(live['tiles'], live['snapshot'], selected_state=state), width='stretch')
    elif section == 'Enterprises':
        st.info('В игровом сохранении есть только последнее состояние предприятий и счетов. Помесячная история доступна для новых экспериментов.')
        st.caption('Текущее состояние; последний завершённый экономический месяц '+str(live['months'][-1]))
        table([e for e in live['enterprises'] if fits(e)], 'live-firms')
        table([a for a in live['accounts'] if fits(a)], 'live-accounts')
    elif section == 'Market Board':
        table([o for o in report.get('orders', []) if fits(o)][:500], 'live-orders')
        st.caption('Показаны первые 500 заявок выбранного месяца с учётом фильтров.')
        table([dict(t, state_id=t['buyer_state'], province_id=t['buyer_province']) for t in report.get('trades', [])
               if fits(dict(t, state_id=t['buyer_state'], province_id=t['buyer_province']))][:500], 'live-trades')
    else:
        table(report.get('diagnostics', []), 'live-diagnostics')


def run():
    st.set_page_config(page_title='Noveria Lab', layout='wide')
    st.title('Noveria Lab')
    runs = Path(os.environ.get('NOVERIA_LAB_RUNS', str(DEFAULT_RUNS)))
    mode = st.sidebar.radio('Режим', ['Experiments', 'Live World', 'Сравнение', 'Импорт CSV'])
    if mode == 'Experiments':
        experiment_page(runs)
    elif mode == 'Live World':
        live_page()
    elif mode == 'Сравнение':
        comparison_page(runs)
    else:
        st.caption('Поддерживаются отчёты существующего экономического аудита. Каждый сценарий импортируется отдельно. Отсутствующие поля остаются неизвестными.')
        uploaded = st.file_uploader('CSV отчёт', type=['csv'])
        if uploaded is not None and st.button('Импортировать отчёт'):
            try:
                directories = import_csv(uploaded.getvalue(), uploaded.name, runs)
                st.success(f'Импортировано сценариев: {len(directories)}')
            except (ValueError, OSError) as error:
                st.error(str(error))
