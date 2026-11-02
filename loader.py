import json
import logging
from pathlib import Path
from modules.geography import generate
from modules.population import create_groups
from core.db import ENTITY_TYPES, RELATIONS

ROOT = Path(__file__).resolve().parent


def validate_content(content):
    entities=content['entities']; index={e['id']:e for e in entities}
    if len(index)!=len(entities): raise ValueError('Повтор ID сценария')
    for e in entities:
        if not e['id'] or not e['name'].strip() or e['kind'] not in ENTITY_TYPES: raise ValueError('Некорректная сущность')
    seen=set()
    for r in content['links']:
        a=index.get(r['source']); b=index.get(r['target'])
        key=(r['source'],r['target'],r['kind'])
        if not a or not b or key in seen or a==b or (a['kind'],b['kind']) not in RELATIONS.get(r['kind'],set()):
            raise ValueError(f'Некорректная связь: {key}')
        seen.add(key)
    for e in entities:
        if e['kind'] in ('City','Province'):
            for kind in ('includes','owns'):
                if sum(r['target']==e['id'] and r['kind']==kind for r in content['links'])!=1:
                    raise ValueError(f'Нет единственного {kind}: {e["id"]}')
        if e['kind']=='Province':
            capital=e['properties'].get('capital_id')
            if not any(r['source']==e['id'] and r['target']==capital and r['kind']=='includes' for r in content['links']):
                raise ValueError('Столица должна принадлежать провинции')
    return content


def load_world(db, content_path=None):
    """Завершённое сохранение никогда не обновляется из авторского JSON."""
    if db.meta()['seeded']:
        return False
    content = validate_content(json.loads(Path(content_path or ROOT / 'content/initial_world.json').read_text(encoding='utf-8')))
    logging.getLogger(__name__).info('Генерация сценария seed=%s',content['generation']['seed'])
    geography=generate(content)
    groups=create_groups(content,geography)
    with db.transaction():
        if db.meta()['seeded']:
            return False
        for entity in content['entities']:
            properties=dict(entity.get('properties',{}))
            properties.update(geography['placements'].get(entity['id'],{}))
            db.create_entity(entity['id'], entity['kind'], entity['name'], properties)
        for link in content['links']:
            db.put_link(link['source'], link['target'], link['kind'], link.get('properties', {}))
        for group in groups:
            db.create_entity(group['id'],group['kind'],group['name'],group['properties'])
            db.put_link(group['id'],group['properties']['owner_id'],'located_in')
        db.store_tiles(geography['tiles'])
        db.save_campaign(dict(**content['generation'],status='ready',content_version=content['version']))
        from core.integrity import validate_world
        validate_world(db.snapshot(), db.tiles(), db.campaign())
        db.mark_seeded()
    return True
