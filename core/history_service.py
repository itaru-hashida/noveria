class HistoryService:
    def __init__(self, db):
        self.db = db

    def list(self, **filters):
        return self.db.history(**filters)

    def parameter_history(self, entity_id, parameter, limit=100, offset=0):
        return self.db.history(entity_id=entity_id, event_type='parameter_changed', parameter=parameter,
                               limit=limit, offset=offset)
