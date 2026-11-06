"""B03 amount basis and full coefficients are confirmed; aggregation is not."""
import copy
from decimal import Decimal
import json

import pytest

from test_s5_calculations import env
from due_app.calculations import calculate_reference, evaluate_indicator, load_model, reference_rating


def test_b03_confirmation_is_partial_and_remaining_rules_stay_pending():
    model, _ = load_model()
    confirmed = next(row for row in model['confirmed_decisions'] if row['id'] == 'MODEL-B03')
    remaining = next(row for row in model['pending_decisions'] if row['id'] == 'MODEL-B03')
    assert confirmed['status'] == 'partially_confirmed'
    assert confirmed['confirmation']['answer'] == '采用当前现值、余额和完整系数'
    assert remaining['status'] == 'partially_confirmed_remaining_pending'
    assert set(remaining['blocks']) == {'D42', 'D44', 'D46_input_qualification'}
    basis = model['confirmed_balance_basis']
    assert basis['asset_amount'] == 'current_value'
    assert basis['debt_amount'] == 'current_outstanding_balance'
    assert basis['same_data_as_of_required'] is True
    assert basis['purchase_price_and_contract_principal_are_separate'] is True
    assert basis['aggregation_scope'] == basis['exclusions_and_discounts'] == 'pending_business_definition'
    assert basis['D42_aggregation'] == basis['D44_derivation'] == 'pending_business_definition'
    assert model['effective_enabled'] is False


@pytest.mark.parametrize('score,rating,coefficient', [
    ('80', 'AAA', '1'), ('75', 'AA', '0.9125'), ('65', 'A', '0.825'),
    ('55', 'BBB', '0.7375'), ('45', 'BB', '0.65'), ('35', 'B', '0.5625'),
    ('25', 'CCC', '0.475'), ('15', 'CC', '0.3875'), ('5', 'C', '0.3'),
])
def test_original_rating_coefficients_keep_full_precision(score, rating, coefficient):
    reference = reference_rating(score)
    assert reference['rating'] == rating and reference['coefficient'] == coefficient
    assert reference['state'] == 'reference_only'


def test_aa_coefficient_in_reference_formula_is_not_rounded_to_sample_091():
    coefficient = reference_rating('75')['coefficient']
    result = calculate_reference({'G10': '100000', 'G13': '0', 'G16': '0', 'D44': '1', 'D46': coefficient})
    route = next(row for row in result['trace'] if row['cell'] == 'E25')
    assert coefficient == '0.9125'
    assert Decimal('100000') * Decimal(coefficient) == Decimal('91250')
    assert Decimal(route['value']) == Decimal('18250')
    assert Decimal(route['value']) != Decimal('18200')
    assert result['effective_limit'] is None


@pytest.mark.parametrize('change', ['asset', 'debt', 'date', 'purchase', 'declared', 'principal', 'AA_rounding', 'duplicate_rating'])
def test_changed_confirmed_basis_or_rating_precision_fails_closed(tmp_path, monkeypatch, change):
    import due_app.calculations as module
    model = copy.deepcopy(load_model()[0])
    basis = model['confirmed_balance_basis']
    if change == 'asset': basis['asset_amount'] = 'purchase_price'
    elif change == 'debt': basis['debt_amount'] = 'contract_principal'
    elif change == 'date': basis['same_data_as_of_required'] = False
    elif change == 'purchase': basis['purchase_price_and_contract_principal_are_separate'] = False
    elif change == 'declared': basis['declared_asset_value_is_not_current_value'] = False
    elif change == 'principal': basis['contract_principal_is_not_current_balance'] = False
    elif change == 'AA_rounding': next(row for row in model['ratings'] if row['rating'] == 'AA')['coefficient'] = '0.91'
    else: model['ratings'].append(copy.deepcopy(model['ratings'][0]))
    path = tmp_path / 'invalid-b03.json'
    path.write_text(json.dumps(model), encoding='utf-8')
    monkeypatch.setattr(module, 'MODEL_FILE', path)
    with pytest.raises(ValueError): module.load_model()


