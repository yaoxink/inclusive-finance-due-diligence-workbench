"""Full assess outputs against pre-frozen source/Fraction threshold expectations.

The fixtures deliberately have no upstream facts and use explicit synthetic
manual input Records. This is arithmetic/qualification coverage, not a source
permission, real-customer, microphone, or business-acceptance test.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
ROOT = APP_ROOT.parent
sys.path.insert(0, str(APP_ROOT))
from due_app.farmer_model_v2 import assess

EVIDENCE = ROOT / 'planning/2026-11-01/implementation/s5-threshold-audit'
MATRIX_PATH = EVIDENCE / 'MATRIX.json'
assert hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest() == '8cadfba0f3107c922553d6e9613e9a111ea17d8987c7d8e9c7bd2ea682d391ab'
MATRIX = json.loads(MATRIX_PATH.read_text(encoding='utf-8'))
CONTRACT = json.loads((EVIDENCE.parent / 'model-v2/model_contract_v2.json').read_text(encoding='utf-8'))
SCHEMA = {r['id']: r for r in CONTRACT['input_schema']}


def period(kind):
    return {'kind': kind, 'label': '独立阈值审计：明确完整会计年度' if kind == 'year' else
            '独立阈值审计：明确截至基准日' if kind == 'as_of' else '独立阈值审计：明确时长',
            'start': '2025-01-01' if kind == 'year' else '2026-10-30' if kind == 'as_of' else None,
            'end': '2025-12-31' if kind == 'year' else '2026-10-30' if kind == 'as_of' else None}


def record(value, unit, subject, kind):
    return {'value': value, 'presence': 'explicit_zero' if value == '0' else 'known',
            'unit': unit, 'subject_id': subject, 'period': period(kind),
            'source_ref': {'kind': 'manual', 'actor': '虚构阈值夹具经办人',
                'text': f'独立冻结纯合成原始值{value}{unit}；主体{subject}；'
                        '存款结息收入是2025同一完整会计年四个季度均值。'},
            'record_confirmation': 'record_accurate',
            'verification': {'state': 'checked_consistent', 'reviewer': '虚构阈值夹具复核人',
                'basis': '仅用于纯函数算术检验的受控合成核验；2025存款结息覆盖完整四季。'}}


def build_inputs(case):
    raw = {'evaluation_as_of': '2026-10-30', 'accounting_period': {'start': '2025-01-01', 'end': '2025-12-31'},
           'household_id': 'C1-HOUSEHOLD',
           'scope_subject_ids': ['C1-APPLICANT', 'C1-HOUSEHOLD', 'C1-MAIN', 'C1-SECONDARY'],
           'scope_basis': '独立阈值测试共同经济单元；按明确份额归入、清单完整无重复、内部转账已排除；纯模拟。',
           'fields': {}, 'assets': [], 'debts': [], 'assets_complete': True, 'debts_complete': True}
    for field, value in case['fields'].items():
        spec = SCHEMA[field]
        subject = ('C1-APPLICANT' if spec['subject_kind'] == 'applicant' else
                   'C1-HOUSEHOLD' if spec['subject_kind'] in {'scope', 'household'} else
                   'C1-SECONDARY' if field.startswith('crop.secondary.') else 'C1-MAIN')
        raw['fields'][field] = record(value, spec['unit'], subject, spec['period_kind'])
    for key in ['assets', 'debts']:
        raw[key] = [{'record_id': r['id'], 'type': r['type'], 'share': r['share'],
                     'amount': record(r['value'], '元', 'C1-HOUSEHOLD', 'as_of')} for r in case[key]]
    invalid = case['invalid_kind']
    if invalid:
        for field in invalid['dependencies']:
            if field.startswith('@assets.'):
                target_type = field.split('.')[-1]
                amount = next(r['amount'] for r in raw['assets'] if r['type'] == target_type)
                amount.update(value=None, presence='missing' if invalid['mode'] == 'missing' else 'not_applicable')
            elif invalid['mode'] == 'missing':
                raw['fields'].pop(field)
            else:
                raw['fields'][field].update(value=None, presence='not_applicable')
    return raw


def controlled_workspace():
    return {'case_id': 'PURE-SYNTHETIC-THRESHOLD-AUDIT', 'facts': [], 'statements': [],
            'previsit': {'state': 'available', 'cached': False, 'data_as_of': '2026-10-30',
                'parties': [{'party_id': 'C1-APPLICANT', 'role': 'applicant'}],
                'households': [{'household_id': 'C1-HOUSEHOLD', 'member_party_ids': ['C1-APPLICANT']}],
                'business_units': [{'business_unit_id': 'C1-MAIN', 'operator_party_id': 'C1-APPLICANT', 'kind': 'main_crop'},
                                   {'business_unit_id': 'C1-SECONDARY', 'operator_party_id': 'C1-APPLICANT', 'kind': 'secondary_crop'}]}}


def exact(actual, expected, field):
    if expected is None:
        assert actual is None, field
    else:
        assert Fraction(actual) == Fraction(expected), (field, actual, expected)


@pytest.mark.parametrize('case', MATRIX['cases'], ids=[c['id'] for c in MATRIX['cases']])
def test_every_frozen_threshold_through_qualified_v2_assess(case):
    result = assess(build_inputs(case), controlled_workspace())
    expected = case['expected']
    assert result['state'] == expected['state'], (case['id'], result['blocking_reasons'])
    assert result['assessment_only'] is True
    assert result['pricing'] == {'state': 'not_configured', 'rate': None}
    if expected['state'] == 'blocked':
        assert result['effective_limit'] is None
        assert result['score'] is None and result['rating'] is None
        assert result['blocking_reasons']
        return
    assert not result['blocking_reasons']
    actual_leaves = {t['step']: t for t in result['trace'] if t['step'].startswith('SCORE-')}
    assert set(actual_leaves) == {e['id'] for e in expected['leafs']}
    assert len(actual_leaves) == 36
    for e in expected['leafs']:
        actual = actual_leaves[e['id']]
        if SCHEMA.get(actual['field'], {}).get('value_type') == 'enum':
            assert actual['input'] == e['input']
        else:
            exact(actual['input'], e['input'], e['id'] + ':input')
        for field in ['score', 'adjustment', 'weighted_score']:
            exact(actual.get(field), e[field], e['id'] + ':' + field)
        assert actual['source'] == e['source'], (e['id'], actual['source'], e['source'])
    full = expected['overall']
    for field, key in [('base_score', 'base_score'), ('score_adjustments', 'adjustments'), ('score', 'score')]:
        exact(result[field], full[key], field)
    assert result['rating'] == full['rating']
    exact(result['rating_coefficient'], full['D46'], 'rating_coefficient')
    assert result['group_scores'].keys() == expected['group_scores'].keys()
    for group, value in expected['group_scores'].items():
        exact(result['group_scores'][group], value, group)
    for key in ['G10', 'G13', 'G16', 'D42', 'D44', 'D46', 'E23', 'E25', 'E28_interest', 'E28_flow', 'E28']:
        exact(result['routes'][key], full[key], key)
    exact(result['aggregates']['asset_total'], full['asset_total'], 'asset_total')
    exact(result['aggregates']['debt_total'], full['debt_total'], 'debt_total')
    exact(result['effective_limit'], full['effective_limit'], 'effective_limit')
    final_steps = {t['step']: t['value'] for t in result['trace'] if t['step'] in {'median', 'adjusted', 'requested_amount_cap'}}
    for step, expected_key in [('median', 'median'), ('adjusted', 'after_area_adjustment'),
                               ('requested_amount_cap', 'after_product_and_request_cap')]:
        exact(final_steps[step], full[expected_key], step)
