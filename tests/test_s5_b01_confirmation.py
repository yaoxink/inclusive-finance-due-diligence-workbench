"""User-confirmed B01 structure; all other business gates remain closed."""
import copy
from decimal import Decimal
import json

import pytest

from test_s5_calculations import env
from due_app.calculations import evaluate_primary_crop_scale, evaluate_score, load_model


def crop(identifier, category, area):
    return {'id': identifier, 'category': category, 'category_status': 'explicit',
            'area': area, 'area_status': 'explicit', 'unit': '亩'}


def test_confirmed_group_weights_and_remaining_five_decisions_are_exact():
    model, _ = load_model()
    assert {g['id']: g['weight'] for g in model['groups']} == {
        'household': '0.25', 'operating': '0.35', 'credit': '0.15', 'financial': '0.25'}
    assert sum(Decimal(g['weight']) for g in model['groups']) == Decimal('1')
    assert [d['id'] for d in model['confirmed_decisions']] == ['MODEL-B01', 'MODEL-B03']
    assert model['confirmed_decisions'][0]['confirmation']['answer'] == '采用这套候选规则'
    assert {d['id'] for d in model['pending_decisions']} == {f'MODEL-B0{i}' for i in range(2, 7)}
    assert all(d['status'] in {'pending', 'original_reference_retained_pending_explanation',
                               'partially_confirmed_remaining_pending'} for d in model['pending_decisions'])
    assert model['effective_enabled'] is False


def test_main_fruit_30_secondary_grain_5_uses_only_main_scale():
    result = evaluate_primary_crop_scale([crop('main-fruit', 'fruit', '30'), crop('secondary-grain', 'grain', '5')],
                                         'main-fruit', '明确主营水果，次营主粮（合成）。')
    assert result['state'] == 'reference_only'
    assert result['scale_indicator'] == 'SCORE-02'
    assert result['selected_area'] == '30' and result['reference_score'] == '60'
    assert result['scale_weight'] == '0.20'
    assert Decimal(result['weighted_reference_score']) == Decimal('4.2')
    assert result['excluded_crop_ids'] == ['secondary-grain']
    assert result['aggregation_applied'] is False and result['effective_score'] is None


def test_secondary_large_crop_never_changes_main_selection_or_area():
    records = [crop('first-secondary', 'grain', '10000'), crop('chosen-main', 'fruit', '30')]
    result = evaluate_primary_crop_scale(records, 'chosen-main', '本次明确主营水果。')
    assert result['selected_area'] == '30' and result['scale_indicator'] == 'SCORE-02'
    assert result['reference_score'] == '60'


def test_score_trace_uses_only_explicit_main_and_does_not_aggregate_scale_weights():
    result = evaluate_score({}, crops=[crop('main', 'fruit', '30'), crop('secondary', 'grain', '5')],
                            main_crop_id='main', selection_basis='本次明确主营水果。')
    scale_rows = [r for r in result['trace'] if r['indicator_id'] in {'SCORE-02', 'SCORE-03', 'SCORE-04', 'SCORE-05'}]
    assert len(scale_rows) == 1 and scale_rows[0]['indicator_id'] == 'SCORE-02'
    assert scale_rows[0]['value'] == '30' and scale_rows[0]['reference_score'] == '60'
    assert Decimal(scale_rows[0]['weighted_reference_score']) == Decimal('4.2')
    assert result['score'] is None and result['reference_base_score'] is None


def test_score_trace_missing_main_never_uses_secondary_scale():
    result = evaluate_score({}, crops=[crop('first', 'grain', '500')])
    assert not any(r['indicator_id'] in {'SCORE-02', 'SCORE-03', 'SCORE-04', 'SCORE-05'} for r in result['trace'])
    assert 'blocked_main_crop_unspecified' in result['missing_or_undefined']
    assert result['score'] is None


def test_main_crop_records_cannot_be_overridden_with_manual_indicator_inputs():
    with pytest.raises(ValueError, match='不可混用'):
        evaluate_score({'SCORE-03': '500'}, crops=[crop('main', 'fruit', '30')], main_crop_id='main', selection_basis='明确主营。')


@pytest.mark.parametrize('main,basis,expected', [
    (None, '已有面积', 'blocked_main_crop_unspecified'),
    ('main', None, 'blocked_main_crop_unspecified'),
    ('main', ' ', 'blocked_main_crop_unspecified'),
    ('not-present', '明确选择', 'blocked_main_crop_not_unique'),
])
def test_missing_or_unexplained_main_does_not_choose_first_or_largest(main, basis, expected):
    result = evaluate_primary_crop_scale([crop('main', 'fruit', '30'), crop('secondary', 'grain', '500')], main, basis)
    assert result['state'] == expected and result['reference_score'] is None
    assert result['scale_indicator'] is None and result['effective_score'] is None