def test_declared_assets_contract_principal_or_even_current_records_do_not_invent_d44(env):
    model, _ = load_model()
    state = copy.deepcopy(env.diligence.workspace(env.cid))
    records = [
        {'id': 'declared-asset', 'field_id': 'assets.house_declared_value', 'value': '450000', 'record_confirmation': 'record_accurate'},
        {'id': 'contract-principal', 'field_id': 'debt.bank_contracts', 'value': '100000', 'record_confirmation': 'record_accurate'},
        # Explicitly synthetic hypothetical extension records; not inserted into S3.
        {'id': 'current-asset', 'field_id': 'assets.current_value', 'value': '300000', 'record_confirmation': 'record_accurate'},
        {'id': 'current-debt', 'field_id': 'debt.current_balance', 'value': '60000', 'record_confirmation': 'record_accurate'},
    ]
    state['facts'].extend(copy.deepcopy(records))
    review, inputs = env.service.input_review(state, model)
    d44 = next(row for row in review if row['cell'] == 'D44')
    assert d44['state'] == 'blocked' and 'business_definition_pending' in d44['reasons']
    assert d44['field_id'] is None and 'D44' not in inputs
    assert state['facts'][-4:] == records
    result = env.service.run(env.cid)
    assert result['effective_limit'] is None and result['score'] is None and result['rating'] is None


def test_b02_user_retains_source_reference_without_approving_new_denominator():
    model, _ = load_model()
    decision = next(row for row in model['pending_decisions'] if row['id'] == 'MODEL-B02')
    assert decision['status'] == 'original_reference_retained_pending_explanation'
    assert decision['user_reply'] == '先保留原表规则，稍后说明'
    assert not any(row['id'] == 'MODEL-B02' for row in model['confirmed_decisions'])
    insurance = next(row for row in model['score_indicators'] if row['id'] == 'SCORE-15')
    assert evaluate_indicator(insurance, '0.3')['reference_score'] == '25'
    assert evaluate_indicator(insurance, None)['status'] == 'blocked_missing'
    assert model['effective_enabled'] is False


def test_b01_history_becomes_stale_after_b03_without_rewriting_snapshot(env, monkeypatch):
    import due_app.calculations as module
    current_model, _ = load_model()
    previous = copy.deepcopy(current_model)
    previous['version'] = 'farmer_reference_v1.1_b01'
    previous['confirmed_decisions'] = [row for row in previous['confirmed_decisions'] if row['id'] != 'MODEL-B03']
    previous.pop('confirmed_balance_basis')
    old_b03 = next(row for row in previous['pending_decisions'] if row['id'] == 'MODEL-B03')
    old_b03.update(title='资产、负债及系数口径', status='pending')
    original_loader = module.load_model
    # Controlled previous-model snapshot fixture, not an assertion that the new
    # loader accepts an old package without the new confirmation metadata.
    monkeypatch.setattr(module, 'load_model', lambda: (previous, 'f74bbb4c9c03586465cb2ea47b052004670c19d89725a4a28ea3cb002ae4783d'))
    run = env.service.run(env.cid)
    assert run['stale'] is False
    assert [row['id'] for row in run['confirmed_decisions']] == ['MODEL-B01']
    monkeypatch.setattr(module, 'load_model', original_loader)
    reopened = env.service.run_detail(env.cid, run['id'])
    assert reopened['stale'] is True and reopened['model_version'] == 'farmer_reference_v1.1_b01'
    assert reopened['business_hash'] == run['business_hash'] and reopened['reference'] == run['reference']
    assert [row['id'] for row in reopened['confirmed_decisions']] == ['MODEL-B01']
    assert next(row for row in reopened['pending_decisions'] if row['id'] == 'MODEL-B03')['status'] == 'pending'
    assert env.service.report_context(env.cid)['stale'] is True
    assert next(row for row in env.service.run(env.cid)['confirmed_decisions'] if row['id'] == 'MODEL-B03')['status'] == 'partially_confirmed'
