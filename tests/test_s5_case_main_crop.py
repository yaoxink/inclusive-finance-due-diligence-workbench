"""Source-bound explicit main-crop selection; synthetic storage, no devices/models."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from due_app.calculations import CalculationService
from due_app.diligence import DiligenceError, DiligenceService, encoded
from due_app.store import DueStore
from test_s3_domain import SyntheticRecordings

DATA = Path(__file__).resolve().parents[1] / 'due_app' / 'data'
PERIOD = {'kind':'year','label':'2025农业生产年度（纯合成）','start':'2025-01-01','end':'2025-12-31'}


@pytest.fixture
def env(tmp_path):
    store=DueStore(tmp_path/'main-crop-synthetic')
    rec=SyntheticRecordings(store)
    service=DiligenceService(store,rec)
    # This independent QA fixture supplies no upstream area. The five original
    # S0 fixture files are not changed, relabelled, or used as credit truth.
    service.fixtures=copy.deepcopy(service.fixtures)
    fixture=service.fixtures['SYN-S0-001']['previsit_context']
    fixture['facts']=[f for f in fixture['facts'] if f['field_id']!='crop.main.area']
    cid=store.create_case('主营品类·仅合成','farmer','farmer_demo')['id']
    service.load_previsit(cid,'SYN-S0-001','base')
    return SimpleNamespace(store=store,recordings=rec,service=service,cid=cid,calculation=CalculationService(store,service))


def payload(env, field='crop.main.scale_category', value='fruit', **changes):
    source=env.service.create_statement(env.cid,{'text':'纯合成：本年度主营水果30亩，次营主粮5亩。','speaker_role':'applicant'})
    env.service.confirm_statement(env.cid,source['id'],1)
    return {'field_id':field,'statement_id':source['id'],'statement_revision':1,
            'value_type':'enum' if field=='crop.main.scale_category' else 'decimal','value':value,'presence':'known',
            'unit':None if field=='crop.main.scale_category' else '亩',
            'unit_status':'not_applicable' if field=='crop.main.scale_category' else 'explicit',
            'period':copy.deepcopy(PERIOD),'period_status':'explicit','subject_id':'C1-MAIN','subject_status':'explicit',
            'subject_basis':'合成原话明确本案主营经营单元及其主营水果身份，不按面积猜选',**changes}


def confirmed(env, **changes):
    data=payload(env,**changes)
    fact=env.service.create_fact(env.cid,data)
    return env.service.confirm_fact(env.cid,fact['id'],1,'仅合成记录准确确认')


def assert_blocked(env, reason):
    result=env.calculation.workspace(env.cid)['primary_crop_scale']
    assert result['state']=='blocked'
    assert reason in result['reasons']
    assert result['effective_score'] is None
    return result


def test_new_catalog_preserves_all_original_source_index_facts():
    old=json.loads((DATA/'questionnaire_v1.json').read_text(encoding='utf-8'))
    new=json.loads((DATA/'questionnaire_v2_main_crop.json').read_text(encoding='utf-8'))
    assert new['version']=='farmer_questionnaire_v2_main_crop_atomic_r2'
    assert new['source_projection']==old['source_projection']
    assert new['source_hashes']==old['source_hashes']
    # atomic-question revision: texts rewritten + hint added; structure (ids/order/handling) unchanged
    old_ids = [f['field_id'] for f in old['fields']]
    new_ids = [f['field_id'] for f in new['fields']
               if f['field_id'] not in {'crop.main.scale_category', 'income.planting.annual_receipts', 'loan.requested_amount'} or f['field_id'] in ('cost.total', 'cashflow.quarter_interest', 'loan.term')]
    assert new_ids == old_ids  # plus the appended answer-driven inputs (in {'crop.main.scale_category', 'income.planting.annual_receipts', 'loan.requested_amount'})
    CALC_FIELDS = {'crop.main.scale_category', 'income.planting.annual_receipts', 'loan.requested_amount',
                   'cost.total', 'cashflow.quarter_interest', 'loan.term'}  # answer-driven calculation inputs (2026-11-05)
    new_rest = [f for f in new['fields'] if f['field_id'] not in CALC_FIELDS]
    old_rest = [f for f in old['fields'] if f['field_id'] not in CALC_FIELDS]
    for f_new, f_old in zip(new_rest, old_rest):
        left = {k: v for k, v in f_new.items() if k not in ('question_text', 'hint')}
        right = {k: v for k, v in f_old.items() if k != 'question_text'}
        assert left == right
    assert len(new['source_projection'])==278
    # 2026-11-05: +5 answer-driven calculation questions became interview (29->34, extension/upstream -1 each... see CALC_FIELDS)
    assert Counter(f['handling'] for f in new['fields'])=={'interview':34,'extension_required':16,'upstream_only':6,'not_supported':1}
    category=next(f for f in new['fields'] if f['field_id']=='crop.main.scale_category')
    assert category['source_question_ids']==[] and category['question_keys']==[]
    assert category['input_binding']['source_taxonomy_mapping']=='not_implemented'
    assert all(c['executable'] is False for f in new['fields'] for c in f.get('conditions',[]))


def test_explicit_confirmed_category_and_same_main_area_enter_reference_only(env):
    category=confirmed(env)
    area=confirmed(env,field='crop.main.area',value='30')
    result=env.calculation.workspace(env.cid)['primary_crop_scale']
    assert result['state']=='reference_only' and result['scale_indicator']=='SCORE-02'
    assert result['selected_area']=='30' and result['reference_score']=='60'
    assert result['effective_score'] is None and result['aggregation_applied'] is False
    assert result['selection_basis'] and result['main_crop_id']=='C1-MAIN'
    assert {s['id'] for s in result['selected_sources']}=={category['id'],area['id']}
    assert all(s['verification']=='not_checked' for s in result['selected_sources'])
    assert result['reference_basis']=='record_accuracy_only_not_verified_fact'
    run=env.calculation.run(env.cid)
    assert run['primary_crop_scale']['selected_area']=='30'
    assert run['score'] is None and run['rating'] is None and run['effective_limit'] is None
    assert 'F10' not in run['inputs']  # Record accuracy did not become verification.


@pytest.mark.parametrize('value',['Fruit','水果','unknown','', 'banana','0'])
def test_invalid_or_implicitly_mapped_categories_are_rejected(env,value):
    with pytest.raises(DiligenceError) as exc:
        env.service.create_fact(env.cid,payload(env,value=value))
    assert exc.value.status==422


@pytest.mark.parametrize('subject,status,basis',[(None,'unknown',None),('C1-SECONDARY','explicit','这是次营'),('C1-HOUSEHOLD','explicit','这是家庭')])
def test_known_category_requires_explicit_main_operating_unit(env,subject,status,basis):
    with pytest.raises(DiligenceError) as exc:
        env.service.create_fact(env.cid,payload(env,subject_id=subject,subject_status=status,subject_basis=basis))
    assert exc.value.code=='main_crop_subject_required'


def test_unknown_category_is_recorded_as_unknown_and_never_becomes_other(env):
    data=payload(env,value=None,presence='unknown',subject_id=None,subject_status='unknown',subject_basis=None)
    env.service.create_fact(env.cid,data)
    confirmed(env,field='crop.main.area',value='30')
    assert_blocked(env,'missing_or_unknown')


def test_category_candidate_or_unconfirmed_source_cannot_enter_reference(env):
    env.service.create_fact(env.cid,payload(env))
    confirmed(env,field='crop.main.area',value='30')
    assert_blocked(env,'record_not_confirmed')


def test_replaying_identical_category_creation_does_not_duplicate_or_confirm(env):
    data=payload(env)
    first=env.service.create_fact(env.cid,data)
    again=env.service.create_fact(env.cid,data)
    assert first['id']==again['id'] and first['revision']==again['revision']==1
    assert again['record_confirmation']=='pending'
    facts=[f for f in env.service.workspace(env.cid)['facts'] if f['field_id']=='crop.main.scale_category']
    assert len(facts)==1


def test_source_revision_change_blocks_old_category_and_preserves_reference_snapshot(env):
    cat=confirmed(env)
    confirmed(env,field='crop.main.area',value='30')
    old=env.calculation.run(env.cid)
    env.service.revise_statement(env.cid,cat['statement_id'],{'expected_revision':1,'text':'纯合成修订：主营品类尚未确认。'})
    assert_blocked(env,'source_changed')
    reread=env.calculation.run_detail(env.cid,old['id'])
    assert reread['stale'] is True and reread['primary_crop_scale']==old['primary_crop_scale']
    with pytest.raises(DiligenceError) as exc:
        env.service.confirm_fact(env.cid,cat['id'],cat['revision'])
    assert exc.value.status==409


def test_secondary_area_is_never_added_to_main_scale(env):
    confirmed(env)
    confirmed(env,field='crop.main.area',value='30')
    confirmed(env,field='crop.secondary.area',value='999',subject_id='C1-SECONDARY',subject_basis='明确次营，不能计入主营')
    result=env.calculation.workspace(env.cid)['primary_crop_scale']
    assert result['selected_area']=='30' and result['reference_score']=='60' and result['aggregation_applied'] is False


def test_mismatched_period_and_future_period_are_blocked(env):
    confirmed(env)
    confirmed(env,field='crop.main.area',value='30',period={**PERIOD,'label':'2024年','start':'2024-01-01','end':'2024-12-31'})
    assert_blocked(env,'main_crop_period_mismatch')


def test_original_future_dated_fixture_is_not_relaxed_for_reference(env):
    service=env.service
    cid=env.store.create_case('原S0时期口径保留','farmer','farmer_demo')['id']
    # Use the unchanged original fixture in a second fresh service.
    original=DiligenceService(env.store,env.recordings)
    original.load_previsit(cid,'SYN-S0-001','base')
    other=SimpleNamespace(service=original,cid=cid)
    data=payload(other,period={'kind':'year','label':'2026农业生产年度（合成口径）','start':'2026-01-01','end':'2026-12-31'})
    fact=original.create_fact(cid,data);original.confirm_fact(cid,fact['id'],1)
    review=CalculationService(env.store,original).workspace(cid)['primary_crop_scale']
    assert review['state']=='blocked' and 'future_period_unapproved' in review['reasons']


def test_old_bound_catalog_is_immutable_and_new_cases_get_v2(env):
    old=json.loads((DATA/'questionnaire_v1.json').read_text(encoding='utf-8'))
    env.service.questionnaire=old
    env.service.catalog_source_sha256=hashlib.sha256((DATA/'questionnaire_v1.json').read_bytes()).hexdigest()
    old_cid=env.store.create_case('旧题库不升级','farmer','farmer_demo')['id']
    env.service.load_previsit(old_cid,'SYN-S0-001','base')
    before=env.service.workspace(old_cid)['questionnaire']
    reloaded=DiligenceService(env.store,env.recordings)
    after=reloaded.workspace(old_cid)['questionnaire']
    assert before==after and after['version']=='farmer_questionnaire_v1'
    new_cid=env.store.create_case('新案新题库','farmer','farmer_demo')['id']
    reloaded.load_previsit(new_cid,'SYN-S0-001','base')
    assert reloaded.workspace(new_cid)['questionnaire']['version']=='farmer_questionnaire_v2_main_crop_atomic_r2'
    other=SimpleNamespace(service=reloaded,cid=old_cid)
    with pytest.raises(DiligenceError) as exc: reloaded.create_fact(old_cid,payload(other))
    assert exc.value.code=='unknown_field'
    assert CalculationService(env.store,reloaded).workspace(old_cid)['primary_crop_scale']['state']=='blocked'
    assert 'primary_crop_scale' not in CalculationService(env.store,reloaded).run(old_cid)


def test_repeated_reference_runs_have_identical_business_hash_and_reopen(tmp_path,env):
    confirmed(env);confirmed(env,field='crop.main.area',value='30')
    first=env.calculation.run(env.cid);second=env.calculation.run(env.cid)
    assert first['business_hash']==second['business_hash']
    reopened=CalculationService(env.store,DiligenceService(env.store,env.recordings)).run_detail(env.cid,first['id'])
    assert reopened['primary_crop_scale']==first['primary_crop_scale'] and not reopened['stale']