@pytest.mark.parametrize('changes,expected', [
    ({'category': None}, 'blocked_main_category_unknown'),
    ({'category': 'unmapped'}, 'blocked_main_category_unknown'),
    ({'category': {}}, 'blocked_main_category_unknown'),
    ({'category_status': 'unknown'}, 'blocked_main_category_unknown'),
    ({'area_status': 'unknown'}, 'blocked_main_area_unknown'),
    ({'unit': '公顷'}, 'blocked_main_area_unknown'),
    ({'area': None}, 'blocked_main_area_invalid'),
    ({'area': 'NaN'}, 'blocked_main_area_invalid'),
    ({'area': '-1'}, 'blocked_main_area_invalid'),
])
def test_unknown_main_inputs_do_not_fall_back_to_secondary_or_other(changes, expected):
    main = {**crop('main', 'fruit', '30'), **changes}
    result = evaluate_primary_crop_scale([main, crop('secondary', 'grain', '100')], 'main', '明确主营但条件未齐。')
    assert result['state'] == expected and result['reference_score'] is None
    assert result['aggregation_applied'] is False


def test_duplicate_main_identity_is_blocked():
    result = evaluate_primary_crop_scale([crop('main', 'fruit', '30'), crop('main', 'grain', '5')], 'main', '明确主营。')
    assert result['state'] == 'blocked_main_crop_not_unique'


@pytest.mark.parametrize('kind', ['group', 'row_group', 'scale_weight', 'aggregate'])
def test_model_cannot_silently_change_confirmed_structure(tmp_path, monkeypatch, kind):
    import due_app.calculations as module
    model = copy.deepcopy(load_model()[0])
    if kind == 'group': model['groups'][0]['weight'] = '0.30'
    elif kind == 'row_group': model['score_indicators'][0]['group_weight'] = '0.10'
    elif kind == 'scale_weight': model['score_indicators'][1]['weight'] = '0.4'
    else: model['confirmed_score_structure']['aggregate_crop_areas'] = True
    path = tmp_path / 'invalid-b01.json'
    path.write_text(json.dumps(model), encoding='utf-8')
    monkeypatch.setattr(module, 'MODEL_FILE', path)
    with pytest.raises(ValueError): module.load_model()


def test_b01_does_not_activate_other_rules_or_guess_missing_case_category(env):
    state = env.service.workspace(env.cid)
    assert state['model']['confirmed_decisions'][0]['id'] == 'MODEL-B01'
    assert len(state['model']['pending_decisions']) == 5
    assert not any(f['field_id'] == 'crop.main.category' for f in env.diligence.workspace(env.cid)['facts'])
    result = env.service.run(env.cid)
    assert result['confirmed_decisions'][0]['id'] == 'MODEL-B01'
    assert env.service.report_context(env.cid)['confirmed_decisions'][0]['id'] == 'MODEL-B01'
    assert result['score'] is None and result['rating'] is None and result['effective_limit'] is None
    assert result['state'] == 'blocked_pending_business_calibration'


def test_pre_b01_snapshot_is_stale_and_old_content_is_not_rewritten(env, tmp_path, monkeypatch):
    import due_app.calculations as module
    current_model, _ = load_model()
    previous = copy.deepcopy(current_model)
    previous['version'] = 'farmer_reference_v1'
    previous['status'] = 'candidate_not_approved'
    old = copy.deepcopy(previous['confirmed_decisions'][0])
    old.update(status='pending', blocks=['score', 'rating', 'D46'])
    old.pop('confirmation')
    previous['confirmed_decisions'] = []
    previous['pending_decisions'].insert(0, old)
    previous.pop('confirmed_score_structure')
    previous.pop('confirmed_decisions')
    original_loader = module.load_model
    # A pre-change persisted snapshot fixture; the current file validator must
    # not be weakened merely to load a different historical schema.
    monkeypatch.setattr(module, 'load_model', lambda: (previous, '4fb15fad8774e5201d79d473f7f06f478cd8f9e626c0f9e04292fe5f63bd02a9'))
    run = env.service.run(env.cid)
    assert run['stale'] is False and run['model_version'] == 'farmer_reference_v1'
    monkeypatch.setattr(module, 'load_model', original_loader)
    reopened = env.service.run_detail(env.cid, run['id'])
    assert reopened['stale'] is True and reopened['model_version'] == 'farmer_reference_v1'
    assert reopened['business_hash'] == run['business_hash'] and reopened['reference'] == run['reference']
    assert reopened['pending_decisions'][0]['id'] == 'MODEL-B01'
    assert reopened['pending_decisions'][0]['status'] == 'pending'
