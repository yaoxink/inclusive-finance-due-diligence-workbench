"""Only gaps beyond backend coverage: score clipping, shares and bound source IDs.

Expected arithmetic comes from the separately authored Fraction oracle. The
existing backend suite already covers its literal full/19/20/request fixtures,
all D44 boundaries, all missing fields and main source/period/ledger guards.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from test_farmer_model_v2 import env  # existing tmp_path-only fixture, no hardware
from due_app.farmer_model_v2 import assess


POLICY_DIR = Path(__file__).resolve().parents[2] / 'planning/2026-11-01/implementation/model-v2'
CONTRACT = json.loads((POLICY_DIR / 'model_contract_v2.json').read_text(encoding='utf-8'))
SPEC = importlib.util.spec_from_file_location('independent_fraction_oracle', POLICY_DIR / 'independent_oracle_v2.py')
ORACLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORACLE)


@pytest.fixture
def independent_case(env):
    """Reuse metadata layout only; replace all numeric/category inputs literally."""
    examples = CONTRACT['independent_examples']
    fields = copy.deepcopy(examples['normal_input_values'])
    assets = copy.deepcopy(examples['normal_assets'])
    debts = copy.deepcopy(examples['normal_debts'])
    return env, fields, assets, debts


def production_input(case, fields, assets, debts):
    draft = copy.deepcopy(case.raw)
    for key, value in fields.items():
        record = draft['fields'][key]
        record.update(value=value, presence='explicit_zero' if value == '0' else 'known')
        record['source_ref'] = {'kind': 'manual', 'text': f'独立有理数对照夹具：{key}={value}；四季存款结息覆盖完整同年度。', 'actor': '虚构夹具采集角色'}
        record['verification'] = {'state': 'checked_consistent', 'reviewer': '虚构夹具复核角色（非真人验收）', 'basis': '仅合成算法对照；完整年度四个季度及所列期间/归属明确。'}
    for key in list(draft['fields']):
        if key not in fields:
            draft['fields'].pop(key)
    template = copy.deepcopy(case.raw['assets'][0]['amount'])
    for key, entries in [('assets', assets), ('debts', debts)]:
        draft[key] = []
        for item in entries:
            record = copy.deepcopy(template)
            record.update(value=item['value'], presence='explicit_zero' if item['value'] == '0' else 'known', subject_id=draft['household_id'])
            record['source_ref'] = {'kind': 'manual', 'text': f'独立合成清单：{item["id"]}同日现值/余额{item["value"]}元，归属份额{item["share"]}。', 'actor': '虚构夹具采集角色'}
            record['verification'] = {'state': 'checked_consistent', 'reviewer': '虚构夹具复核角色（非真人验收）', 'basis': '纯合成同日明细与明示份额，无真实客户资料。'}
            draft[key].append({'record_id': item['id'], 'type': item['type'], 'share': item['share'], 'amount': record})
    return draft


def compare(actual, expected):
    assert actual['state'] == 'valid', actual['blocking_reasons']
    # Decimal transport retains source precision/trailing zeros; rational
    # equality is exact here, with no tolerance or rounding allowance.
    numeric_equal = lambda left, right: ORACLE.F(left) == ORACLE.F(right)
    assert numeric_equal(actual['score'], expected['score'])
    assert actual['rating'] == expected['rating']
    assert numeric_equal(actual['effective_limit'], expected['effective_limit'])
    assert numeric_equal(actual['base_score'], expected['base_score'])
    assert numeric_equal(actual['score_adjustments'], expected['adjustments'])
    for key in ['D42', 'D44', 'D46', 'G10', 'G13', 'G16', 'E23', 'E25', 'E28_interest', 'E28_flow', 'E28']:
        assert numeric_equal(actual['routes'][key], expected[key]), key
    assert numeric_equal(actual['aggregates']['asset_total'], expected['asset_total'])
    assert numeric_equal(actual['aggregates']['debt_total'], expected['debt_total'])


def test_production_score_clips_after_full_qualified_adjustments(independent_case):
    case, fields, assets, debts = independent_case
    fields.update({'operator.village_position': '是', 'income.subsidy_projects_24m': '3', 'operator.agricultural_manager_grade': '高级'})
    expected = ORACLE.evaluate(fields, assets, debts)
    assert (expected['base_score'], expected['adjustments'], expected['score'], expected['rating'], expected['effective_limit']) == ('75', '40', '100', 'AAA', '336000')
    compare(assess(production_input(case, fields, assets, debts), case.diligence.workspace(case.cid)), expected)


def test_production_score_lower_clip_and_explicit_zero_end_bands(independent_case):
    case, fields, _, _ = independent_case
    fields.update({
        'crop.main.area': '0', 'crop.main.yield': '0', 'crop.main.price': '0', 'crop.secondary.present': '否',
        'income.other.annual_revenue': '100', 'cost.total': '1', 'income.planting.annual_receipts': '0',
        'business.sales_stability': '无渠道', 'operator.age': '20', 'operator.experience_years': '0',
        'business.operating_years': '0', 'operator.marital_status': '离婚', 'household.working_age_count': '0',
        'household.pension_count': '0', 'household.pension_tier': '0', 'household.medical_count': '0',
        'operator.mobile_months': '0', 'operator.local_registration': '非本地', 'credit.overdue_30d': '1 - 逾期',
        'credit.successful_repayments': '0', 'credit.failed_transactions': '10', 'credit.loan_months': '0',
        'credit.failed_transactions_180d': '40', 'credit.institutions_90d': '5', 'credit.institutions_180d': '5',
        'insurance.policy_status': '明确无政策性保险', 'income.personal_level': 'A',
        'assets.homestead_decoration': '明确无自有宅基地房产', 'household.dependent_count': '4',
        'risk.court_notices_12m': '6', 'risk.enterprise_exposures_12m': '3',
        'risk.admin_penalties_24m': '6', 'risk.tax_penalties_24m': '3',
        'cashflow.quarter_interest': '1', 'policy.annual_interest_rate': '0.01',
        'cashflow.annual_inflow': '100', 'policy.cashflow_margin': '0.1',
    })
    for key in ['crop.secondary.area', 'crop.secondary.yield', 'crop.secondary.price',
                'insurance.eligible_area', 'insurance.policy_covered_area', 'insurance.policy_continuous_years']:
        fields.pop(key)
    assets = [{'id': 'ORACLE-FIN', 'type': 'financial', 'value': '100', 'share': '1'}]
    debts = [{'id': 'ORACLE-DEBT', 'type': 'bank', 'value': '100', 'share': '1'}]
    expected = ORACLE.evaluate(fields, assets, debts)
    assert (expected['base_score'], expected['adjustments'], expected['score'], expected['rating']) == ('5.7125', '-29', '0', 'C')
    # A human risk-pass is an explicit synthetic fixture input. This only checks
    # the adopted arithmetic floor, not whether any real person should get credit.
    assert expected['effective_limit'] == '10000'
    compare(assess(production_input(case, fields, assets, debts), case.diligence.workspace(case.cid)), expected)


def test_fractional_financial_asset_share_drives_D42_D44_and_routes(independent_case):
    case, fields, assets, debts = independent_case
    next(row for row in assets if row['type'] == 'financial')['share'] = '0.5'
    expected = ORACLE.evaluate(fields, assets, debts)
    assert (expected['asset_total'], expected['D42'], expected['D44'], expected['effective_limit']) == ('1000000', '0.48', '0.68', '248200')
    compare(assess(production_input(case, fields, assets, debts), case.diligence.workspace(case.cid)), expected)


def test_existing_case_fact_of_different_field_cannot_support_model_input(independent_case):
    case, fields, assets, debts = independent_case
    workspace = case.diligence.workspace(case.cid)
    foreign_field = next(fact for fact in workspace['facts'] if fact['field_id'] != 'cost.total')
    draft = production_input(case, fields, assets, debts)
    # This is the right case/current revision, but an unrelated fact cannot be
    # used as proof of the annual cost amount by attaching its valid source ID.
    draft['fields']['cost.total']['source_ref'] = {'kind': 'fact', 'fact_id': foreign_field['id'], 'revision': foreign_field['revision']}
    actual = assess(draft, workspace)
    assert actual['state'] == 'blocked' and actual['effective_limit'] is None
    assert any(row['field'] == 'cost.total' and row['reason'] == 'fact_source_field_mismatch' for row in actual['blocking_reasons'])
