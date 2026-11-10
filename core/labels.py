"""Чистые подписи сущностей мира, независимые от представления."""


def territory_labels(snapshot):
    index = {entity['id']: entity for entity in snapshot['entities']}
    owners = {relation['target']: relation['source'] for relation in snapshot['links']
              if relation['kind'] == 'owns'}
    kinds = {'State': 'государство', 'Province': 'провинция', 'City': 'город'}
    labels = {}
    for identity, entity in index.items():
        if entity['kind'] not in kinds:
            continue
        parts = [entity['name'], kinds[entity['kind']]]
        if identity in owners:
            parts.append(index[owners[identity]]['name'])
        province_id = entity['properties'].get('province_id')
        if entity['kind'] == 'City' and province_id in index:
            parts.append('провинция '+index[province_id]['name'])
        labels[identity] = ' · '.join(parts)
    return labels
