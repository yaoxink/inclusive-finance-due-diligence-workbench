"""Readable report projections of an already-qualified, frozen calculation.

This module never recalculates scores or promotes an upstream fact. The original
report bindings and the separate assessment inputs retain their own provenance.
"""
import copy


SCORE_BINDINGS = {
    'F009': 'SCORE-08', 'F011': 'SCORE-10', 'F012': 'SCORE-11',
    'F013': 'SCORE-12', 'F014': 'SCORE-15', 'F015': 'SCORE-14',
    'F016': 'SCORE-16', 'F017': 'SCORE-17', 'F018': 'SCORE-01',
    'F019': 'SCORE-02', 'F020': 'SCORE-03', 'F021': 'SCORE-04',
    'F022': 'SCORE-05', 'F023': 'SCORE-06', 'F024': 'SCORE-07',
    'F025': 'SCORE-18', 'F026': 'SCORE-19', 'F027': 'SCORE-20',
    'F028': 'SCORE-21', 'F029': 'SCORE-24', 'F030': 'SCORE-25',
    'F031': 'SCORE-26', 'F032': 'SCORE-22', 'F033': 'SCORE-23',
    'F034': 'SCORE-27', 'F035': 'SCORE-28', 'F036': 'SCORE-29',
    'F037': 'SCORE-30', 'F039': 'SCORE-32', 'F040': 'SCORE-33',
    'F041': 'SCORE-34', 'F042': 'SCORE-35', 'F043': 'SCORE-36',
    'F044': 'SCORE-37', 'F045': 'SCORE-38', 'F046': 'SCORE-39',
}
DERIVED_UNITS = {
    'derived.main_area': '亩',
    'derived.working_age_ratio': '比例', 'derived.pension_rate': '比例',
    'derived.medical_rate': '比例', 'derived.policy_coverage': '比例',
    'derived.policy_years': '年', 'derived.debt_asset_ratio': '比例',
    'derived.fixed_assets_excluding_homestead': '元',
    'derived.machinery_value': '元', 'derived.planting_revenue': '元',
}
DERIVED_METADATA_SOURCE = {
    'derived.main_area': 'crop.main.area',
    'derived.working_age_ratio': 'household.working_age_count',
    'derived.pension_rate': 'household.pension_count',
    'derived.medical_rate': 'household.medical_count',
    'derived.policy_coverage': 'insurance.policy_covered_area',
    'derived.policy_years': 'insurance.policy_continuous_years',
}
LABELS = {
    'F018': '本次测算会计年度种植收入流水',
    'F023': '本次测算会计年度种植推算收入',
    'F034': '申请人个人收入等级（模型输入）',
    'F040': '申请人个人近12个月开庭公告数量（模型输入）',
    'F045': '近24个月补贴项目条数（不含贴息）',
    'F117': '本次测算经营总成本',
}


def qualified_v2(calculation):
    return (calculation.get('state') == 'valid' and not calculation.get('stale')
            and calculation.get('effective_limit') is not None
            and bool(calculation.get('calculation_id')) and bool(calculation.get('calculation_hash'))
            and isinstance(calculation.get('input_snapshot'), dict)
            and isinstance(calculation.get('input_schema'), list))


def build_appendix(calculation):
    if not qualified_v2(calculation):
        return None
    inputs = calculation['input_snapshot']
    descriptors = {row['id']: row for row in calculation['input_schema']}
    fields = []
    for field_id, record in inputs.get('fields', {}).items():
        spec = descriptors.get(field_id, {})
        fields.append({**copy.deepcopy(record), 'field_id': field_id,
                       'label': ('本次会计年度种植收入流水' if field_id == 'income.planting.annual_receipts'
                                 else spec.get('label', field_id)),
                       'original_label': spec.get('label', field_id)})
    indicators = []
    for original in calculation.get('trace', []):
        if not str(original.get('step', '')).startswith('SCORE-'):
            continue
        row = copy.deepcopy(original)
        if row.get('value') is None:
            row['value'] = original.get('input')  # trace carries the used value as 'input'
        if row['step'] in {'SCORE-01', 'SCORE-06'}:
            row['original_label'] = row['label']
            row['label'] = '本次会计年度种植收入流水' if row['step'] == 'SCORE-01' else '本次会计年度种植推算收入'
        indicators.append(row)
    return {
        'title': '本次测算输入快照',
        'notice': '测算输入与事前资料记载分别保留。请按各自期间、主体及核验依据阅读，口径不同不能直接互换；条件未启用的字段不补值。',
        'scope': {key: copy.deepcopy(inputs.get(key)) for key in [
            'evaluation_as_of', 'accounting_period', 'household_id', 'scope_subject_ids', 'scope_basis']},
        'fields': fields, 'assets': copy.deepcopy(inputs.get('assets', [])),
        'debts': copy.deepcopy(inputs.get('debts', [])),
        'indicators': indicators,
    }


