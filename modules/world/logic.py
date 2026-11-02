def transfer_territory(service, territory_id, state_id, **kwargs):
    return service.transfer_territory(territory_id, state_id, **kwargs)


def appoint_ruler(service, state_id, character_id, **kwargs):
    return service.appoint_ruler(state_id, character_id, **kwargs)
