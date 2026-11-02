from uuid import uuid4

from modules.social import change_loyalty


def run_demo(service, group_id, source='demo'):
    action_id = str(uuid4())
    return [change_loyalty(service, group_id, value, source=source, action_id=action_id)
            for value in (70, 55, 40)]