def binding_value(binding_id, calculation):
    """Return a projection, never reinterpret an unbound source coordinate."""
    if not qualified_v2(calculation):
        return None
    inputs = calculation['input_snapshot']
    records = inputs.get('fields', {})
    indicator = SCORE_BINDINGS.get(binding_id)
    if indicator:
        step = next((row for row in calculation.get('trace', []) if row.get('step') == indicator), None)
        if step is None:
            if indicator in {'SCORE-02', 'SCORE-03', 'SCORE-04', 'SCORE-05'}:
                return {'field_id': indicator, 'value': '本次未选为主营规模评分项', 'unit': None,
                        'period': copy.deepcopy(calculation.get('accounting_period')),
                        'subject_id': calculation.get('subject_id'), 'derived': True,
                        'source_field': 'crop.main.scale_category', 'status': '主营品类单选，本项未参与评分；未记作0分'}
            return None
        field = step['field']
        source = records.get(field)
        metadata = source or records.get(DERIVED_METADATA_SOURCE.get(field))
        if metadata is None and field in {'derived.policy_coverage', 'derived.policy_years'}:
            metadata = records.get('insurance.policy_status')
        direct = source or (metadata if field == 'derived.main_area' else None)
        period = metadata.get('period') if metadata else (
            calculation.get('accounting_period') if field == 'derived.planting_revenue' else
            {'kind': 'as_of', 'label': '本次测算基准日', 'start': inputs.get('evaluation_as_of'),
             'end': inputs.get('evaluation_as_of')})
        return {'field_id': indicator, 'value': step['input'],
                'presence': direct.get('presence', 'known') if direct else 'known',
                'label': LABELS.get(binding_id),
                'unit': source.get('unit') if source else DERIVED_UNITS.get(field),
                'period': copy.deepcopy(period),
                'subject_id': metadata.get('subject_id') if metadata else inputs.get('household_id'),
                'derived': source is None and field != 'derived.main_area',
                'source_field': DERIVED_METADATA_SOURCE[field] if field == 'derived.main_area' else field,
                'source_ref': copy.deepcopy(metadata.get('source_ref')) if metadata else None,
                'record_confirmation': metadata.get('record_confirmation') if metadata else 'not_applicable',
                'verification': copy.deepcopy(metadata.get('verification')) if metadata else None,
                'status': '本次辅助测算指标取值；原始输入、分档和加权结果见测算附录'}
    if binding_id == 'F127' and 'operator.agricultural_manager_grade' in records:
        source = records['operator.agricultural_manager_grade']
        return {'field_id': 'derived.has_manager_grade',
                'label': '本次模型记录：是否有农业经理人等级',
                'value': '否' if source['value'] == '无等级' else '是', 'unit': None,
                'period': copy.deepcopy(source.get('period')), 'subject_id': source.get('subject_id'),
                'derived': True, 'source_field': 'operator.agricultural_manager_grade',
                'source_ref': copy.deepcopy(source.get('source_ref')),
                'status': '由本稿明确等级记录派生；不替代外部资格核查'}
    if binding_id == 'F117' and 'cost.total' in records:
        source = records['cost.total']
        return {**copy.deepcopy(source), 'field_id': 'cost.total', 'source_field': 'cost.total',
                'label': LABELS['F117'],
                'derived': False, 'status': '本次测算的同年度经营总成本；不与其他期间明细混加'}
    return None


def source_text(source, reference_id=''):
    source = source or {}
    kind = source.get('kind')
    if kind == 'calculation_snapshot':
        text = '计算快照 ' + str(source.get('calculation_id') or '待确认')
        if source.get('model_version'): text += '；模型 ' + source['model_version']
        if source.get('source_field'): text += '；输入/派生项 ' + source['source_field']
        if source.get('input_source'): text += '\n原始依据：' + source_text(source['input_source'])
        return text
    if kind == 'manual':
        return '人工记录 ' + str(source.get('actor') or '未明确') + '：' + str(source.get('text') or '未明确')
    if kind in {'fact', 'statement'}:
        label = '事实' if kind == 'fact' else '原话'
        return label + ' ' + str(source.get(kind + '_id') or '未明确') + ' 第' + str(source.get('revision') or '未明确') + '版'
    # Full structured locators remain losslessly available in evidence.json.
    positions = [str(source[key]) for key in ['material_id', 'source_id', 'snapshot_id', 'location', 'path', 'field_path', 'source_path']
                 if source.get(key) is not None and isinstance(source[key], (str, int))]
    return '；'.join(positions) if positions else '完整来源定位见证据索引' + (' ' + reference_id if reference_id else '')
