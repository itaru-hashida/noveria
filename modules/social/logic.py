import math


def change_loyalty(service, group_id, value, **kwargs):
    if service.db.get_entity(group_id)['kind'] != 'SocialGroup':
        raise ValueError('Ожидается социальная группа')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Лояльность должна быть конечным неотрицательным числом; верхнего предела нет')
    return service.change_parameter(group_id, 'loyalty', value, **kwargs)
