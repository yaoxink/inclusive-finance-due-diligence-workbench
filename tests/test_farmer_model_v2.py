"""V2 model boundaries against independently authored literal rule examples."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from due_app.calculations import CalculationService
from due_app.diligence import DiligenceError, DiligenceService
from due_app.farmer_model_v2 import FarmerModelV2Service, assess, example_inputs, finalize_limit, load_model
from due_app.farmer_model_v2_routes import install_farmer_model_v2_routes
from due_app.store import DueStore


@pytest.fixture
def env(tmp_path):
    store = DueStore(tmp_path/'synthetic-v2')
    diligence = DiligenceService(store, SimpleNamespace(get=lambda *args: None))
    legacy = CalculationService(store, diligence)
    app = FastAPI()
    service = install_farmer_model_v2_routes(app, store, diligence, legacy)
    cid = store.create_case('V2 isolated synthetic', 'farmer', 'farmer_demo')['id']
    diligence.load_previsit(cid, 'SYN-S0-001', 'base')
    raw = service.examples(cid)['examples'][0]['inputs']
    return SimpleNamespace(store=store, diligence=diligence, service=service,
                           legacy=legacy, cid=cid, raw=raw, client=TestClient(app))


def calculate(env, raw=None):
    return assess(raw or env.raw, env.diligence.workspace(env.cid))


def persist(env, raw=None):
    revision = env.service.workspace(env.cid)['draft']['revision']
    ws = env.service.save_inputs(env.cid, raw or env.raw, revision)
    return env.service.run(env.cid, ws['draft']['revision'])


def reason(result, expected):
    return any(r['reason'] == expected for r in result['blocking_reasons'])


def test_independent_literal_normal_score_routes_and_final(env):
    # Oracle: independently hand-expanded score groups 23.45+21+12.675+17.875.
    # D42=.4 => .84; AA=.9125; 200000*.84*.9125*2=306600.
    result = calculate(env)
    assert result['state'] == 'valid'
    assert Decimal(result['score']) == Decimal('75')
    assert result['rating'] == 'AA' and result['rating_coefficient'] == '0.9125'
    assert {k: Decimal(v) for k, v in result['group_scores'].items()} == {
        '生产经营能力': Decimal('23.45'), '农户及家庭信息': Decimal('21'),
        '信用状况': Decimal('12.675'), '财务状况': Decimal('17.875')}
    expected = {'G10': '300000', 'G13': '50000', 'G16': '50000', 'D42': '0.4',
                'D44': '0.84', 'D46': '0.9125', 'E23': '306600', 'E25': '68985',
                'E28_interest': '400000', 'E28_flow': '120000', 'E28': '306600'}
    assert {k: Decimal(result['routes'][k]) for k in expected} == {k: Decimal(v) for k, v in expected.items()}
    assert result['effective_limit'] == '306600'
    assert len([t for t in result['trace'] if t['step'].startswith('SCORE-')]) == 36
    assert result['pricing'] == {'state': 'not_configured', 'rate': None}


@pytest.mark.parametrize('field,value,score,limit', [
    ('crop.main.area', '19', '73.6', '151700'),
    ('crop.main.area', '20', '73.6', '153300'),
    ('loan.requested_amount', '250050', '75', '250000'),
])
def test_independent_literal_area_and_request_order(env, field, value, score, limit):
    env.raw['fields'][field]['value'] = value
    result = calculate(env)
    assert result['state'] == 'valid'
    assert Decimal(result['score']) == Decimal(score)
    assert result['effective_limit'] == limit


@pytest.mark.parametrize('ratio,coefficient', [
    ('0', '1'), ('0.3', '1'), ('0.3001', '0.84'), ('0.4', '0.84'),
    ('0.4001', '0.68'), ('0.5', '0.68'), ('0.5001', '0.52'), ('0.6', '0.52'),
    ('0.6001', '0.36'), ('0.7', '0.36'), ('0.7001', '0.2')])
def test_transplanted_d44_all_independent_boundaries(env, ratio, coefficient):
    amount = env.raw['debts'][0]['amount']
    amount['value'] = str(Decimal('1200000')*Decimal(ratio))
    amount['presence'] = 'explicit_zero' if Decimal(ratio) == 0 else 'known'
    result = calculate(env)
    assert result['state'] == 'valid'
    assert result['routes']['D44'] == coefficient


@pytest.mark.parametrize('field', [row['id'] for row in load_model()[0]['input_schema']])
def test_each_active_raw_input_missing_blocks_without_zero_defaults(env, field):
    env.raw['fields'].pop(field)
    result = calculate(env)
    assert result['state'] == 'blocked' and result['effective_limit'] is None
    assert any(r['field'] == field for r in result['blocking_reasons'])


@pytest.mark.parametrize('kind', ['unverified', 'unconfirmed', 'source_missing', 'wrong_unit', 'wrong_period', 'wrong_subject', 'not_zero'])
def test_raw_input_qualification_is_separate_and_explicit(env, kind):
    record = env.raw['fields']['cost.total']
    if kind == 'unverified': record['verification']['state'] = 'not_checked'
    elif kind == 'unconfirmed': record['record_confirmation'] = 'pending'
    elif kind == 'source_missing': record['source_ref'] = {}
    elif kind == 'wrong_unit': record['unit'] = '万元'
    elif kind == 'wrong_period': record['period']['kind'] = 'season'
    elif kind == 'wrong_subject': record['subject_id'] = 'OUT-OF-CASE'
    else: record['presence'] = 'explicit_zero'
    result = calculate(env)
    assert result['state'] == 'blocked' and result['effective_limit'] is None


@pytest.mark.parametrize('field,value,expected', [
    ('loan.requested_amount', '9999', 'below_product_minimum'),
    ('cost.total', '400000', 'nonpositive_operating_profit'),
    ('policy.annual_interest_rate', '0', 'positive_annual_rate_required'),
    ('assessment.risk_review', '未通过', 'risk_review_not_passed'),
    ('household.pension_eligible_count', '0', 'invalid_ratio_denominator_or_numerator')])
def test_independent_block_cases_do_not_emit_minimum_allowance(env, field, value, expected):
    record = env.raw['fields'][field]
    record['value'] = value
    record['presence'] = 'explicit_zero' if value == '0' else 'known'
    result = calculate(env)
    assert result['state'] == 'blocked' and result['effective_limit'] is None
    assert reason(result, expected)


def test_single_quarter_with_annual_label_is_not_complete_four_quarter_evidence(env):
    record = env.raw['fields']['cashflow.quarter_interest']
    record['source_ref']['text'] = '虚构旺季某季度利息300'
    record['verification']['basis'] = '仅核对本季度'
    result = calculate(env)
    assert reason(result, 'four_quarter_evidence_required') and result['effective_limit'] is None


@pytest.mark.parametrize('change', ['duplicate', 'share_missing', 'share_zero', 'wrong_date', 'incomplete', 'zero_assets', 'outside_family'])
def test_ledger_scope_date_share_and_duplicate_boundaries(env, change):
    if change == 'duplicate': env.raw['assets'].append(copy.deepcopy(env.raw['assets'][0]))
    elif change == 'share_missing': env.raw['assets'][0].pop('share')
    elif change == 'share_zero': env.raw['assets'][0]['share'] = '0'
    elif change == 'wrong_date': env.raw['assets'][0]['amount']['period']['end'] = '2026-10-29'
    elif change == 'incomplete': env.raw['debts_complete'] = False
    elif change == 'outside_family': env.raw['scope_subject_ids'].append('C1-THIRD')
    else:
        for entry in env.raw['assets']:
            entry['amount'].update(value='0', presence='explicit_zero')
    result = calculate(env)
    assert result['state'] == 'blocked' and result['effective_limit'] is None


@pytest.mark.parametrize('change', ['omitted', 'no_property_with_positive', 'no_property_with_zero'])
def test_explicit_homestead_status_must_match_complete_ledger(env, change):
    if change == 'omitted':
        env.raw['assets'] = [r for r in env.raw['assets'] if r['type'] != 'homestead']
    else:
        env.raw['fields']['assets.homestead_decoration']['value'] = '明确无自有宅基地房产'
        if change == 'no_property_with_zero':
            env.raw['assets'][0]['amount'].update(value='0', presence='explicit_zero')
    result = calculate(env)
    assert result['state'] == 'blocked' and result['effective_limit'] is None


def test_explicit_absence_derives_zero_but_unknown_is_not_absence(env):
    env.raw['fields']['crop.secondary.present']['value'] = '否'
    env.raw['fields']['insurance.policy_status']['value'] = '明确无政策性保险'
    for key in ('crop.secondary.area', 'crop.secondary.yield', 'crop.secondary.price',
                'insurance.eligible_area', 'insurance.policy_covered_area', 'insurance.policy_continuous_years'):
        env.raw['fields'].pop(key)
    result = calculate(env)
    assert result['state'] == 'valid'
    assert result['routes']['G13'] == '0'
    assert result['derived']['derived.policy_coverage'] == result['derived']['derived.policy_years'] == '0'
    env.raw['fields']['crop.secondary.present'].update(value=None, presence='unknown')
    assert calculate(env)['effective_limit'] is None


def test_same_field_known_conflict_cannot_be_bypassed_by_manual_verification(env):
    workspace = env.diligence.workspace(env.cid)
    conflicting = copy.deepcopy(workspace['facts'][0])
    conflicting.update(field_id='cost.total', conflict=True)
    workspace['facts'].append(conflicting)
    assert reason(assess(env.raw, workspace), 'known_case_source_conflict')
    conflicting['field_id'] = 'unrelated.field'
    assert assess(env.raw, workspace)['state'] == 'valid'


@pytest.mark.parametrize('difference,blocked', [
    ('same_identity_different_value', True), ('label_only_different', True),
    ('same_numeric_value_trailing_zero', False), ('different_year', False),
    ('different_field', False), ('different_subject', False), ('different_unit', False),
    ('unverified', False), ('unconfirmed', False), ('stale', False)])
def test_manual_input_cannot_override_current_verified_fact_identity(env, difference, blocked):
    workspace = env.diligence.workspace(env.cid)
    record = env.raw['fields']['cost.total']
    fact = {'id': 'synthetic-verified-cost', 'field_id': 'cost.total', 'revision': 1,
            'value': '100000', 'unit': record['unit'], 'period': copy.deepcopy(record['period']),
            'subject_id': record['subject_id'], 'presence': 'known',
            'record_confirmation': 'record_accurate', 'verification': 'checked_consistent',
            'needs_review': False, 'conflict': False}
    if difference == 'label_only_different': fact['period']['label'] = '另一个同年度标签'
    elif difference == 'same_numeric_value_trailing_zero': fact['value'] = record['value']+'.00'
    elif difference == 'different_year': fact['period'].update(start='2024-01-01', end='2024-12-31')
    elif difference == 'different_field': fact['field_id'] = 'income.other.net_income'
    elif difference == 'different_subject': fact['subject_id'] = 'C1-APPLICANT'
    elif difference == 'different_unit': fact['unit'] = '万元'
    elif difference == 'unverified': fact['verification'] = 'not_checked'
    elif difference == 'unconfirmed': fact['record_confirmation'] = 'pending'
    elif difference == 'stale': fact['needs_review'] = True
    workspace['facts'].append(fact)
    actual = assess(env.raw, workspace)
    assert reason(actual, 'source_conflict') is blocked
    if blocked:
        assert actual['state'] == 'blocked' and actual['effective_limit'] is None


def test_report_context_exposes_frozen_model_descriptors_without_replacing_raw_snapshot(env):
    run = persist(env)
    context = env.service.report_context(env.cid)
    assert len(context['input_schema']) == 53 and len(context['ratings']) == 9
    assert context['score_bindings']['SCORE-01'] == 'income.planting.annual_receipts'
    assert context['input_snapshot'] == run['input_snapshot']
    assert context['trace'] == run['trace']
    assert context['ratings'][1]['rating'] == 'AA' and context['ratings'][1]['coefficient'] == '0.9125'


def test_explicit_inconsistent_fact_does_not_become_verified_by_new_record(env):
    workspace = env.diligence.workspace(env.cid)
    original = copy.deepcopy(workspace['facts'][0])
    original.update(id='synthetic-inconsistent-age', field_id='operator.age')
    original['verification'] = 'checked_inconsistent'
    workspace['facts'].append(original)
    env.raw['fields']['operator.age'] = {**env.raw['fields']['operator.age'],
        **{k: original.get(k) for k in ('value', 'presence', 'unit', 'period', 'subject_id')},
        'source_ref': {'kind': 'fact', 'fact_id': original['id'], 'revision': original['revision']}}
    result = assess(env.raw, workspace)
    assert reason(result, 'source_unresolved') and result['effective_limit'] is None


def test_draft_revision_cas_immutable_history_replay_and_report_current_only(env):
    first = persist(env)
    again = env.service.run(env.cid, 1)
    assert first['business_hash'] == again['business_hash']
    assert env.legacy.report_context(env.cid)['effective_limit'] == '306600'
    revised = copy.deepcopy(env.raw)
    revised['fields']['cost.total']['verification']['state'] = 'not_checked'
    env.service.save_inputs(env.cid, revised, 1)
    with pytest.raises(DiligenceError) as error:
        env.service.save_inputs(env.cid, env.raw, 1)
    assert error.value.code == 'revision_conflict'
    historical = env.service.run_detail(env.cid, first['id'])
    assert historical['stale'] and historical['effective_limit'] == '306600' and historical['current_effective_limit'] is None
    assert env.legacy.report_context(env.cid)['effective_limit'] is None
    history = env.service.input_history(env.cid)['versions']
    assert [v['revision'] for v in history] == [2, 1]
    assert history[1]['inputs'] == first['input_snapshot']
    reopened = FarmerModelV2Service(DueStore(env.store.root), env.diligence)
    assert reopened.run_detail(env.cid, first['id'])['input_snapshot'] == first['input_snapshot']


def test_source_updates_deny_unavailable_and_cross_case_history(env):
    first = persist(env)
    env.diligence.load_previsit(env.cid, 'SYN-S0-001', 'updated')
    assert env.service.run_detail(env.cid, first['id'])['stale']
    result = env.service.run(env.cid, 1)
    assert result['state'] == 'blocked' and reason(result, 'sources_changed_since_input_save')
    other = env.store.create_case('Other synthetic', 'farmer', 'farmer_demo')['id']
    env.diligence.load_previsit(other, 'SYN-S0-002', 'base')
    with pytest.raises(DiligenceError) as error: env.service.run_detail(other, first['id'])
    assert error.value.status == 404
    env.diligence.load_previsit(env.cid, 'SYN-S0-001', 'denied')
    for action in (lambda: env.service.workspace(env.cid), lambda: env.service.run_detail(env.cid, first['id']),
                   lambda: env.service.examples(env.cid), lambda: env.service.save_inputs(env.cid, env.raw, 1)):
        with pytest.raises(DiligenceError) as denied: action()
        assert denied.value.status == 403
    env.diligence.load_previsit(env.cid, 'SYN-S0-001', 'unavailable')
    with pytest.raises(DiligenceError) as denied: env.service.workspace(env.cid)
    assert denied.value.status == 403


def test_http_strictness_sources_and_no_silent_write_on_examples(env):
    prefix = f'/api/due-diligence/cases/{env.cid}/calculation/v2'
    assert env.client.get(prefix+'/examples').status_code == 200
    assert env.client.get(prefix).json()['draft']['revision'] == 0
    for revision in (True, '0', 0.0):
        assert env.client.put(prefix+'/inputs', json={'expected_revision': revision, 'inputs': env.raw}).status_code == 422
    raw = copy.deepcopy(env.raw); raw['fields']['loan.requested_amount']['value'] = 500000
    assert env.client.put(prefix+'/inputs', json={'expected_revision': 0, 'inputs': raw}).status_code == 422
    raw = copy.deepcopy(env.raw); raw['fields']['derived.debt_asset_ratio'] = raw['fields']['operator.age']
    assert env.client.put(prefix+'/inputs', json={'expected_revision': 0, 'inputs': raw}).status_code == 422
    assert env.client.put(prefix+'/inputs', json={'expected_revision': 0, 'inputs': env.raw}).status_code == 200
    result = env.client.post(prefix+'/run', json={'expected_revision': 1})
    assert result.status_code == 201 and result.json()['state'] == 'valid'


def test_rule_json_cannot_change_without_supported_engine_version(env, monkeypatch, tmp_path):
    import due_app.farmer_model_v2 as module
    model = json.loads(module.MODEL_FILE.read_text(encoding='utf-8'))
    model['quota']['E25'] = 'invented formula'
    altered = tmp_path/'altered-model.json'; altered.write_text(json.dumps(model), encoding='utf-8')
    monkeypatch.setattr(module, 'MODEL_FILE', altered)
    with pytest.raises(ValueError, match='版本不一致'): module.load_model()


@pytest.mark.parametrize('median,area,request_amount,expected', [
    ('12345', '19', '500000', '13500'), ('12345', '20', '500000', '12300'),
    ('2000000', '19', '900000', '900000'), ('5000', '20', '500000', '10000'),
    ('50000', '20', '10050', '10000')])
def test_independent_frozen_final_order_cases(median, area, request_amount, expected):
    answer, _ = finalize_limit([Decimal(median)]*3, Decimal(area), Decimal(request_amount))
    assert answer == Decimal(expected)


@pytest.mark.parametrize('field,value,indicator,score,adjustment', [
    ('operator.experience_years', '0', 'SCORE-09', '0', None),
    ('operator.mobile_months', '0', 'SCORE-16', '0', None),
    ('credit.loan_months', '0', 'SCORE-21', '0', None),
    ('credit.failed_transactions_180d', '0', 'SCORE-24', '100', None),
    ('credit.institutions_90d', '3', 'SCORE-25', '0', None),
    ('credit.institutions_180d', '4', 'SCORE-26', '0', None),
    ('risk.admin_penalties_24m', '0', 'SCORE-35', None, '0'),
    ('risk.tax_penalties_24m', '3', 'SCORE-36', None, '-5'),
    ('operator.agricultural_manager_grade', '无等级', 'SCORE-39', None, '0')])
def test_explicit_v2_gap_patches_preserve_zero_and_adverse_end_semantics(env, field, value, indicator, score, adjustment):
    env.raw['fields'][field].update(value=value, presence='explicit_zero' if value == '0' else 'known')
    result = calculate(env)
    row = next(t for t in result['trace'] if t['step'] == indicator)
    assert row['score'] == score and row['adjustment'] == adjustment


def test_total_positive_adjustments_are_clipped_at_100_without_early_rounding(env):
    best = {'crop.main.area': '60', 'crop.main.yield': '5000', 'income.planting.annual_receipts': '2000000',
        'operator.experience_years': '12', 'business.operating_years': '20', 'household.working_age_count': '4',
        'household.pension_count': '4', 'household.pension_tier': '12', 'credit.successful_repayments': '10',
        'credit.loan_months': '61', 'insurance.policy_covered_area': '40', 'credit.institutions_90d': '0',
        'credit.institutions_180d': '0', 'income.personal_level': 'I', 'assets.homestead_decoration': '精装',
        'operator.village_position': '是', 'income.subsidy_projects_24m': '3', 'operator.agricultural_manager_grade': '高级'}
    for field, value in best.items():
        env.raw['fields'][field].update(value=value, presence='explicit_zero' if value == '0' else 'known')
    env.raw['assets'][1]['amount']['value'] = '1000000'
    env.raw['assets'][2]['amount']['value'] = '300000'
    env.raw['debts'] = []
    result = calculate(env)
    assert Decimal(result['base_score']) == Decimal('100')
    assert result['score_adjustments'] == '40'
    assert result['score'] == '100' and result['rating'] == 'AAA'
