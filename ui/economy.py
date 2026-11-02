"""Обзор рынка и безопасные диагностические прогоны."""
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st

from modules.economy import Economy


def economy_page(service):
    st.header('Экономика и торговля')
    if not service.db.campaign():
        st.info('Для экономики нужна кампания с географией. Создайте отдельную новую кампанию по инструкции README.')
        return
    if not service.db.economy_records('EconomyMeta'):
        st.info('Начальные фонды и резервы создаются один раз. Это также произойдёт при завершении первого месяца.')
        if st.button('Инициализировать экономику'):
            service.initialize_economy()
            st.rerun()
        return
    engine=Economy.load(service.db)
    entities=engine.entities
    months=service.db.economy_records('EconomyMonth')
    with st.expander('Тестовый прогон на отдельной копии'):
        duration=st.selectbox('Месяцев',[12,60,120])
        st.caption('Новая база в data/economy-runs. Основная кампания не изменяется. Копия выполняет полный месячный цикл, включая ожидающие распоряжения.')
        if st.button('Запустить тестовую симуляцию'):
            destination=Path(service.db.path).resolve().parent/'economy-runs'/f'run-{uuid4().hex}.lbug'
            progress=st.progress(0,text='Создание копии…')
            reports=service.simulate_economy(duration,destination,
                progress=lambda i:progress.progress(i/duration,text=f'Месяц {i} из {duration}'))
            st.session_state['economy-stress']=dict(path=str(destination),reports=reports)
        if 'economy-stress' in st.session_state:
            st.write('Копия: '+st.session_state['economy-stress']['path'])
    source=st.radio('Источник анализа',['Текущая кампания','Последний тестовый прогон'],horizontal=True)
    if source=='Последний тестовый прогон':
        saved=st.session_state.get('economy-stress')
        if not saved:
            st.info('Сначала запустите тестовую симуляцию.'); return
        months=saved['reports']
        from core.db import WorldDB
        with WorldDB(saved['path']) as db:
            engine=Economy.load(db)
    states=[e['id'] for e in entities.values() if e['kind']=='State']
    state=st.selectbox('Государство',[None]+states,format_func=lambda i:entities[i]['name'] if i else 'Все государства')
    provinces=[e['id'] for e in entities.values() if e['kind']=='Province' and (not state or engine.owners[e['id']]==state)]
    province=st.selectbox('Провинция',[None]+provinces,format_func=lambda i:entities[i]['name'] if i else 'Все провинции')
    good=st.selectbox('Товар',[None]+list(engine.goods),format_func=lambda g:engine.goods[g]['name'] if g else 'Все товары')
    numbers=sorted(r['month'] for r in months)
    if len(numbers)>1:
        start,end=st.select_slider('Период',numbers,value=(numbers[0],numbers[-1]))
        months=[r for r in months if start<=r['month']<=end]
    def fits(row):
        return (not state or row.get('state_id')==state) and (not province or row.get('province_id')==province) and (not good or row.get('good',good)==good)
    firms=[e for e in engine.enterprises.values() if fits(e)]
    accounts=[a for a in engine.accounts.values() if fits(a)]
    section=st.selectbox('Раздел',['Обзор','Торговля','Предприятия','Провинции','Диагностика'])
    if section=='Обзор':
        a,b,c=st.columns(3)
        a.metric('Действуют',sum(e['status']=='active' for e in firms))
        b.metric('Золото',f'{sum(a["balance"] for a in accounts):,.2f}')
        c.metric('Запасы, т',f'{sum(q for a in accounts for g,q in a["stock"].items() if not good or g==good):,.2f}')
        st.caption('Балансы и предприятия — конец выбранного источника; графики — выбранный период. Золото и труд относятся ко всем товарам.')
        series=[]; prices=[]
        for r in months:
            if r['month']==0:
                continue
            ps=[p for p in r['provinces'] if fits(p)]
            trades=[t for t in r['trades'] if (not state or state in (t['buyer_state'],t['seller_state'])) and (not province or province in (t['buyer_province'],t['seller_province'])) and (not good or t['good']==good)]
            series.append(dict(Месяц=r['month'],Производство=sum(q for p in ps for g,q in p['production'].items() if not good or good==g),
                               Потребление=sum(q for p in ps for g,q in p['consumption'].items() if not good or good==g),
                               Дефицит=sum(q for p in ps for g,q in p['deficits'].items() if not good or good==g),
                               Торговля=sum(t['quantity'] for t in trades),Перевозки=sum(t['freight'] for t in trades),
                               Золото_населения=sum(p['household_gold'] for p in ps)))
            for g in engine.goods:
                selected=[t for t in trades if t['good']==g]
                volume=sum(t['quantity'] for t in selected)
                if volume:
                    prices.append(dict(Месяц=r['month'],Товар=engine.goods[g]['name'],Цена=sum(t['gold']+t['freight'] for t in selected)/volume))
        if series:
            frame=pd.DataFrame(series).set_index('Месяц')
            st.line_chart(frame[['Производство','Потребление','Дефицит','Торговля']])
            st.line_chart(frame[['Золото_населения','Перевозки']])
            st.dataframe(frame)
        if prices:
            st.line_chart(pd.DataFrame(prices).pivot(index='Месяц',columns='Товар',values='Цена'))
        location_series=[]
        for r in months:
            rows=[row for row in r.get('locations',[]) if fits(row)]
            if rows:
                location_series.append(dict(month=r['month'],gold=sum(row['balance'] for row in rows),
                    stock=sum(q for row in rows for g,q in row['stocks'].items() if not good or g==good),
                    active=sum(row['active'] for row in rows),open=sum(row['open'] for row in rows)))
        if location_series:
            data=pd.DataFrame(location_series).set_index('month')
            st.line_chart(data[['active','open']])
            st.line_chart(data[['stock']])
            st.dataframe(data)
    elif section=='Предприятия':
        st.dataframe([dict(e,location=entities[e['province_id']]['name'],
                           owner_kind=engine.accounts[e['owner_id']]['kind'],
                           balance=engine.accounts[e['id']]['balance'],stock=engine.accounts[e['id']]['stock']) for e in firms],hide_index=True)
        st.dataframe([dict(month=r['month'],**entry) for r in months for entry in r['lifecycle'] if fits(entry)],hide_index=True)
    elif section=='Торговля':
        trades=[dict(month=r['month'],**t) for r in months for t in r['trades']
                if (not state or state in (t['buyer_state'],t['seller_state'])) and (not province or province in (t['buyer_province'],t['seller_province'])) and (not good or t['good']==good)]
        st.subheader('Сделки и пути по клеткам')
        st.dataframe(trades,hide_index=True)
        st.subheader('Объявления и причины неисполнения')
        st.caption('Месячные заявки истекают в конце расчёта. remaining — неисполненный объём; следующий месяц создаёт новые заявки.')
        st.dataframe([dict(month=r['month'],**o) for r in months for o in r['orders'] if fits(o)],hide_index=True)
    elif section=='Провинции':
        rows=[dict(month=r['month'],name=entities[p['province_id']]['name'],**p) for r in months for p in r['provinces'] if fits(p)]
        st.dataframe(rows,hide_index=True)
        if rows:
            st.line_chart(pd.DataFrame(rows).pivot(index='month',columns='name',values='gold_per_capita'))
            st.line_chart(pd.DataFrame(rows).pivot(index='month',columns='name',values='welfare'))
    else:
        st.subheader('Проверки инвариантов')
        st.dataframe([dict(month=r['month'],**r['invariants']) for r in months],hide_index=True)
        st.subheader('Причины простоев и инвестиционных решений')
        st.dataframe([dict(month=r['month'],**d) for r in months for d in r['diagnostics'] if fits(d)],hide_index=True)
        st.subheader('Движение золота: отправитель и получатель')
        ids={a['id'] for a in accounts}
        st.dataframe([dict(month=r['month'],**f) for r in months for f in r['flows']
                      if ids.intersection((f['sender'],f['recipient']))],hide_index=True)
