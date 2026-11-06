"""Cross-field qualification regressions. All facts are synthetic fixtures."""
import copy

import pytest

from test_s5_calculations import env
from due_app.calculations import load_model,period_rejection


BASELINE='2026-10-30'
PRODUCTION={'kind':'season','label':'2026生产季','start':'2026-04-01','end':BASELINE}
AS_OF={'kind':'as_of','label':'截至2026年10月30日','start':BASELINE,'end':BASELINE}


def fact(identifier,field,value,unit,period=None,subject='C1-MAIN',**changes):
    return {'id':identifier,'field_id':field,'revision':1,'value':value,'unit':unit,
            'unit_status':'explicit','period':copy.deepcopy(period or PRODUCTION),'period_status':'explicit',
            'subject_id':subject,'subject_scope':'context_match','subject_status':'explicit',
            'record_confirmation':'record_accurate','verification':'checked_consistent',
            'presence':'known','needs_review':False,'conflict':False,'source_ref':{'synthetic':True},
            **changes}


def workspace(facts):
    return {'facts':facts,'previsit':{'data_as_of':BASELINE}}


def main_facts():
    return [fact('area','crop.main.area','30','亩'),fact('yield','crop.main.yield','4500','斤/亩'),
            fact('price','crop.main.price','2.5','元/斤',AS_OF)]


def review(env,facts):
    rows,inputs=env.service.input_review(workspace(facts),load_model()[0])
    return {r['cell']:r for r in rows},inputs


def test_exact_same_unit_subject_and_period_pair_passes_but_price_relation_does_not(env):
    rows,inputs=review(env,main_facts())
    assert rows['F10']['state']==rows['F11']['state']=='qualified_for_reference'
    assert inputs=={'F10':'30','F11':'4500'}
    assert rows['F12']['state']=='blocked'
    assert rows['F12']['reasons']==['price_period_relation_pending']
    assert rows['F12']['facts'][0]['period']==AS_OF


@pytest.mark.parametrize('period',[
    {'kind':'season','label':'2025生产季','start':'2025-04-01','end':'2025-10-30'},
    {'kind':'season','label':'2026生产季','start':'2026-05-01','end':BASELINE},
    {'kind':'season','label':'2026另一生产季','start':'2026-04-01','end':BASELINE},
])
def test_different_production_year_dates_or_labels_do_not_multiply(env,period):
    facts=main_facts();facts[0]['period']=period
    rows,inputs=review(env,facts)
    for cell in ['F10','F11']:
        assert rows[cell]['state']=='blocked' and 'route_period_mismatch' in rows[cell]['reasons']
        assert cell not in inputs
    assert 'F12' not in inputs


def test_two_distinct_main_crop_units_are_not_silently_merged(env):
    facts=main_facts();facts[1]['subject_id']='C1-SECOND-MAIN'
    rows,inputs=review(env,facts)
    assert all('route_subject_mismatch' in rows[k]['reasons'] for k in ['F10','F11'])
    assert not inputs


@pytest.mark.parametrize('change,expected',[
    ({'start':None},'period_incomplete'),
    ({'end':None},'period_incomplete'),
    ({'label':''},'period_incomplete'),
    ({'start':'2026-02-30'},'period_invalid'),
    ({'start':'2026-11-01'},'period_invalid'),
    ({'label':'2025生产季'},'period_label_mismatch'),
    ({'end':'2026-10-31'},'future_period_unapproved'),
])
def test_period_structure_label_and_baseline_boundaries(change,expected):
    assert period_rejection({**PRODUCTION,**change},BASELINE)==expected


def test_asof_must_be_single_current_date_and_not_annual_price_equivalence(env):
    assert period_rejection(AS_OF,BASELINE) is None
    assert period_rejection({**AS_OF,'start':'2026-10-29'},BASELINE)=='period_invalid'
    old={'kind':'as_of','label':'截至2026年10月29日','start':'2026-10-29','end':'2026-10-29'}
    assert period_rejection(old,BASELINE)=='as_of_not_current'
    facts=main_facts();facts[2]['period']=old
    rows,inputs=review(env,facts)
    assert rows['F12']['reasons']==['as_of_not_current'] and 'F12' not in inputs


def test_retracted_wrong_source_is_excluded_before_multiple_source_check(env):
    facts=main_facts()
    facts.append(fact('revoked-wrong','crop.main.area','999','万元',subject='OUTSIDE',record_confirmation='revoked'))
    before=copy.deepcopy(facts)
    rows,inputs=review(env,facts)
    assert rows['F10']['state']=='qualified_for_reference' and inputs['F10']=='30'
    assert [f['id'] for f in rows['F10']['facts']]==['area']
    assert facts==before  # historical record is not deleted or rewritten


def test_only_retracted_sources_remain_missing(env):
    facts=[fact('revoked','crop.main.area','30','亩',record_confirmation='revoked')]
    rows,inputs=review(env,facts)
    assert rows['F10']['reasons']==['missing'] and rows['F10']['facts']==[] and not inputs


def test_two_non_retracted_sources_still_require_explicit_resolution(env):
    facts=main_facts();facts.append(fact('another-active-area','crop.main.area','30','亩'))
    rows,inputs=review(env,facts)
    assert rows['F10']['state']=='blocked' and 'multiple_sources_require_resolution' in rows['F10']['reasons']
    assert 'F10' not in inputs


def test_route_relationships_for_aggregated_and_cashflow_paths_are_explicitly_blocked(env):
    rows,_=env.service.input_review(workspace(main_facts()),load_model()[0])
    checks={r['cell']:r for r in env.service.route_checks(rows)}
    for key in ['E23','E25']:
        assert checks[key]['state']=='blocked_business_definition'
        assert 'aggregate_subject_scope_pending' in checks[key]['reasons']
        assert 'common_accounting_period_pending' in checks[key]['reasons']
    assert 'cashflow_period_relation_pending' in checks['E28']['reasons']
    assert 'cashflow_subject_scope_pending' in checks['E28']['reasons']


def test_original_and_repaired_source_fingerprints_change_with_qualification_version():
    model,model_hash=load_model()
    assert model['qualification_version']=='s5-input-coherence/2'
    assert model['effective_enabled'] is False
    assert model['input_relationship_policy']['revoked_sources'].startswith('excluded')
    assert len(model_hash)==64
