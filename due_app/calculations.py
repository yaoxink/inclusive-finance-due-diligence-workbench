"""Versioned, deterministic reference calculation. No effective credit decisions."""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext, ROUND_DOWN, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import re
import uuid

from .diligence import DiligenceError
from .store import now

MODEL_FILE = Path(__file__).parent / 'data' / 'model_farmer_reference_v1.json'
NUMERIC = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
CELL_KEYS = {'F10','F11','F12','F13','F14','F15','F16','F17','F18','E20','D28','D29','D30','D31','D32','D44','D46','G10','G13','G16','E23','E25','E28'}
ORIGINAL_FORMULAE = {
    'G10':'=F10*F11*F12', 'G13':'=F13*F14*F15', 'G16':'=F16*F17*F18',
    'E23':'=(SUM(G10:G18)-E20)*D44*D46*2',
    'E25':'=(G10*20%+G13*20%+G16*40%)*D44*D46',
    'E28':'=(MAX((D28/3*D30)/(3*D32),(D29*D31/12)*D30))*D44*D46',
    'D48':'=ROUNDDOWN(MIN(MAX(MEDIAN(E23,E25,E28),10000),1000000),-2)',
}
CONFIRMED_GROUP_WEIGHTS = {'household': '0.25', 'operating': '0.35', 'credit': '0.15', 'financial': '0.25'}
PRIMARY_SCALE_INDICATORS = {'fruit': 'SCORE-02', 'grain': 'SCORE-03', 'herb': 'SCORE-04', 'other': 'SCORE-05'}
CONFIRMED_RATING_COEFFICIENTS = {'AAA': '1', 'AA': '0.9125', 'A': '0.825', 'BBB': '0.7375',
                                 'BB': '0.65', 'B': '0.5625', 'CCC': '0.475', 'CC': '0.3875', 'C': '0.3'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def decimal(value):
    if not isinstance(value, str) or len(value) > 80 or not NUMERIC.fullmatch(value):
        raise ValueError('数值必须是有限十进制字符串，不能使用JSON数字、指数或缺省值。')
    result = Decimal(value)
    if not result.is_finite() or abs(result) > Decimal('1e40'):
        raise ValueError('数值超出本参考引擎支持范围。')
    return result


def number(value):
    return format(value, 'f') if value else '0'


def load_model():
    raw = MODEL_FILE.read_bytes()
    model = json.loads(raw)
    if (model.get('schema') != 'farmer-reference-model/1' or model.get('effective_enabled') is not False
            or len(model.get('formulae', [])) != 7 or len(model.get('score_indicators', [])) != 39):
        raise ValueError('模型参考包损坏或未支持的生效配置；停止计算。')
    if {r.get('cell'):r.get('formula') for r in model['formulae']} != ORIGINAL_FORMULAE:
        raise ValueError('模型公式与本实现支持的原7式不一致，不能用旧计算器冒充新公式。')
    if any(model.get('industry_profit_rates',{}).get(k)!=v for k,v in [('main','0.20'),('secondary','0.20'),('other','0.40')]):
        raise ValueError('E25参数与用户已定20%/20%/40%不一致。')
    structure = model.get('confirmed_score_structure', {})
    if ({g['id']: g['weight'] for g in model['groups']} != CONFIRMED_GROUP_WEIGHTS
            or structure.get('group_weights') != CONFIRMED_GROUP_WEIGHTS
            or structure.get('scale_indicator_by_category') != PRIMARY_SCALE_INDICATORS
            or structure.get('scale_weight') != '0.20'
            or structure.get('scale_selection') != 'explicit_main_crop_only'
            or structure.get('aggregate_crop_areas') is not False):
        raise ValueError('评分结构与用户已定四类权重及主营单选规则不一致。')
    group_weights = {g['label']: g['weight'] for g in model['groups']}
    if any(row['group'] in group_weights and row['group_weight'] != group_weights[row['group']] for row in model['score_indicators']):
        raise ValueError('评分指标的分类权重与已确认结构不一致。')
    if any(row['id'] in PRIMARY_SCALE_INDICATORS.values() and row['weight'] != '0.2' for row in model['score_indicators']):
        raise ValueError('种植规模必须只形成一个20%子项。')
    balance_basis = model.get('confirmed_balance_basis', {})
    if (balance_basis.get('asset_amount') != 'current_value'
            or balance_basis.get('debt_amount') != 'current_outstanding_balance'
            or balance_basis.get('same_data_as_of_required') is not True
            or balance_basis.get('purchase_price_and_contract_principal_are_separate') is not True
            or balance_basis.get('declared_asset_value_is_not_current_value') is not True
            or balance_basis.get('contract_principal_is_not_current_balance') is not True
            or balance_basis.get('rating_coefficients') != CONFIRMED_RATING_COEFFICIENTS
            or len(model.get('ratings', [])) != 9
            or {row['rating']: row['coefficient'] for row in model['ratings']} != CONFIRMED_RATING_COEFFICIENTS):
        raise ValueError('资产负债取值或评级系数与用户已确认的B03部分口径不一致。')
    return model, hashlib.sha256(raw).hexdigest()


def normalize_condition(condition):
    """Normalize explicit notation only; never fills gaps in a source band."""
    if isinstance(condition, (int, float)) and not isinstance(condition, bool):
        return str(condition)
    if not isinstance(condition, str):
        return None
    text = condition.strip().replace('（', '(').replace('）', ')').replace('，', ',').replace('∞', 'inf').replace('Inf', 'inf')
    if text.endswith('月'):
        text = text[:-1].strip()
        text = re.sub(r'^(\([^,]+)-(\d+\])$', r'\1,\2', text)
        text = re.sub(r'^\((\d+)\+\]$', r'(\1,+inf)', text)
    return text


def endpoint(value):
    value = value.strip()
    if value in {'+inf','inf'}:
        return Decimal('Infinity')
    if value == '-inf':
        return Decimal('-Infinity')
    if value.endswith('万'):
        return decimal(value[:-1]) * Decimal('10000')
    return decimal(value)


def band_matches(condition, value, allow_missing=False):
    text = normalize_condition(condition)
    if text is None:
        return None
    if value is None:
        return text == '空值' if allow_missing else None
    if text == '空值':
        return False
    match = re.fullmatch(r'([\[(])([^,]+),([^,]+)([\])])', text)
    if match:
        try:
            val = decimal(value)
            lo, hi = endpoint(match[2]), endpoint(match[3])
        except ValueError:
            return None
        return (val > lo or (match[1] == '[' and val == lo)) and (val < hi or (match[4] == ']' and val == hi))
    if isinstance(value, str):
        if NUMERIC.fullmatch(text):
            try:
                return decimal(value) == decimal(text)
            except ValueError:
                return None
        return value in text.split('||')
    return None


def evaluate_indicator(indicator, value, allow_missing=False):
    matches = [b for b in indicator['bins'] if band_matches(b['condition'], value, allow_missing) is True]
    base = {'indicator_id': indicator['id'], 'label': indicator['label'], 'value': value,
            'effective_score': None, 'status': 'blocked_missing' if value is None else 'blocked_bin_gap'}
    if len(matches) > 1:
        return {**base, 'status': 'blocked_ambiguous_bin', 'matching_sources': [b['source'] for b in matches]}
    if not matches:
        return base
    band = matches[0]
    return {**base, 'status': 'reference_only', 'reference_score': band['score'], 'reference_adjustment': band['adjustment'],
            'condition': band['condition'], 'source': band['source']}


def reference_rating(score, model=None):
    model = model or load_model()[0]
    values = [r for r in model['ratings'] if band_matches(r['condition'], score) is True]
    if len(values) != 1:
        return {'state': 'blocked', 'rating': None, 'coefficient': None}
    return {'state': 'reference_only', **copy.deepcopy(values[0])}


def evaluate_primary_crop_scale(crops, main_crop_id=None, selection_basis=None, model=None):
    """Apply confirmed B01 to explicit synthetic crop records, without aggregation.

    This pure reference check does not infer which crop is the main business,
    classify an unknown crop as "other", verify a case fact, or enable credit.
    Case inputs must use the separate explicit B01 scale-category field; the
    original source category tree remains unresolved and cannot be guessed.
    """
    model = model or load_model()[0]
    base = {'state': 'blocked_main_crop_unspecified', 'effective_score': None,
            'decision_id': 'MODEL-B01', 'main_crop_id': main_crop_id,
            'scale_indicator': None, 'selected_area': None, 'reference_score': None,
            'weighted_reference_score': None, 'aggregation_applied': False}
    if not isinstance(crops, list) or any(not isinstance(item, dict) for item in crops):
        raise ValueError('种植条目必须为显式记录列表。')
    if not isinstance(main_crop_id, str) or not main_crop_id.strip() or not isinstance(selection_basis, str) or not selection_basis.strip():
        return base
    selected = [item for item in crops if item.get('id') == main_crop_id]
    if len(selected) != 1:
        return {**base, 'state': 'blocked_main_crop_not_unique'}
    selected = selected[0]
    category = selected.get('category')
    indicator_id = model['confirmed_score_structure']['scale_indicator_by_category'].get(category) if isinstance(category, str) else None
    if indicator_id is None or selected.get('category_status') != 'explicit':
        return {**base, 'state': 'blocked_main_category_unknown'}
    if selected.get('unit') != '亩' or selected.get('area_status') != 'explicit':
        return {**base, 'state': 'blocked_main_area_unknown'}
    try:
        area = decimal(selected.get('area'))
        if area < 0:
            raise ValueError('面积不能为负数。')
    except ValueError:
        return {**base, 'state': 'blocked_main_area_invalid'}
    indicator = next(row for row in model['score_indicators'] if row['id'] == indicator_id)
    trace = evaluate_indicator(indicator, number(area))
    result = {**base, 'state': trace['status'], 'scale_indicator': indicator_id,
              'selected_area': number(area), 'unit': '亩', 'selection_basis': selection_basis.strip(),
              'excluded_crop_ids': [item.get('id') for item in crops if item.get('id') != main_crop_id],
              'scale_weight': model['confirmed_score_structure']['scale_weight'], 'trace': trace}
    if trace['status'] == 'reference_only':
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
            weighted = decimal(trace['reference_score']) * decimal(model['confirmed_score_structure']['scale_weight']) * decimal(CONFIRMED_GROUP_WEIGHTS['operating'])
        result.update(reference_score=trace['reference_score'], weighted_reference_score=number(weighted))
    return result


def evaluate_score(values, scale_indicator=None, model=None, *, crops=None, main_crop_id=None, selection_basis=None):
    """Pure candidate evaluator; explicit crop records use confirmed main-only B01.

    Direct scale_indicator inputs remain isolated source-band reference checks.
    Neither path asserts that real case inputs are qualified or enables credit.
    """
    model = model or load_model()[0]
    values = copy.deepcopy(values)
    unknown = set(values) - {r['id'] for r in model['score_indicators']}
    if unknown:
        raise ValueError('未知评分输入。')
    if scale_indicator is not None and scale_indicator not in {'SCORE-02','SCORE-03','SCORE-04','SCORE-05'}:
        raise ValueError('规模选项不在原四类指标内。')
    primary_scale = None
    if crops is not None or main_crop_id is not None or selection_basis is not None:
        if scale_indicator is not None or set(values) & set(PRIMARY_SCALE_INDICATORS.values()):
            raise ValueError('显式主营记录与手填规模指标不可混用。')
        primary_scale = evaluate_primary_crop_scale(crops, main_crop_id, selection_basis, model)
        if primary_scale['state'] == 'reference_only':
            scale_indicator = primary_scale['scale_indicator']
            values[scale_indicator] = primary_scale['selected_area']
    traces, totals, missing = [], {}, []
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        for row in model['score_indicators']:
            if row['id'] in {'SCORE-02','SCORE-03','SCORE-04','SCORE-05'} and row['id'] != scale_indicator:
                continue
            result = evaluate_indicator(row, values.get(row['id']))
            traces.append(result)
            if result['status'] != 'reference_only':
                missing.append(row['id'])
                continue
            if row['weight'] is not None and row['group_weight'] is not None:
                subtotal = decimal(result['reference_score']) * decimal(row['weight']) * decimal(row['group_weight'])
                totals[row['group']] = totals.get(row['group'], Decimal(0)) + subtotal
                result['weighted_reference_score'] = number(subtotal)
        if scale_indicator is None:
            missing.append('scale_selection_pending')
        if primary_scale and primary_scale['state'] != 'reference_only':
            missing.append(primary_scale['state'])
        reference_base = number(sum(totals.values(), Decimal(0))) if not missing else None
    return {'state': 'candidate_not_approved', 'score': None, 'reference_base_score': reference_base,
            'reference_group_totals': {k:number(v) for k,v in totals.items()}, 'trace': traces,
            'missing_or_undefined': missing, 'scale_selection': scale_indicator,
            'primary_scale': primary_scale,
            'note': '四类权重及只按明确主营品类选一个规模指标已由用户确认；实际主营类别与面积仍须有明确记录。其余加减分及输入口径待定，参考分档不构成有效评分。'}


def calculate_reference(inputs, model=None):
    """Reproduce seven original formulae in fixed order; never execute Excel text."""
    model = model or load_model()[0]
    if not isinstance(inputs, dict) or set(inputs) - CELL_KEYS:
        raise ValueError('存在不支持的单元格输入。')
    values = {k:decimal(v) for k,v in inputs.items()}
    if any(values[k] < 0 for k in values if k.startswith('F') or k in {'E20','D28','D29','D44','D46'}):
        raise ValueError('面积、产量、单价、费用及系数等原始输入不能为负。负收益通过收入减费用体现。')
    if 'D31' in values and not Decimal(0) <= values['D31'] <= Decimal(1):
        raise ValueError('D31参考利润率必须在0到1之间；有效业务口径仍未确认。')
    traces = []
    dependencies = {'G10':['F10','F11','F12'], 'G13':['F13','F14','F15'], 'G16':['F16','F17','F18'],
                    'E23':['G10','G13','G16','E20','D44','D46'], 'E25':['G10','G13','G16','D44','D46'],
                    'E28':['D28','D29','D30','D31','D32','D44','D46'], 'D48':['E23','E25','E28']}
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        for spec in model['formulae']:
            cell = spec['cell']
            needed = dependencies[cell]
            missing = [k for k in needed if k not in values]
            row = {**spec, 'dependencies':needed, 'inputs':{k:number(values[k]) for k in needed if k in values},
                   'value':None, 'state':'blocked_missing' if missing else 'reference_only', 'missing':missing}
            if missing:
                traces.append(row)
                continue
            try:
                if cell in {'G10','G13','G16'}:
                    val = values[needed[0]] * values[needed[1]] * values[needed[2]]
                elif cell == 'E23':
                    val = (values['G10'] + values['G13'] + values['G16'] - values['E20']) * values['D44'] * values['D46'] * 2
                elif cell == 'E25':
                    rates=model['industry_profit_rates']
                    val=(values['G10']*decimal(rates['main'])+values['G13']*decimal(rates['secondary'])+values['G16']*decimal(rates['other']))*values['D44']*values['D46']
                elif cell == 'E28':
                    if values['D30'] <= 0:
                        row.update(state='blocked_invalid_term',reason='D30期限必须大于0，期限量纲仍待确认。')
                        traces.append(row)
                        continue
                    if values['D32'] <= 0:
                        row.update(state='blocked_invalid_rate', reason='D32活期利率必须大于0，且其量纲仍待业务确认。')
                        traces.append(row)
                        continue
                    interest=(values['D28']/3*values['D30'])/(3*values['D32'])
                    flow=(values['D29']*values['D31']/12)*values['D30']
                    val=max(interest,flow)*values['D44']*values['D46']
                    row['branches']={'original_interest_branch':number(interest),'original_inflow_branch':number(flow)}
                else:
                    middle=sorted([values['E23'], values['E25'], values['E28']])[1]
                    clamped=min(max(middle,Decimal('10000')),Decimal('1000000'))
                    val=clamped.quantize(Decimal('1E2'),rounding=ROUND_DOWN)
                    row['median']=number(middle)
                    row['original_clamped']=number(clamped)
                    row['reason']='原D48参考：尚未合并申请额、20亩调整、准入及负收益业务规则。'
                if not val.is_finite():
                    raise InvalidOperation
                if cell in inputs and values[cell] != val:
                    raise ValueError(f'{cell}直接输入与其公式计算值冲突。')
                values[cell]=val
                row['value']=number(val)
            except (InvalidOperation, ZeroDivisionError):
                row.update(state='blocked_numeric_error', value=None)
            traces.append(row)
    return {'state':'reference_only' if all(r['state']=='reference_only' for r in traces) else 'blocked',
            'effective_limit':None, 'reference_limit':number(values['D48']) if 'D48' in values else None,
            'trace':traces, 'values':{k:number(v) for k,v in values.items()},
            'numeric_semantics':copy.deepcopy(model['numeric_semantics'])}


FIXTURES = {
    'E25-MAIN': {'label':'E25主营单路：100 × 20% = 20', 'inputs':{'G10':'100','G13':'0','G16':'0','D44':'1','D46':'1'}},
    'E25-SECONDARY': {'label':'E25次营单路：100 × 20% = 20', 'inputs':{'G10':'0','G13':'100','G16':'0','D44':'1','D46':'1'}},
    'E25-OTHER': {'label':'E25其他单路：100 × 40% = 40', 'inputs':{'G10':'0','G13':'0','G16':'100','D44':'1','D46':'1'}},
    'COMPLETE-REFERENCE': {'label':'七式完整合成夹具（非本案事实）', 'inputs':{'F10':'30','F11':'4500','F12':'2.5','F13':'5','F14':'3000','F15':'3.2','F16':'1','F17':'1000','F18':'1','E20':'100000','D28':'300','D29':'120000','D30':'12','D31':'0.2','D32':'0.003','D44':'1','D46':'1'}},
    'MISSING-ROUTE': {'label':'缺流水路线：不能按两路得综合额', 'inputs':{'G10':'100000','G13':'0','G16':'0','E20':'10000','D44':'1','D46':'1'}},
}


def fingerprint(workspace, model_hash):
    return digest({'case_id':workspace['case_id'], 'model_hash':model_hash,
                   'previsit':{k:workspace['previsit'].get(k) for k in ['state','snapshot_id','context_version','cached']},
                   'questionnaire':{k:workspace['questionnaire'].get(k) for k in ['version','content_sha256']},
                   'facts':workspace['facts'], 'statements':workspace['statements']})


def period_rejection(period, data_as_of=None):
    if not isinstance(period,dict) or not isinstance(period.get('label'),str) or not period['label'].strip():
        return 'period_incomplete'
    kind=period.get('kind')
    start,end=period.get('start'),period.get('end')
    if kind=='duration' and start is None and end is None:
        # A stated duration is not an accounting period; route conversion is
        # separately blocked until its business relationship is defined.
        return None
    try:
        if not isinstance(start,str) or not isinstance(end,str): return 'period_incomplete'
        start_date,end_date=date.fromisoformat(start),date.fromisoformat(end)
        if start_date>end_date: return 'period_invalid'
        if kind=='as_of' and start_date!=end_date: return 'period_invalid'
        years={int(v) for v in re.findall(r'(?<!\d)((?:19|20|21)\d{2})(?!\d)',period['label'])}
        if years and not years.issubset(set(range(start_date.year,end_date.year+1))): return 'period_label_mismatch'
        if data_as_of is not None:
            baseline=date.fromisoformat(data_as_of)
            if end_date>baseline: return 'future_period_unapproved'
            if kind=='as_of' and end_date!=baseline: return 'as_of_not_current'
    except (ValueError,TypeError):
        return 'period_invalid'
    return None


def period_signature(fact):
    period=fact.get('period') or {}
    return tuple(period.get(k) for k in ('kind','label','start','end'))


def fact_rejection(fact, dependency, data_as_of=None):
    if fact.get('needs_review'): return 'source_changed'
    if fact.get('conflict'): return 'unresolved_conflict'
    if fact.get('record_confirmation') != 'record_accurate': return 'record_not_confirmed'
    if fact.get('verification') != 'checked_consistent': return 'fact_not_verified'
    if fact.get('presence') not in {'known','explicit_zero'}: return 'missing_or_unknown'
    if fact.get('subject_scope') != 'context_match': return 'subject_scope_unresolved'
    if fact.get('unit_status') != 'explicit' or fact.get('unit') != dependency['unit']: return 'unit_mismatch'
    kind=(fact.get('period') or {}).get('kind')
    expected=dependency['period_requirement']
    if fact.get('period_status') != 'explicit' or (expected=='year_or_season' and kind not in {'year','season'}) or (expected!='year_or_season' and kind!=expected): return 'period_mismatch'
    problem=period_rejection(fact.get('period'),data_as_of)
    if problem: return problem
    try: decimal(fact.get('value'))
    except ValueError: return 'invalid_number'
    return None


class CalculationService:
    def __init__(self, store, diligence):
        self.store,self.diligence=store,diligence
        with store.lock,store.connect() as connection:
            connection.executescript('''CREATE TABLE IF NOT EXISTS s5_runs (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
              kind TEXT NOT NULL, created_at TEXT NOT NULL, business_hash TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS s5_run_case ON s5_runs(case_id,created_at,id);
              CREATE TABLE IF NOT EXISTS s5_decision_reviews (
              case_id TEXT NOT NULL REFERENCES cases(id), decision_id TEXT NOT NULL, note TEXT NOT NULL,
              reviewed_at TEXT NOT NULL, PRIMARY KEY(case_id,decision_id));''')

    def _workspace(self, case_id):
        case=self.store.case(case_id)
        if case is None:
            raise DiligenceError(404,'entity_not_found','本案不存在。')
        if case['subject_type']!='farmer' or case['product_id']!='farmer_demo':
            raise DiligenceError(409,'model_not_applicable','当前参考模型仅适用于本版农户产品。')
        state=self.diligence.workspace(case_id)
        if state['previsit']['state']=='denied':
            raise DiligenceError(403,'previsit_access_denied','当前资料权限拒绝，不能从计算快照旁路读取。')
        return state

    def model_view(self,case_id):
        self._workspace(case_id)
        model,model_hash=load_model()
        with self.store.lock,self.store.connect() as connection:
            reviews=[dict(r) for r in connection.execute('SELECT decision_id,note,reviewed_at FROM s5_decision_reviews WHERE case_id=?',(case_id,))]
        return {**model,'model_hash':model_hash,'reviews':reviews,
                'fixtures':[{'id':k,'label':v['label']} for k,v in FIXTURES.items()]}

    def review_decision(self,case_id,decision_id,note):
        self._workspace(case_id)
        model,_=load_model()
        if decision_id not in {d['id'] for d in model['pending_decisions']}:
            raise DiligenceError(404,'decision_not_found','不存在该规则待决项。')
        if not isinstance(note,str) or not note.strip() or len(note)>4000:
            raise DiligenceError(422,'invalid_review','请填写不超过4000字的审阅意见。')
        with self.store.lock,self.store.connect() as connection:
            connection.execute('INSERT INTO s5_decision_reviews VALUES (?,?,?,?) ON CONFLICT(case_id,decision_id) DO UPDATE SET note=excluded.note,reviewed_at=excluded.reviewed_at',(case_id,decision_id,note.strip(),now()))
        return {'decision_id':decision_id,'note':note.strip(),'state':'review_note_only','effective_enabled':False}

    def input_review(self,workspace,model):
        reviews,inputs,selected=[],{},{}
        for dep in model['dependencies']:
            candidates=[f for f in workspace['facts'] if dep['field_id'] and f['field_id']==dep['field_id']
                        and f.get('record_confirmation')!='revoked']
            reasons=[]
            if dep['field_id'] is None: reasons=['business_definition_pending']
            elif not candidates: reasons=['missing']
            else:
                reasons=list(dict.fromkeys(fact_rejection(f,dep,workspace['previsit'].get('data_as_of')) for f in candidates))
                if reasons==[None] and len(candidates)==1:
                    inputs[dep['cell']]=candidates[0]['value']; selected[dep['cell']]=candidates[0]; reasons=[]
                elif None in reasons: reasons=[r for r in reasons if r]+['multiple_sources_require_resolution']
            reviews.append({**dep,'state':'qualified_for_reference' if not reasons else 'blocked','reasons':reasons,
                            'facts':[{'id':f['id'],'revision':f['revision'],'value':f['value'],'unit':f.get('unit'),
                                      'period':f.get('period'),'subject_id':f.get('subject_id'),
                                      'record_confirmation':f['record_confirmation'],'verification':f['verification'],
                                      'source_ref':f.get('source_ref'),'statement_id':f.get('statement_id'),
                                      'statement_revision':f.get('statement_revision')} for f in candidates]})
        by_cell={row['cell']:row for row in reviews}
        def block(cells,reason):
            for cell in cells:
                inputs.pop(cell,None)
                row=by_cell[cell]
                row['state']='blocked'
                if reason not in row['reasons']: row['reasons'].append(reason)
        for area,yield_cell,price in [('F10','F11','F12'),('F13','F14','F15'),('F16','F17','F18')]:
            if area in selected and yield_cell in selected:
                if selected[area].get('subject_id')!=selected[yield_cell].get('subject_id'):
                    block([area,yield_cell],'route_subject_mismatch')
                if period_signature(selected[area])!=period_signature(selected[yield_cell]):
                    block([area,yield_cell],'route_period_mismatch')
            if price in selected:
                # An as-of price cannot silently become an average or applicable
                # price for a production year/season, even when years match.
                block([price],'price_period_relation_pending')
        return reviews,inputs

    @staticmethod
    def primary_crop_scale(workspace, model):
        """B01 bucket reference from frozen case sources, never a credit input.

        Confirming that a statement was recorded accurately does not verify its
        truth. This separately labelled reference accepts that record status;
        the seven financial formulae retain their stricter fact qualification.
        """
        category_field = 'crop.main.scale_category'
        result = {'state':'blocked','reasons':[],'effective_score':None,'reference_score':None,
                  'selected_area':None,'main_crop_id':None,'aggregation_applied':False,
                  'reference_basis':'record_accuracy_only_not_verified_fact','selected_sources':[]}
        metadata = next((f for f in workspace['questionnaire']['fields'] if f['field_id']==category_field), None)
        if (metadata is None or metadata.get('handling')!='interview'
                or metadata.get('input_binding',{}).get('contract')!='explicit-main-crop-scale-category/1'):
            return {**result,'reasons':['questionnaire_extension_required']}
        selected = {}
        for key, field_id in [('category',category_field),('area','crop.main.area')]:
            facts = [f for f in workspace['facts'] if f['field_id']==field_id and f.get('record_confirmation')!='revoked']
            if not facts:
                result['reasons'].append('missing_main_'+key)
            elif len(facts)!=1:
                result['reasons'].append('multiple_main_'+key+'_sources')
            else:
                selected[key] = facts[0]
        for key, fact in selected.items():
            result['selected_sources'].append({name:copy.deepcopy(fact.get(name)) for name in
                ['id','revision','field_id','origin','value','unit','unit_status','period','subject_id','subject_basis',
                 'record_confirmation','verification','statement_id','statement_revision','source_ref']})
            if fact.get('needs_review'): result['reasons'].append('source_changed')
            if fact.get('conflict'): result['reasons'].append('unresolved_conflict')
            if fact.get('record_confirmation')!='record_accurate': result['reasons'].append('record_not_confirmed')
            if fact.get('presence') not in {'known','explicit_zero'}: result['reasons'].append('missing_or_unknown')
            if (fact.get('subject_status')!='explicit' or fact.get('subject_scope')!='context_match'
                    or not isinstance(fact.get('subject_basis'),str) or not fact['subject_basis'].strip()):
                result['reasons'].append('main_crop_subject_required')
            if fact.get('origin')=='onsite':
                if not fact.get('source_record_accurate'): result['reasons'].append('source_record_not_confirmed')
                if fact.get('speaker_role')!='applicant': result['reasons'].append('applicant_source_required')
            if (fact.get('period_status')!='explicit'
                    or (fact.get('period') or {}).get('kind') not in {'year','season','as_of'}):
                result['reasons'].append('period_mismatch')
            else:
                problem = period_rejection(fact.get('period'),workspace['previsit'].get('data_as_of'))
                if problem: result['reasons'].append(problem)
            if key=='category':
                if fact.get('value_type')!='enum' or fact.get('value') not in PRIMARY_SCALE_INDICATORS:
                    result['reasons'].append('invalid_main_crop_category')
                if fact.get('unit_status')!='not_applicable' or fact.get('unit') is not None:
                    result['reasons'].append('main_crop_category_unit')
            else:
                if fact.get('unit_status')!='explicit' or fact.get('unit')!='亩': result['reasons'].append('unit_mismatch')
                try:
                    if decimal(fact.get('value')) < 0: raise ValueError('negative area')
                except ValueError:
                    result['reasons'].append('invalid_number')
        if len(selected)==2:
            if selected['category'].get('subject_id')!=selected['area'].get('subject_id'):
                result['reasons'].append('main_crop_subject_mismatch')
            if period_signature(selected['category'])!=period_signature(selected['area']):
                result['reasons'].append('main_crop_period_mismatch')
        result['reasons'] = list(dict.fromkeys(result['reasons']))
        if result['reasons']:
            return result
        category,area = selected['category'],selected['area']
        primary = evaluate_primary_crop_scale([{'id':category['subject_id'],'category':category['value'],
            'category_status':'explicit','area':area['value'],'area_status':'explicit','unit':area['unit']}],
            category['subject_id'],category['subject_basis'],model)
        if primary['state']!='reference_only':
            return {**result,'reasons':[primary['state']]}
        return {**result,**primary,'reasons':[],
                'note':'仅按已确认记录的显式主营品类和面积展示分档；尚未核验的事实不被升级，未产生有效评分、评级或额度。'}

    @staticmethod
    def route_checks(reviews):
        rows={r['cell']:r for r in reviews}
        specifications=[
            ('G10',['F10','F11','F12'],['price_period_relation_pending']),
            ('G13',['F13','F14','F15'],['price_period_relation_pending']),
            ('G16',['F16','F17','F18'],['other_business_dimensions_pending']),
            ('E23',['F10','F11','F12','F13','F14','F15','F16','F17','F18','E20','D44','D46'],
             ['aggregate_subject_scope_pending','common_accounting_period_pending','coefficient_qualification_pending']),
            ('E25',['F10','F11','F12','F13','F14','F15','F16','F17','F18','D44','D46'],
             ['aggregate_subject_scope_pending','common_accounting_period_pending','coefficient_qualification_pending']),
            ('E28',['D28','D29','D30','D31','D32','D44','D46'],
             ['cashflow_period_relation_pending','cashflow_subject_scope_pending','coefficient_qualification_pending']),
        ]
        return [{'cell':cell,'state':'blocked_business_definition','input_cells':members,
                 'blocked_inputs':[key for key in members if rows[key]['state']=='blocked'],
                 'reasons':reasons} for cell,members,reasons in specifications]

    def workspace(self,case_id):
        state=self._workspace(case_id)
        model,model_hash=load_model()
        review,_=self.input_review(state,model)
        with self.store.lock,self.store.connect() as connection:
            runs=[{'id':r['id'],'kind':r['kind'],'created_at':r['created_at'],'business_hash':r['business_hash']} for r in connection.execute('SELECT * FROM s5_runs WHERE case_id=? ORDER BY created_at DESC,id DESC',(case_id,))]
        return {'case_id':case_id,'model':self.model_view(case_id),'input_review':review,
                'current_status':'pending_business_calibration','effective_limit':None,
                'primary_crop_scale':self.primary_crop_scale(state,model),
                'route_checks':self.route_checks(review),
                'input_fingerprint':fingerprint(state,model_hash),'runs':runs,'pricing':model['pricing']}

    def run(self,case_id,fixture_id=None):
        state=self._workspace(case_id)
        model,model_hash=load_model()
        review,inputs=self.input_review(state,model)
        kind='case_snapshot'
        if fixture_id is not None:
            if fixture_id not in FIXTURES:
                raise DiligenceError(422,'unknown_fixture','请选择已登记的合成夹具。')
            kind='synthetic_fixture'
            inputs=copy.deepcopy(FIXTURES[fixture_id]['inputs'])
        calculation=calculate_reference(inputs,model)
        value={'case_id':case_id,'kind':kind,'fixture_id':fixture_id,'model_version':model['version'],
               'model_hash':model_hash,'source_hash':model['source_hash'],'input_fingerprint':fingerprint(state,model_hash),
               'input_snapshot':review if kind=='case_snapshot' else [],'inputs':inputs,
               'route_checks':self.route_checks(review) if kind=='case_snapshot' else [],
               'state':'blocked_pending_business_calibration','effective_limit':None,'score':None,'rating':None,
               'reference':calculation,'pending_decisions':copy.deepcopy(model['pending_decisions']),
               'confirmed_decisions':copy.deepcopy(model.get('confirmed_decisions', [])),
               'data_as_of':state['previsit'].get('data_as_of'),'business_approved':False}
        if kind=='case_snapshot' and any(f['field_id']=='crop.main.scale_category' for f in state['questionnaire']['fields']):
            value['primary_crop_scale']=self.primary_crop_scale(state,model)
        business_hash=digest(value)
        result={**value,'id':'calculation_'+uuid.uuid4().hex,'created_at':now(),'business_hash':business_hash}
        with self.store.lock,self.store.connect() as connection:
            connection.execute('INSERT INTO s5_runs VALUES (?,?,?,?,?,?)',(result['id'],case_id,kind,result['created_at'],business_hash,canonical(result)))
        return self.run_detail(case_id,result['id'])

    def run_detail(self,case_id,run_id):
        state=self._workspace(case_id)
        _,model_hash=load_model()
        with self.store.lock,self.store.connect() as connection:
            row=connection.execute('SELECT payload FROM s5_runs WHERE case_id=? AND id=?',(case_id,run_id)).fetchone()
        if row is None: raise DiligenceError(404,'entity_not_found','本案不存在该计算快照。')
        value=json.loads(row['payload'])
        return {**value,'stale':value['input_fingerprint']!=fingerprint(state,model_hash),
                'current_model_hash':model_hash,'effective_limit':None}

    def report_context(self,case_id):
        if hasattr(self, 'v2') and self.v2.has_inputs(case_id):
            return self.v2.report_context(case_id)
        state=self._workspace(case_id)
        model,model_hash=load_model()
        with self.store.lock,self.store.connect() as connection:
            row=connection.execute("SELECT id FROM s5_runs WHERE case_id=? AND kind='case_snapshot' ORDER BY created_at DESC,id DESC LIMIT 1",(case_id,)).fetchone()
        value=self.run_detail(case_id,row['id']) if row else None
        return {'model_version':model['version'],'model_hash':model_hash,
                'calculation_id':value['id'] if value else None,'calculation_hash':value['business_hash'] if value else None,
                'input_fingerprint':fingerprint(state,model_hash),'state':value['state'] if value else 'not_calculated',
                'stale':value['stale'] if value else False,'effective_limit':None,'score':None,'rating':None,
                'trace':value['reference']['trace'] if value else [],'input_snapshot':value['input_snapshot'] if value else [],
                'confirmed_decisions':copy.deepcopy(model.get('confirmed_decisions', [])),
                'pending_decisions':copy.deepcopy(model['pending_decisions'])}
