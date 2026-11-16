from modules.economy.native import enterprise_state_boundary as boundary


def test_apply_outlook_writes_same_live_enterprise():
    enterprise = {'id': 'e1', 'planned_output': 1}
    result = boundary.apply_outlook(enterprise, {'planned_output': 2, 'production_limit': 'capacity'})
    assert result is enterprise
    assert enterprise == {'id': 'e1', 'planned_output': 2, 'production_limit': 'capacity'}


def test_apply_outlook_preserves_values_and_types():
    enterprise = {}
    boundary.apply_outlook(enterprise, {'output': 1, 'expected_margin': 2.5})
    assert type(enterprise['output']) is int
    assert type(enterprise['expected_margin']) is float


def test_python_reference_matches_native_writer():
    left = {'id': 'e1'}
    right = {'id': 'e1'}
    outlook = {'planned_output': 3, 'production_plan': 2.5, 'production_limit': None}
    boundary.apply_outlook_python(left, outlook)
    boundary.apply_outlook(right, outlook)
    assert left == right
    assert [type(left[k]) for k in outlook] == [type(right[k]) for k in outlook]
