"""Явное восстановление авторского контента из канона, без доступа к БД."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / 'docs' / 'noweria.md').read_text(encoding='utf-8')
    path = ROOT / 'content/initial_world.json'
    content = json.loads(path.read_text(encoding='utf-8'))
    names = {
        'noweria':'Новерская Республика', 'itaru':'Итару Первый',
        'velaria':'Веларийская Лига', 'arven':'Арвенское княжество',
        'velaria-ruler':'Марцелла Велар','arven-ruler':'Элиан Арвен',
        'demo-character':'Луций Север','group-0':'Крестьяне Новерии',
        'group-1':'Шахтёры Аурис Валлар','group-2':'Сенат',
    }
    cities = [
        ['Лунария'],['Феррата','Купрум','Баллистарис','Фортис'],
        ['Валларис','Звёздный Пик','Лунный Путь','Каменный Клык'],
        ['Эниополис','Кастеллум','Аграрис','Северный Щит'],
        ['Эллина','Лесной Круг','Серебряный Ручей','Солнечный Перевал'],
        ['Соларис','Огненные Башни','Золотой Перевал'],
        ['Оникс','Пшеничный Путь','Травяные Врата','Речной Пост'],
        ['Фирополис','Пряный Порт','Морской Щит','Караванный Перекрёсток'],
    ]
    descriptions = {}
    province_lines = [line for line in text.splitlines() if line.startswith('**') and ' — ' in line]
    for i, line in enumerate(province_lines):
        names[f'province-{i}'] = line.split('. ',1)[1].split('**')[0]
        descriptions[f'province-{i}'] = line.split(' — ',1)[1]
        for j, name in enumerate(cities[i]):
            names[f'city-{i}-{j}'] = name
    for sid, provinces in [('velaria',['Берег Ветров','Соляные Холмы']),('arven',['Сосновый Край','Медный Хребет'])]:
        for i, name in enumerate(provinces):
            names[f'{sid}-p{i}'] = name
            for j, suffix in enumerate(['столица','торговый город']):
                names[f'{sid}-c{i}-{j}'] = name+' — '+suffix
    for entity in content['entities']:
        entity['name'] = names[entity['id']]
        props = entity['properties']
        if 'description' in props:
            props['description'] = descriptions.get(entity['id'], 'Дополнение MVP, не канон noweria.md')
        if entity['id']=='noweria':
            props['description'] = text.splitlines()[1]
        if entity['id']=='itaru':
            props['title'] = 'Военный диктатор'
        if entity['kind']=='SocialGroup':
            props['description'] = 'Лояльность — стартовое значение MVP. Сенат — канонический институт; остальные группы добавлены для MVP.'
    path.write_text(json.dumps(content,ensure_ascii=False,indent=2),encoding='utf-8')
    for name, value in {
        'balance.json':dict(default_loyalty=70,note='Стартовое значение MVP, не канон'),
        'catalogs.json':dict(entity_types=['State','Province','City','Character','SocialGroup','Army'],political_systems=['Республика','Лига','Княжество']),
    }.items():
        (ROOT / 'content' / name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
