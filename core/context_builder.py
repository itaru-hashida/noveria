import json


class ContextBuilder:
    def __init__(self, db):
        self.db = db

    def world_summary(self):
        return dict(meta=self.db.meta(), counts=self.db.counts())

    def entity_context(self, entity_id, event_limit=10):
        return dict(current_state=self.db.get_entity(entity_id),
                    current_relations=self.db.links(entity_id, limit=100),
                    recent_history=self.db.history(entity_id=entity_id, limit=event_limit))

    def state_context(self, entity_id, event_limit=10):
        result = self.entity_context(entity_id, event_limit)
        if result['current_state']['kind'] != 'State':
            raise ValueError('Ожидается государство')
        return result

    def character_context(self, entity_id, event_limit=10):
        result = self.entity_context(entity_id, event_limit)
        if result['current_state']['kind'] != 'Character':
            raise ValueError('Ожидается персонаж')
        return result

    def build(self, entity_id=None, event_limit=10, max_chars=12000):
        if not 1 <= event_limit <= 100 or max_chars < 256:
            raise ValueError('Недопустимый лимит контекста')
        result = dict(world=self.world_summary(), current_state=None, current_relations=[],
                      recent_history=[], important_facts=[])
        if entity_id:
            result.update(self.entity_context(entity_id, event_limit))
        else:
            result['recent_history'] = self.db.history(limit=event_limit)
        remaining = event_limit - len(result['recent_history'])
        if remaining:
            result['important_facts'] = self.db.history(importance=8, limit=remaining)
        result['truncated'] = False
        while len(json.dumps(result, ensure_ascii=False)) > max_chars:
            result['truncated'] = True
            for key in ('important_facts', 'recent_history', 'current_relations'):
                if result[key]:
                    result[key].pop()
                    break
            else:
                result['current_state'] = None
                result['world'] = {'turn': self.db.meta()['turn']}
        return result
