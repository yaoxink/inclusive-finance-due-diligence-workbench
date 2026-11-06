"""Immutable, source-linked farmer model assessments for synthetic cases.

The separately versioned optimisation preserves the original v1 reference model.
Values and verification records belong to this assessment; they never rewrite an
upstream fact or promote a transcription confirmation into a factual check.
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
import hashlib
import json
from pathlib import Path
import uuid

from .calculations import (canonical, decimal, digest, number, band_matches,
                           evaluate_indicator, reference_rating, load_model as load_reference_model)
from .diligence import DiligenceError
from .store import now

MODEL_FILE = Path(__file__).parent / 'data' / 'model_farmer_v2.json'
ENGINE_VERSION = 'farmer-assessment-engine/2.0'
SUPPORTED_MODEL_SHA256 = '83520a545a9c54ef4cf3e58cb0cff451b5321be60a798bdd8969f0528db114ae'
INPUT_KEYS = {'evaluation_as_of', 'accounting_period', 'household_id',
              'scope_subject_ids', 'scope_basis', 'fields', 'assets', 'debts',
              'assets_complete', 'debts_complete'}
RECORD_KEYS = {'value', 'presence', 'unit', 'subject_id', 'period', 'source_ref',
               'record_confirmation', 'verification'}
PRESENCE = {'known', 'explicit_zero', 'unknown', 'missing', 'not_applicable'}


def fail(status, code, message):
    raise DiligenceError(status, code, message)


def load_model():
    raw = MODEL_FILE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SUPPORTED_MODEL_SHA256:
        raise ValueError('模型规则与本引擎已验证版本不一致，停止测算。')
    value = json.loads(raw)
    if value.get('schema') != 'farmer-assessment-model/2' or value.get('engine_version') != ENGINE_VERSION:
        raise ValueError('不支持的农户模型版本。')
    _, reference_hash = load_reference_model()
    if value.get('reference_model_sha256') != reference_hash:
        raise ValueError('原评分表已变化，请为新规则发布明确模型版本，不能静默沿用旧引擎。')
    return value, hashlib.sha256(raw).hexdigest()


def empty_inputs():
    return {'evaluation_as_of': '', 'accounting_period': {'start': '', 'end': ''},
            'household_id': '', 'scope_subject_ids': [], 'scope_basis': '',
            'fields': {}, 'assets': [], 'debts': [],
            'assets_complete': False, 'debts_complete': False}


def subjects_for(workspace):
    previsit = workspace['previsit']
    households = previsit.get('households', [])
    applicant = next((party['party_id'] for party in previsit.get('parties', [])
                      if party.get('role') == 'applicant'), None)
    result = []
    for party in previsit.get('parties', []):
        pid = party['party_id']
        result.append({'id': pid, 'label': party.get('display_name') or party.get('synthetic_label') or party.get('label') or pid,
                       'kind': 'applicant' if pid == applicant else 'party'})
    for household in households:
        hid = household['household_id']
        result.append({'id': hid, 'label': household.get('label') or hid, 'kind': 'household',
                       'member_party_ids': household.get('member_party_ids', [])})
    for unit in previsit.get('business_units', []):
        uid = unit['business_unit_id']
        result.append({'id': uid, 'label': unit.get('label') or uid, 'kind': 'business',
                       'business_kind': unit.get('kind'), 'operator_party_id': unit.get('operator_party_id')})
    return result


def source_fingerprint(workspace):
    return digest({'case_id': workspace['case_id'], 'previsit': workspace['previsit'],
                   'questionnaire': workspace['questionnaire'],
                   'facts': workspace['facts'], 'statements': workspace['statements']})


def validate_record(record):
    if not isinstance(record, dict) or set(record) - RECORD_KEYS:
        raise ValueError('输入记录含未知字段或格式不正确。')
    if record.get('presence', 'missing') not in PRESENCE:
        raise ValueError('输入记录的完整性状态不正确。')
    if record.get('value') is not None and not isinstance(record['value'], str):
        raise ValueError('原始输入必须为字符串，未知值使用null。')
    for key in ('unit', 'subject_id'):
        if key in record and not isinstance(record[key], str):
            raise ValueError('单位和主体必须使用字符串。')
    period = record.get('period', {})
    if not isinstance(period, dict) or set(period) - {'kind', 'start', 'end', 'label'}:
        raise ValueError('期间格式不正确。')
    if any(value is not None and not isinstance(value, str) for value in period.values()):
        raise ValueError('期间必须为字符串或null。')
    if record.get('record_confirmation', 'pending') not in {'pending', 'record_accurate'}:
        raise ValueError('记录准确状态不正确。')
    source = record.get('source_ref', {})
    if not isinstance(source, dict) or set(source) - {'kind', 'fact_id', 'statement_id', 'revision', 'text', 'actor'}:
        raise ValueError('来源引用格式不正确。')
    if source.get('kind') not in {None, 'manual', 'fact', 'statement'}:
        raise ValueError('不支持的来源类型。')
    if 'revision' in source and (type(source['revision']) is not int or source['revision'] < 1):
        raise ValueError('来源修订必须为正整数。')
    if any(key != 'revision' and not isinstance(value, str) for key, value in source.items()):
        raise ValueError('来源引用内容必须为字符串。')
    verification = record.get('verification', {})
    if not isinstance(verification, dict) or set(verification) - {'state', 'reviewer', 'basis'}:
        raise ValueError('独立核验记录格式不正确。')
    if verification.get('state', 'not_checked') not in {'not_checked', 'checked_consistent'}:
        raise ValueError('核验状态不正确。')
    if any(not isinstance(value, str) for value in verification.values()):
        raise ValueError('核验信息必须为字符串。')
    if len(canonical(record)) > 16000:
        raise ValueError('单项输入记录过长。')


def validate_inputs(inputs, model):
    if not isinstance(inputs, dict) or set(inputs) - INPUT_KEYS:
        raise ValueError('测算输入包含未知顶层字段。')
    if len(canonical(inputs)) > 600000:
        raise ValueError('测算输入超出本版大小限制。')
    fields = inputs.get('fields', {})
    allowed = {row['id'] for row in model['input_schema'] if not row.get('derived')}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValueError('包含未知或不允许手填的推导字段。')
    for record in fields.values():
        validate_record(record)
    for key in ('evaluation_as_of', 'household_id', 'scope_basis'):
        if key in inputs and not isinstance(inputs[key], str):
            raise ValueError('基准日、家庭和范围依据必须为字符串。')
    period = inputs.get('accounting_period', {})
    if not isinstance(period, dict) or set(period) - {'start', 'end'} or any(not isinstance(v, str) for v in period.values()):
        raise ValueError('会计期间格式不正确。')
    ids = inputs.get('scope_subject_ids', [])
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) > 100:
        raise ValueError('汇总主体范围格式不正确。')
    for key in ('assets_complete', 'debts_complete'):
        if key in inputs and type(inputs[key]) is not bool:
            raise ValueError('明细完整性必须明确选择。')
    for key in ('assets', 'debts'):
        entries = inputs.get(key, [])
        if not isinstance(entries, list) or len(entries) > 200:
            raise ValueError('明细条数超出本版范围。')
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {'record_id', 'type', 'share', 'amount'}:
                raise ValueError('资产或负债明细格式不正确。')
            if any(k in entry and not isinstance(entry[k], str) for k in ('record_id', 'type', 'share')):
                raise ValueError('明细编号、类型和份额必须为字符串。')
            validate_record(entry.get('amount', {}))
    return copy.deepcopy(inputs)


def _scope(inputs, workspace):
    reasons, available = [], subjects_for(workspace)
    by_id = {row['id']: row for row in available}
    household = by_id.get(inputs.get('household_id'))
    applicant = next((row['id'] for row in available if row['kind'] == 'applicant'), None)
    if (household is None or household['kind'] != 'household'
            or applicant not in household.get('member_party_ids', [])):
        reasons.append('household_scope_required')
        allowed = set()
    else:
        members = set(household['member_party_ids'])
        allowed = {household['id']} | members | {row['id'] for row in available
            if row['kind'] == 'business' and row.get('operator_party_id') in members}
    selected = inputs.get('scope_subject_ids', [])
    if (not selected or len(set(selected)) != len(selected) or not set(selected).issubset(allowed)
            or applicant not in selected or inputs.get('household_id') not in selected):
        reasons.append('scope_subjects_invalid')
    if not inputs.get('scope_basis', '').strip():
        reasons.append('scope_basis_required')
    if workspace['previsit']['state'] != 'available' or workspace['previsit'].get('cached'):
        reasons.append('current_previsit_required')
    try:
        baseline = date.fromisoformat(inputs.get('evaluation_as_of', ''))
        period = inputs.get('accounting_period', {})
        start, end = date.fromisoformat(period.get('start', '')), date.fromisoformat(period.get('end', ''))
        if start > end or not 364 <= (end-start).days <= 365 or end > baseline:
            reasons.append('complete_accounting_year_required')
        if workspace['previsit'].get('data_as_of') != baseline.isoformat():
            reasons.append('baseline_not_current')
    except (TypeError, ValueError):
        reasons.append('accounting_dates_invalid')
    return reasons, by_id, applicant


def _record_reasons(record, spec, inputs, workspace, subjects, applicant):
    reasons = []
    if not record or record.get('presence') not in {'known', 'explicit_zero'}:
        return ['missing_or_unknown']
    value = record.get('value')
    if record.get('record_confirmation') != 'record_accurate':
        reasons.append('record_not_confirmed')
    verification = record.get('verification', {})
    if (verification.get('state') != 'checked_consistent'
            or not verification.get('reviewer', '').strip() or not verification.get('basis', '').strip()):
        reasons.append('explicit_verification_required')
    if record.get('unit') != spec['unit']:
        reasons.append('unit_mismatch')
    subject = record.get('subject_id')
    kind = spec.get('subject_kind', 'scope')
    if subject not in inputs.get('scope_subject_ids', []) or subject not in subjects:
        reasons.append('subject_out_of_scope')
    elif ((kind == 'applicant' and subject != applicant)
          or (kind == 'household' and subject != inputs.get('household_id'))
          or (kind == 'business' and subjects[subject]['kind'] != 'business')):
        reasons.append('subject_kind_mismatch')
    if subject in subjects and kind == 'business':
        if spec['id'].startswith('crop.main.') and subjects[subject].get('business_kind') != 'main_crop':
            reasons.append('main_business_required')
        if spec['id'].startswith('crop.secondary.') and subjects[subject].get('business_kind') != 'secondary_crop':
            reasons.append('secondary_business_required')
    period, expected = record.get('period') or {}, spec['period_kind']
    if period.get('kind') != expected or not period.get('label', '').strip():
        reasons.append('period_kind_mismatch')
    if expected == 'as_of':
        if period.get('start') != inputs.get('evaluation_as_of') or period.get('end') != inputs.get('evaluation_as_of'):
            reasons.append('as_of_mismatch')
    elif expected == 'year':
        if any(period.get(key) != inputs.get('accounting_period', {}).get(key) for key in ('start', 'end')):
            reasons.append('accounting_period_mismatch')
    elif expected == 'duration' and (period.get('start') is not None or period.get('end') is not None):
        reasons.append('duration_period_required')
    source = record.get('source_ref') or {}
    dependency_fields = set(spec.get('source_field_ids', [spec['id']]))
    if spec['id'] == 'cashflow.quarter_interest':
        dependency_fields.add('cashflow.quarterly_interest')
    if any(fact.get('field_id') in dependency_fields and
           (fact.get('conflict') or fact.get('verification') in {'checked_inconsistent', 'checked_conflict'})
           for fact in workspace['facts'] if fact.get('record_confirmation') != 'revoked'):
        reasons.append('known_case_source_conflict')
    # A second manual/statement assessment is not a resolution event for an
    # already verified case fact. Compare the exact economic identity, while
    # deliberately ignoring the free-form period label: relabelling one year
    # must not hide a contradictory amount, and a different year must not be
    # treated as a conflict.
    if source.get('kind') in {'manual', 'statement'}:
        period_key = lambda item: tuple((item.get('period') or {}).get(k) for k in ('kind', 'start', 'end'))
        for fact in workspace['facts']:
            if (fact.get('field_id') != spec['id'] or fact.get('subject_id') != subject
                    or fact.get('unit') != record.get('unit')
                    or period_key(fact) != period_key(record)
                    or fact.get('needs_review') or fact.get('record_confirmation') != 'record_accurate'
                    or fact.get('verification') != 'checked_consistent'
                    or fact.get('presence') not in {'known', 'explicit_zero'}):
                continue
            numeric = (spec.get('value_type') or spec.get('type')) in {'integer', 'decimal'}
            try:
                equal = decimal(fact.get('value')) == decimal(value) if numeric else fact.get('value') == value
            except ValueError:
                equal = False
            if not equal:
                reasons.append('source_conflict')
                break
    source_kind = source.get('kind')
    if source_kind == 'manual':
        if not source.get('text', '').strip() or not source.get('actor', '').strip():
            reasons.append('manual_source_required')
    elif source_kind == 'fact':
        fact = next((item for item in workspace['facts'] if item['id'] == source.get('fact_id')), None)
        if fact is None:
            reasons.append('source_not_in_case')
        elif fact['revision'] != source.get('revision') or fact.get('needs_review'):
            reasons.append('source_changed')
        else:
            if (fact.get('conflict') or fact.get('record_confirmation') != 'record_accurate'
                    or fact.get('verification') in {'checked_inconsistent', 'checked_conflict'}):
                reasons.append('source_unresolved')
            allowed_fields = spec.get('source_field_ids', [spec['id']])
            if fact.get('field_id') not in allowed_fields:
                reasons.append('fact_source_field_mismatch')
            if any(fact.get(key) != record.get(key) for key in ('value', 'presence', 'unit', 'subject_id', 'period')):
                reasons.append('fact_source_value_mismatch')
    elif source_kind == 'statement':
        statement = next((item for item in workspace['statements'] if item['id'] == source.get('statement_id')), None)
        if statement is None:
            reasons.append('source_not_in_case')
        elif statement['revision'] != source.get('revision') or statement.get('needs_review'):
            reasons.append('source_changed')
        elif statement.get('record_confirmation') != 'record_accurate':
            reasons.append('source_not_confirmed')
    else:
        reasons.append('source_required')
    value_type = spec.get('value_type') or spec.get('type')
    if value_type in {'integer', 'decimal'}:
        try:
            numeric = decimal(value)
            if value_type == 'integer' and numeric != numeric.to_integral_value():
                reasons.append('integer_required')
            if spec.get('minimum') is not None and numeric < decimal(str(spec['minimum'])):
                reasons.append('below_minimum')
            if spec.get('maximum') is not None and numeric > decimal(str(spec['maximum'])):
                reasons.append('above_maximum')
            if record.get('presence') == 'explicit_zero' and numeric != 0:
                reasons.append('explicit_zero_mismatch')
        except ValueError:
            reasons.append('invalid_decimal')
    elif spec.get('options') and value not in [option.get('value') if isinstance(option, dict) else option for option in spec['options']]:
        reasons.append('invalid_option')
    if record.get('presence') == 'explicit_zero' and value_type not in {'integer', 'decimal'}:
        reasons.append('explicit_zero_not_numeric')
    return list(dict.fromkeys(reasons))


def finalize_limit(routes, main_area, request):
    """Apply the frozen order only after every route has qualified."""
    if len(routes) != 3 or any(value < 0 or not value.is_finite() for value in routes):
        raise ValueError('三路额度必须齐全、非负且有限。')
    if request < 10000:
        raise ValueError('申请金额低于产品下限。')
    median = sorted(routes)[1]
    factor = Decimal('1.1') if main_area < 20 else Decimal(1)
    adjusted = median*factor
    product = min(max(adjusted, Decimal(10000)), Decimal(1000000))
    capped = min(product, request)
    final = capped.quantize(Decimal('1E2'), rounding=ROUND_DOWN)
    return final, [{'step': 'median', 'value': number(median)},
                   {'step': 'under_20_mu_factor', 'value': number(factor)},
                   {'step': 'adjusted', 'value': number(adjusted)},
                   {'step': 'product_bounds', 'value': number(product)},
                   {'step': 'requested_amount_cap', 'value': number(capped)},
                   {'step': 'final_truncate_100', 'value': number(final)}]


def assess(inputs, workspace, model=None):
    """Pure deterministic assessment; no persistence, I/O, LLM or approval."""
    model = model or load_model()[0]
    validate_inputs(inputs, model)
    scope_reasons, subjects, applicant = _scope(inputs, workspace)
    blocks = [{'field': 'scope', 'reason': reason} for reason in scope_reasons]
    reviews, values, trace = [], {}, []
    fields = inputs.get('fields', {})
    secondary = fields.get('crop.secondary.present', {}).get('value')
    policy_status = fields.get('insurance.policy_status', {}).get('value')
    for spec in model['input_schema']:
        field = spec['id']
        active = spec.get('required', True)
        if field in {'crop.secondary.area', 'crop.secondary.yield', 'crop.secondary.price'}:
            active = secondary == '是'
        if field in {'insurance.eligible_area', 'insurance.policy_covered_area', 'insurance.policy_continuous_years'}:
            active = policy_status == '有政策性保险'
        record = fields.get(field)
        if not active and (not record or record.get('presence') in {'missing', 'not_applicable'}):
            reviews.append({'field': field, 'state': 'not_applicable', 'reasons': []})
            continue
        reasons = _record_reasons(record, spec, inputs, workspace, subjects, applicant)
        if reasons:
            blocks.extend({'field': field, 'reason': reason} for reason in reasons)
        else:
            values[field] = record['value']
        reviews.append({'field': field, 'state': 'blocked' if reasons else 'qualified', 'reasons': reasons,
                        'source_ref': copy.deepcopy(record.get('source_ref')) if record else None})
    # A qualified absence source may not be contradicted by hidden positive
    # component records. Conditional omissions are different from unknowns.
    if secondary == '否':
        for field in ('crop.secondary.area', 'crop.secondary.yield', 'crop.secondary.price'):
            if field in values and decimal(values[field]) != 0:
                blocks.append({'field': field, 'reason': 'absence_conflicts_with_positive_input'})
    if policy_status == '明确无政策性保险':
        for field in ('insurance.policy_covered_area', 'insurance.policy_continuous_years'):
            if field in values and decimal(values[field]) != 0:
                blocks.append({'field': field, 'reason': 'absence_conflicts_with_positive_input'})
    aggregates = {'asset_total': Decimal(0), 'debt_total': Decimal(0),
                  'fixed_assets': Decimal(0), 'machinery_value': Decimal(0)}
    record_ids = set()
    ledger_trace = []
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        for kind in ('assets', 'debts'):
            if inputs.get(kind+'_complete') is not True:
                blocks.append({'field': kind, 'reason': 'ledger_completeness_not_confirmed'})
            types = model['assets_and_debts']['asset_types' if kind == 'assets' else 'debt_types']
            for index, entry in enumerate(inputs.get(kind, [])):
                field = f'{kind}[{index}]'
                reasons = []
                record_id = entry.get('record_id')
                if not record_id or not record_id.strip() or record_id in record_ids:
                    reasons.append('duplicate_or_missing_record_id')
                record_ids.add(record_id)
                if entry.get('type') not in types:
                    reasons.append('ledger_type_invalid')
                try:
                    share = decimal(entry.get('share'))
                    if share <= 0 or share > 1:
                        reasons.append('ownership_share_invalid')
                except ValueError:
                    reasons.append('ownership_share_invalid')
                    share = Decimal(0)
                record = entry.get('amount', {})
                spec = {'id': field, 'unit': '元', 'period_kind': 'as_of',
                        'subject_kind': 'scope', 'value_type': 'decimal', 'minimum': '0',
                        'source_field_ids': ['assets.'+str(entry.get('type'))+'.current_value'] if kind == 'assets'
                                            else ['debts.'+str(entry.get('type'))+'.current_balance']}
                reasons.extend(_record_reasons(record, spec, inputs, workspace, subjects, applicant))
                weighted = None
                if not reasons:
                    weighted = decimal(record['value'])*share
                    aggregates['asset_total' if kind == 'assets' else 'debt_total'] += weighted
                    if kind == 'assets' and entry['type'] in {'residence', 'vehicle', 'machinery', 'other_fixed'}:
                        aggregates['fixed_assets'] += weighted
                    if kind == 'assets' and entry['type'] == 'machinery':
                        aggregates['machinery_value'] += weighted
                blocks.extend({'field': field, 'reason': reason} for reason in reasons)
                ledger_trace.append({'record_id': record_id, 'kind': kind, 'type': entry.get('type'),
                    'share': entry.get('share'), 'raw_value': record.get('value'),
                    'weighted_value': number(weighted) if weighted is not None else None,
                    'source_ref': copy.deepcopy(record.get('source_ref')), 'reasons': reasons})
        base = {'state': 'blocked', 'score': None, 'rating': None, 'rating_coefficient': None,
                'effective_limit': None, 'pricing': {'state': 'not_configured', 'rate': None},
                'blocking_reasons': blocks, 'input_review': reviews, 'ledger_trace': ledger_trace,
                'trace': trace, 'assessment_only': True,
                'note': '合成案件的模型辅助测算；人员核验为本次显式记录，不构成银行审批。'}
        if blocks:
            return base
        has_homestead = any(entry.get('type') == 'homestead' for entry in inputs.get('assets', []))
        decoration = values.get('assets.homestead_decoration')
        if decoration in {'精装', '简装', '毛坯'} and not has_homestead:
            blocks.append({'field': 'assets.homestead_decoration', 'reason': 'homestead_missing_from_complete_ledger'})
        if decoration == '明确无自有宅基地房产' and has_homestead:
            blocks.append({'field': 'assets.homestead_decoration', 'reason': 'homestead_absence_conflicts_with_ledger'})
        D = lambda key: decimal(values[key])
        def ratio(label, numerator, denominator):
            n, d = D(numerator), D(denominator)
            if d <= 0 or n > d:
                blocks.append({'field': label, 'reason': 'invalid_ratio_denominator_or_numerator'})
                return None
            return n/d
        derived = {}
        for key, numerator, denominator in (
            ('derived.working_age_ratio', 'household.working_age_count', 'household.member_count'),
            ('derived.pension_rate', 'household.pension_count', 'household.pension_eligible_count'),
            ('derived.medical_rate', 'household.medical_count', 'household.medical_eligible_count')):
            derived[key] = ratio(key, numerator, denominator)
        for key in ('household.pension_eligible_count', 'household.medical_eligible_count', 'household.dependent_count'):
            if D(key) > D('household.member_count'):
                blocks.append({'field': key, 'reason': 'count_exceeds_household'})
        if D('household.pension_count') == 0 and D('household.pension_tier') != 0:
            blocks.append({'field': 'household.pension_tier', 'reason': 'pension_tier_without_insured_person'})
        if D('household.pension_count') > 0 and D('household.pension_tier') <= 0:
            blocks.append({'field': 'household.pension_tier', 'reason': 'pension_tier_required'})
        if policy_status == '明确无政策性保险':
            derived['derived.policy_coverage'], derived['derived.policy_years'] = Decimal(0), Decimal(0)
        else:
            derived['derived.policy_coverage'] = ratio('derived.policy_coverage', 'insurance.policy_covered_area', 'insurance.eligible_area')
            derived['derived.policy_years'] = D('insurance.policy_continuous_years')
        if aggregates['asset_total'] <= 0:
            blocks.append({'field': 'assets', 'reason': 'positive_total_assets_required'})
        else:
            derived['derived.debt_asset_ratio'] = aggregates['debt_total']/aggregates['asset_total']
        derived['derived.fixed_assets_excluding_homestead'] = aggregates['fixed_assets']
        derived['derived.machinery_value'] = aggregates['machinery_value']
        g10 = D('crop.main.area')*D('crop.main.yield')*D('crop.main.price')
        g13 = Decimal(0) if secondary == '否' else D('crop.secondary.area')*D('crop.secondary.yield')*D('crop.secondary.price')
        g16 = D('income.other.annual_revenue')
        derived['derived.planting_revenue'] = g10+g13
        # All factors of one crop must describe that same explicitly selected
        # operation; being two businesses in the same family is insufficient.
        for prefix in ('crop.main.', 'crop.secondary.'):
            keys = [prefix+suffix for suffix in ('area', 'yield', 'price') if prefix+suffix in values]
            if len({fields[key]['subject_id'] for key in keys}) > 1:
                blocks.append({'field': prefix, 'reason': 'route_subject_mismatch'})
        if blocks:
            return base
        reference, _ = load_reference_model()
        score_values = {**values, **{key: number(value) for key, value in derived.items()}}
        selected_scale = model['scale']['indicator_by_category'][values['crop.main.scale_category']]
        score_values['derived.main_area'] = values['crop.main.area']
        score_bindings = {**model['score_bindings'], selected_scale: 'derived.main_area'}
        totals, adjustment = {}, Decimal(0)
        for original in reference['score_indicators']:
            if original['id'] not in score_bindings:
                continue
            row = copy.deepcopy(original)
            for patch in model['score']['gap_patches']:
                if patch['indicator'] == row['id']:
                    row['bins'].append({'condition': patch['condition'], 'score': patch.get('score'),
                        'adjustment': patch.get('adjustment'), 'source': 'v2-optimization:'+patch['reason']})
            source_field = score_bindings[row['id']]
            value = score_values[source_field]
            evaluated = evaluate_indicator(row, value)
            if evaluated['status'] != 'reference_only':
                blocks.append({'field': row['id'], 'reason': evaluated['status']})
                continue
            if row['weight'] is not None:
                weighted = decimal(evaluated['reference_score'])*decimal(row['weight'])*decimal(row['group_weight'])
                totals[row['group']] = totals.get(row['group'], Decimal(0))+weighted
                evaluated['weighted_score'] = number(weighted)
            else:
                adjustment += decimal(evaluated['reference_adjustment'])
            trace.append({'step': row['id'], 'field': source_field, 'label': row['label'],
                          'input': value, 'source': evaluated.get('source'),
                          'score': evaluated.get('reference_score'), 'adjustment': evaluated.get('reference_adjustment'),
                          'weighted_score': evaluated.get('weighted_score'), 'condition': evaluated.get('condition')})
        if blocks:
            return base
        base_score = sum(totals.values(), Decimal(0))
        score = min(Decimal(100), max(Decimal(0), base_score+adjustment))
        rating = reference_rating(number(score), reference)
        if rating['state'] != 'reference_only':
            blocks.append({'field': 'rating', 'reason': 'rating_band_not_unique'})
            return base
        d46 = decimal(rating['coefficient'])
        debt_ratio = derived['derived.debt_asset_ratio']
        d44 = next(decimal(row['coefficient']) for row in model['derivations']['D44']['bands']
                   if ('upper' in row and debt_ratio <= decimal(row['upper']))
                      or ('lower_exclusive' in row and debt_ratio > decimal(row['lower_exclusive'])))
        net_profit = g10+g13+g16-D('cost.total')
        e23 = net_profit*d44*d46*2
        e25 = (g10*Decimal('.20')+g13*Decimal('.20')+g16*Decimal('.40'))*d44*d46
        annual_rate = D('policy.annual_interest_rate')
        if annual_rate <= 0:
            blocks.append({'field': 'policy.annual_interest_rate', 'reason': 'positive_annual_rate_required'})
        if values['assessment.risk_review'] != '通过':
            blocks.append({'field': 'assessment.risk_review', 'reason': 'risk_review_not_passed'})
        if net_profit <= 0:
            blocks.append({'field': 'cost.total', 'reason': 'nonpositive_operating_profit'})
        # A four-quarter coverage assertion is a separate evidence requirement;
        # a single quarter amount with an annual date label cannot satisfy it.
        interest_record = fields['cashflow.quarter_interest']
        interest_evidence = ' '.join([interest_record.get('source_ref', {}).get('text', ''),
                                      interest_record.get('verification', {}).get('basis', '')])
        if not any(marker in interest_evidence for marker in ('四季', '四个季度', '4季', '4个季度', 'four quarter', 'four-quarter')):
            blocks.append({'field': 'cashflow.quarter_interest', 'reason': 'four_quarter_evidence_required'})
        e28_interest = D('cashflow.quarter_interest')*4/annual_rate if annual_rate > 0 else None
        e28_flow = D('cashflow.annual_inflow')*D('policy.cashflow_margin')*D('loan.term')/12
        e28 = max(e28_interest, e28_flow)*d44*d46 if e28_interest is not None else None
        calculations = {'G10': g10, 'G13': g13, 'G16': g16, 'D42': debt_ratio, 'D44': d44,
                        'D46': d46, 'E23': e23, 'E25': e25, 'E28_interest': e28_interest,
                        'E28_flow': e28_flow, 'E28': e28}
        trace.extend({'step': cell, 'value': number(value) if value is not None else None,
                      'formula': model['quota'].get(cell) or model['derivations'].get(cell),
                      'state': 'calculated' if value is not None else 'blocked'} for cell, value in calculations.items())
        if any(value is not None and value < 0 for value in (e23, e25, e28)):
            blocks.append({'field': 'quota', 'reason': 'negative_route'})
        request = D('loan.requested_amount')
        if request < 10000:
            blocks.append({'field': 'loan.requested_amount', 'reason': 'below_product_minimum'})
        final = None
        if not blocks:
            final, final_trace = finalize_limit([e23, e25, e28], D('crop.main.area'), request)
            trace.extend(final_trace)
            if final < 10000:
                blocks.append({'field': 'quota', 'reason': 'below_product_minimum'})
                final = None
        return {**base, 'state': 'valid' if not blocks else 'blocked',
                'score': number(score), 'rating': rating['rating'], 'rating_coefficient': number(d46),
                'effective_limit': number(final) if final is not None else None,
                'base_score': number(base_score), 'score_adjustments': number(adjustment),
                'group_scores': {key: number(value) for key, value in totals.items()},
                'derived': {key: number(value) for key, value in derived.items()},
                'aggregates': {key: number(value) for key, value in aggregates.items()},
                'routes': {key: number(value) if value is not None else None for key, value in calculations.items()}}


def example_inputs(workspace, model=None):
    """Explicit fiction for the testing UI; never written to case facts."""
    model = model or load_model()[0]
    subjects = subjects_for(workspace)
    applicant = next((s['id'] for s in subjects if s['kind'] == 'applicant'), None)
    household = next((s['id'] for s in subjects if s['kind'] == 'household'
                      and applicant in s.get('member_party_ids', [])), None)
    main = next((s['id'] for s in subjects if s.get('business_kind') == 'main_crop'), None)
    secondary = next((s['id'] for s in subjects if s.get('business_kind') == 'secondary_crop'), None)
    if not applicant or not household or not main:
        return []
    members = set(next(s['member_party_ids'] for s in subjects if s['id'] == household))
    included_subjects = [s['id'] for s in subjects if s['id'] == household or s['id'] in members
                         or s.get('operator_party_id') in members]
    baseline = workspace['previsit'].get('data_as_of') or '2026-10-30'
    year = date.fromisoformat(baseline).year-1
    inputs = empty_inputs()
    inputs.update(evaluation_as_of=baseline, accounting_period={'start': f'{year}-01-01', 'end': f'{year}-12-31'},
                  household_id=household, scope_subject_ids=included_subjects,
                  scope_basis='虚构演示：本案明确共同经营家庭经济单元，按所列份额归入；资产债务已完整核对、无重复，流水已剔除内部转账。',
                  assets_complete=True, debts_complete=True)
    preset = {
        'crop.main.scale_category': 'fruit', 'crop.main.area': '30', 'crop.main.yield': '4000', 'crop.main.price': '2',
        'crop.secondary.present': '是' if secondary else '否', 'crop.secondary.area': '5',
        'crop.secondary.yield': '3000', 'crop.secondary.price': '2',
        'income.other.annual_revenue': '30000', 'cost.total': '100000',
        'cashflow.quarter_interest': '300', 'cashflow.annual_inflow': '300000',
        'loan.term': '12', 'policy.cashflow_margin': '0.2', 'policy.annual_interest_rate': '0.03',
        'loan.requested_amount': '200000', 'income.planting.annual_receipts': '270000',
        'business.sales_stability': '固定合作', 'operator.age': '42', 'operator.experience_years': '12',
        'business.operating_years': '10', 'operator.marital_status': '已婚',
        'household.member_count': '4', 'household.working_age_count': '3',
        'household.pension_eligible_count': '4', 'household.pension_count': '3', 'household.pension_tier': '5',
        'household.medical_eligible_count': '4', 'household.medical_count': '4',
        'operator.mobile_months': '36', 'operator.local_registration': '本地',
        'credit.overdue_30d': '0 - 未逾期', 'credit.successful_repayments': '10',
        'credit.failed_transactions': '0', 'credit.loan_months': '36',
        'insurance.policy_status': '有政策性保险', 'insurance.eligible_area': '35',
        'insurance.policy_covered_area': '35', 'insurance.policy_continuous_years': '3',
        'credit.failed_transactions_180d': '0', 'credit.institutions_90d': '0', 'credit.institutions_180d': '0',
        'income.personal_level': 'I', 'assets.homestead_decoration': '精装', 'household.dependent_count': '0',
        'risk.court_notices_12m': '0', 'risk.enterprise_exposures_12m': '0',
        'risk.admin_penalties_24m': '0', 'risk.tax_penalties_24m': '0', 'operator.village_position': '否',
        'income.subsidy_projects_24m': '0', 'operator.agricultural_manager_grade': '无等级',
        'assessment.risk_review': '通过'}
    independent = model.get('independent_examples', {})
    if independent.get('normal_input_values'):
        preset = copy.deepcopy(independent['normal_input_values'])
        if 'main_crop' in preset:
            preset['crop.main.scale_category'] = preset.pop('main_crop')
        if not secondary:
            preset['crop.secondary.present'] = '否'
    def record(value, unit, subject_id, kind):
        period = {'kind': kind, 'label': '虚构明确完整会计年度' if kind == 'year' else '虚构明确截至基准日' if kind == 'as_of' else '虚构明确时长',
                  'start': inputs['accounting_period']['start'] if kind == 'year' else baseline if kind == 'as_of' else None,
                  'end': inputs['accounting_period']['end'] if kind == 'year' else baseline if kind == 'as_of' else None}
        return {'value': value, 'presence': 'explicit_zero' if value == '0' else 'known', 'unit': unit,
                'subject_id': subject_id, 'period': period,
                'source_ref': {'kind': 'manual', 'text': '虚构合成资料：'+value+' '+unit+'；存款结息为完整四季均值。',
                               'actor': '虚构演示经办人'}, 'record_confirmation': 'record_accurate',
                'verification': {'state': 'checked_consistent', 'reviewer': '虚构演示复核人',
                                 'basis': '纯虚构演示，已对照合成预期；存款利息覆盖同会计年度完整四个季度。'}}
    for spec in model['input_schema']:
        field = spec['id']
        if field.startswith('crop.secondary.') and field != 'crop.secondary.present' and not secondary:
            continue
        kind = spec['subject_kind']
        subject = applicant if kind == 'applicant' else household if kind in {'household', 'scope'} else secondary if field.startswith('crop.secondary.') else main
        inputs['fields'][field] = record(preset[field], spec['unit'], subject or main, spec['period_kind'])
    inputs['assets'] = [
        {'record_id': 'SYN-ASSET-RESIDENCE', 'type': 'residence', 'share': '1', 'amount': record('500000', '元', applicant, 'as_of')},
        {'record_id': 'SYN-ASSET-MACHINERY', 'type': 'machinery', 'share': '1', 'amount': record('100000', '元', household, 'as_of')}]
    inputs['debts'] = [{'record_id': 'SYN-DEBT-01', 'type': 'bank', 'share': '1', 'amount': record('120000', '元', applicant, 'as_of')}]
    if independent.get('normal_assets'):
        inputs['assets'] = [{'record_id': row['id'], 'type': row['type'], 'share': row['share'],
                             'amount': record(row['value'], '元', household, 'as_of')}
                            for row in independent['normal_assets']]
        inputs['debts'] = [{'record_id': row['id'], 'type': row['type'], 'share': row['share'],
                            'amount': record(row['value'], '元', household, 'as_of')}
                           for row in independent['normal_debts']]
    examples = [{'id': 'V2-COMPLETE', 'label': '完整虚构农户', 'inputs': inputs, 'expected_state': 'valid',
                 'description': '53项已准备的虚构输入和显式模拟核验；用于流程演示，不是业务复核结论。'}]
    for identifier, label, field, value, description in [
        ('V2-MISSING', '缺关键来源', 'cashflow.annual_inflow', None, '年度有效流入缺失，应阻断三路综合额度。'),
        ('V2-LOSS', '经营亏损', 'cost.total', '500000', '经营利润为负，应阻断有效额度。'),
        ('V2-BELOW-MIN', '申请额低于产品下限', 'loan.requested_amount', '9999', '申请额低于10000元，不抬高客户申请额。')]:
        variant = copy.deepcopy(inputs)
        if value is None:
            variant['fields'].pop(field)
        else:
            variant['fields'][field]['value'] = value
            variant['fields'][field]['source_ref']['text'] = '虚构合成资料：'+value
        examples.append({'id': identifier, 'label': label, 'inputs': variant, 'expected_state': 'blocked', 'description': description})
    duplicate = copy.deepcopy(inputs)
    duplicate['debts'].append(copy.deepcopy(duplicate['debts'][0]))
    examples.append({'id': 'V2-DUPLICATE', 'label': '同笔债务重复', 'inputs': duplicate, 'expected_state': 'blocked',
                     'description': '同一record_id重复出现，不能静默汇总或去重。'})
    return examples


class FarmerModelV2Service:
    def __init__(self, store, diligence):
        self.store, self.diligence = store, diligence
        with store.lock, store.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS s5v2_inputs (
                case_id TEXT NOT NULL REFERENCES cases(id), revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, source_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(case_id,revision));
              CREATE TABLE IF NOT EXISTS s5v2_runs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                created_at TEXT NOT NULL,input_revision INTEGER NOT NULL,
                business_hash TEXT NOT NULL,payload_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS s5v2_run_case ON s5v2_runs(case_id,created_at,id);
            ''')

    def _sql_token(self, connection, case_id):
        state = {}
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            table = row['name']
            if not (table.startswith('s3_') or table in {'cases', 'recordings', 'recording_segments'}):
                continue
            columns = {c['name'] for c in connection.execute('PRAGMA table_info("'+table+'")')}
            if 'case_id' in columns:
                values = connection.execute('SELECT * FROM "'+table+'" WHERE case_id=? ORDER BY rowid', (case_id,))
            elif table == 'cases':
                values = connection.execute('SELECT * FROM cases WHERE id=?', (case_id,))
            else:
                continue
            state[table] = [dict(value) for value in values]
        return digest(state)

    def _state(self, case_id):
        case = self.store.case(case_id)
        if case is None:
            fail(404, 'entity_not_found', '本案不存在。')
        if case['subject_type'] != 'farmer' or case['product_id'] != 'farmer_demo':
            fail(409, 'model_not_applicable', '本模型仅适用于本版农户演示产品。')
        for _ in range(3):
            with self.store.lock, self.store.connect() as connection:
                before = self._sql_token(connection, case_id)
            workspace = self.diligence.workspace(case_id)
            if workspace['previsit']['state'] == 'denied':
                fail(403, 'previsit_access_denied', '当前资料权限拒绝，不能旁路读取测算输入或历史。')
            with self.store.lock, self.store.connect() as connection:
                after = self._sql_token(connection, case_id)
            if before == after:
                return workspace, after
        fail(409, 'source_changed', '案件来源仍在变化，请稍后刷新。')

    def _draft(self, connection, case_id):
        row = connection.execute('SELECT * FROM s5v2_inputs WHERE case_id=? ORDER BY revision DESC LIMIT 1', (case_id,)).fetchone()
        if row is None:
            return {'revision': 0, 'inputs': empty_inputs(), 'created_at': None, 'source_fingerprint': None}
        return {'revision': row['revision'], 'inputs': json.loads(row['payload_json']),
                'created_at': row['created_at'], 'source_fingerprint': row['source_fingerprint']}

    def has_inputs(self, case_id):
        with self.store.lock, self.store.connect() as connection:
            return connection.execute('SELECT 1 FROM s5v2_inputs WHERE case_id=? LIMIT 1', (case_id,)).fetchone() is not None

    def fill_missing_from_example(self, case_id, overwrite=False):
        """One-click helper: fill every model input the questionnaire cannot
        answer (subject registry, ledgers, cashflow/credit material side) from
        the canonical synthetic V2-COMPLETE example. Questionnaire-carried
        fields and existing values are never overwritten; filled records are
        marked as synthetic demo material in their source/basis."""
        internal = self._state(case_id)[0]  # carries previsit for example_inputs
        template = next((item['inputs'] for item in example_inputs(internal)
                         if item['id'] == 'V2-COMPLETE'), None)
        if template is None:
            fail(503, 'example_template_missing', '合成演示样例不可用，无法自动补齐。')
        workspace = self.workspace(case_id)  # public view carries draft/input_schema
        draft = workspace['draft']
        inputs = json.loads(json.dumps(draft['inputs']))
        inputs.setdefault('fields', {})
        def _blank(value):
            if value is None or value == '':
                return True
            if isinstance(value, list):
                return not value
            if isinstance(value, dict):  # e.g. accounting_period {'start': '', 'end': ''}
                return not any(str(v or '').strip() for v in value.values())
            return False
        filled_scope = []
        for key in ('evaluation_as_of', 'accounting_period', 'household_id', 'scope_subject_ids', 'scope_basis'):
            if _blank(inputs.get(key)) and not _blank(template.get(key)):
                inputs[key] = json.loads(json.dumps(template[key]))
                filled_scope.append(key)
        for key in ('assets_complete', 'debts_complete'):
            if inputs.get(key) is not True and template.get(key) is True:
                inputs[key] = True
                filled_scope.append(key)
        for key in ('assets', 'debts'):
            if not inputs.get(key) and template.get(key):
                inputs[key] = json.loads(json.dumps(template[key]))
                filled_scope.append(key)
        filled_fields = []
        if overwrite:
            # one-click reset: material side wholesale from the template; the
            # answer bridge overlays real questionnaire answers right after
            inputs['fields'] = json.loads(json.dumps(template.get('fields', {})))
            filled_fields = sorted(inputs['fields'])
        for spec in workspace.get('input_schema', []):
            field_id = spec['id']
            current = inputs['fields'].get(field_id)
            if (current and current.get('presence') in ('known', 'explicit_zero'))                     or not template.get('fields', {}).get(field_id):
                continue
            record = json.loads(json.dumps(template['fields'][field_id]))
            record['source_ref'] = {**(record.get('source_ref') or {}), 'kind': 'manual',
                                    'text': '材料侧合成预填（演示口径）'}
            record['verification'] = {'state': 'checked_consistent', 'reviewer': 'demo_prefill',
                                      'basis': '合成演示材料自动补齐'}
            inputs['fields'][field_id] = record
            filled_fields.append(field_id)
        if not filled_scope and not filled_fields:
            return {**self.workspace(case_id), 'filled_fields': [], 'filled_scope': []}
        saved = self.save_inputs(case_id, inputs, draft['revision'])
        return {**saved, 'filled_fields': filled_fields, 'filled_scope': filled_scope}

    def examples(self, case_id):
        workspace, _ = self._state(case_id)
        return {'case_id': case_id, 'data_class': 'synthetic', 'examples': example_inputs(workspace),
                'note': '只读虚构样例；加载不会保存或改变事实，核验标记均为虚构演示。'}

    def input_history(self, case_id):
        self._state(case_id)
        with self.store.lock, self.store.connect() as connection:
            versions = [{'revision': row['revision'], 'created_at': row['created_at'],
                         'source_fingerprint': row['source_fingerprint'],
                         'inputs': json.loads(row['payload_json'])}
                        for row in connection.execute('SELECT * FROM s5v2_inputs WHERE case_id=? ORDER BY revision DESC', (case_id,))]
        return {'case_id': case_id, 'versions': versions}

    def _fingerprint(self, draft, workspace, model_hash):
        return digest({'inputs': draft['inputs'], 'input_revision': draft['revision'],
                       'source_fingerprint': source_fingerprint(workspace), 'model_hash': model_hash,
                       'engine_version': ENGINE_VERSION})

    def _view(self, payload, draft, workspace, model_hash):
        value = copy.deepcopy(payload)
        reasons = []
        if payload['input_revision'] != draft['revision']:
            reasons.append('inputs_changed')
        if payload['model_hash'] != model_hash:
            reasons.append('model_changed')
        if payload['source_fingerprint'] != source_fingerprint(workspace):
            reasons.append('sources_changed')
        value.update(stale=bool(reasons), stale_reasons=reasons,
                     current_effective_limit=None if reasons else payload.get('effective_limit'))
        return value

    def workspace(self, case_id):
        workspace, _ = self._state(case_id)
        model, model_hash = load_model()
        with self.store.lock, self.store.connect() as connection:
            draft = self._draft(connection, case_id)
            payloads = [json.loads(row['payload_json']) for row in connection.execute(
                'SELECT payload_json FROM s5v2_runs WHERE case_id=? ORDER BY created_at DESC,id DESC', (case_id,))]
        runs = [self._view(p, draft, workspace, model_hash) for p in payloads]
        assessment = assess(draft['inputs'], workspace, model)
        if draft['revision'] and draft['source_fingerprint'] != source_fingerprint(workspace):
            assessment.update(state='blocked', effective_limit=None)
            assessment['blocking_reasons'].append({'field': 'scope', 'reason': 'sources_changed_since_input_save'})
        return {'case_id': case_id, 'model': {**copy.deepcopy(model), 'hash': model_hash},
                'input_schema': copy.deepcopy(model['input_schema']),
                'record_schema': {'source_kinds': ['manual', 'fact', 'statement'],
                    'fields': sorted(RECORD_KEYS), 'presence_options': sorted(PRESENCE),
                    'verification_states': ['not_checked', 'checked_consistent']},
                'ledger_schema': {'fields': ['record_id', 'type', 'share', 'amount'],
                    'asset_types': model['assets_and_debts']['asset_types'],
                    'debt_types': model['assets_and_debts']['debt_types'],
                    'amount': {'unit': '元', 'period_kind': 'as_of', 'subject_kind': 'scope'}},
                'subjects': subjects_for(workspace), 'draft': draft,
                'qualification': assessment, 'latest_run': runs[0] if runs else None,
                'runs': [{k: r.get(k) for k in ['id', 'created_at', 'state', 'input_revision', 'score',
                           'rating', 'effective_limit', 'current_effective_limit', 'stale', 'stale_reasons']} for r in runs],
                'current_input_fingerprint': self._fingerprint(draft, workspace, model_hash)}

    def save_inputs(self, case_id, inputs, expected_revision):
        workspace, token = self._state(case_id)
        model, _ = load_model()
        value = validate_inputs(inputs, model)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('必须提供当前输入修订号。')
        with self.store.lock, self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if self._sql_token(connection, case_id) != token:
                fail(409, 'source_changed', '来源已变化，请刷新后保存。')
            draft = self._draft(connection, case_id)
            if draft['revision'] != expected_revision:
                fail(409, 'revision_conflict', '输入已被其他操作更新，旧草稿不会覆盖新版本。')
            connection.execute('INSERT INTO s5v2_inputs VALUES (?,?,?,?,?)',
                (case_id, expected_revision+1, now(), source_fingerprint(workspace), canonical(value)))
        return self.workspace(case_id)

    def run(self, case_id, expected_revision):
        workspace, token = self._state(case_id)
        model, model_hash = load_model()
        with self.store.lock, self.store.connect() as connection:
            draft = self._draft(connection, case_id)
        if type(expected_revision) is not int or expected_revision < 1 or draft['revision'] != expected_revision:
            fail(409, 'revision_conflict', '请先保存并使用当前输入版本测算。')
        result = assess(draft['inputs'], workspace, model)
        if draft['source_fingerprint'] != source_fingerprint(workspace):
            result.update(state='blocked', effective_limit=None)
            result['blocking_reasons'].append({'field': 'scope', 'reason': 'sources_changed_since_input_save'})
        value = {**result, 'case_id': case_id, 'kind': 'case_snapshot_v2',
                 'model_version': model['version'], 'model_hash': model_hash,
                 'engine_version': ENGINE_VERSION, 'input_revision': draft['revision'],
                 'input_snapshot': copy.deepcopy(draft['inputs']),
                 'source_fingerprint': source_fingerprint(workspace),
                 'input_fingerprint': self._fingerprint(draft, workspace, model_hash),
                 'assessment_only': True, 'business_approved': False, 'data_class': 'synthetic'}
        business_hash = digest(value)
        value.update(id='calculation_v2_'+uuid.uuid4().hex, created_at=now(), business_hash=business_hash)
        with self.store.lock, self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if self._sql_token(connection, case_id) != token:
                fail(409, 'source_changed', '计算期间来源已变化，没有保存过期结果。')
            if self._draft(connection, case_id)['revision'] != expected_revision:
                fail(409, 'revision_conflict', '计算期间输入已变化，没有保存旧版本结果。')
            connection.execute('INSERT INTO s5v2_runs VALUES (?,?,?,?,?,?)',
                (value['id'], case_id, value['created_at'], expected_revision, business_hash, canonical(value)))
        return self.run_detail(case_id, value['id'])

    def run_detail(self, case_id, run_id):
        workspace, _ = self._state(case_id)
        _, model_hash = load_model()
        with self.store.lock, self.store.connect() as connection:
            draft = self._draft(connection, case_id)
            row = connection.execute('SELECT payload_json FROM s5v2_runs WHERE case_id=? AND id=?', (case_id, run_id)).fetchone()
        if row is None:
            fail(404, 'entity_not_found', '本案不存在该模型测算快照。')
        return self._view(json.loads(row[0]), draft, workspace, model_hash)

    def report_context(self, case_id):
        value = self.workspace(case_id)
        latest = value['latest_run']
        current = latest is not None and not latest['stale'] and latest['state'] == 'valid'
        return {'model_version': value['model']['version'], 'model_hash': value['model']['hash'],
                'calculation_id': latest['id'] if latest else None,
                'calculation_hash': latest['business_hash'] if latest else None,
                'input_fingerprint': value['current_input_fingerprint'],
                'state': 'valid' if current else 'blocked', 'stale': latest['stale'] if latest else False,
                'effective_limit': latest['effective_limit'] if current else None,
                'score': latest['score'] if current else None, 'rating': latest['rating'] if current else None,
                'rating_coefficient': latest.get('rating_coefficient') if current else None,
                'trace': latest.get('trace', []) if latest else [],
                'input_snapshot': latest.get('input_snapshot', {}) if latest else value['draft']['inputs'],
                'input_schema': copy.deepcopy(value['input_schema']),
                'score_bindings': copy.deepcopy(value['model']['score_bindings']),
                'ratings': copy.deepcopy(load_reference_model()[0]['ratings']),
                'blocking_reasons': latest.get('blocking_reasons', []) if latest and not latest['stale'] else
                                    value['qualification']['blocking_reasons'],
                'assessment_only': True, 'business_approved': False, 'pricing': {'state': 'not_configured', 'rate': None},
                'subject_id': value['draft']['inputs'].get('household_id'),
                'data_as_of': value['draft']['inputs'].get('evaluation_as_of'),
                'accounting_period': {'kind': 'year', 'label': '本次明确完整会计年度',
                    **value['draft']['inputs'].get('accounting_period', {})},
                'pending_decisions': [], 'confirmed_decisions': value['model'].get('confirmed_decisions', [])}
