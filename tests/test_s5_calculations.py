"""Independent expected values and source boundaries, no microphone or model calls."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from due_app.calculations import (CalculationService,FIXTURES,band_matches,calculate_reference,
    decimal,evaluate_indicator,evaluate_score,fact_rejection,fingerprint,load_model,reference_rating)
from due_app.calculation_routes import install_calculation_routes
from due_app.diligence import DiligenceError,DiligenceService
from due_app.store import DueStore


def value(result,cell):
    return next(t for t in result['trace'] if t['cell']==cell)


@pytest.mark.parametrize('kind,expected',[('E25-MAIN','20.00'),('E25-SECONDARY','20.00'),('E25-OTHER','40.00')])
def test_e25_user_confirmed_rates_are_independent_of_d31(kind,expected):
    result=calculate_reference(FIXTURES[kind]['inputs'])
    assert Decimal(value(result,'E25')['value'])==Decimal(expected)
    assert value(result,'E28')['state']=='blocked_missing'
    assert result['effective_limit'] is None


def test_seven_original_formulas_against_hand_derived_outputs():
    result=calculate_reference(FIXTURES['COMPLETE-REFERENCE']['inputs'])
    expected={'G10':'337500.0','G13':'48000.0','G16':'1000','E23':'573000.0','E25':'77500.000','D48':'133300'}
    for cell,answer in expected.items():
        assert Decimal(value(result,cell)['value'])==Decimal(answer)
    assert value(result,'E28')['value']=='133333.33333333333333333333333333333333333333333333'
    assert result['state']=='reference_only' and result['effective_limit'] is None


@pytest.mark.parametrize('bad',[None,1,True,'NaN','Infinity','1e3','01',' 1','1,000','1/2','0x10','1'*81])
def test_strict_decimal_does_not_coerce(bad):
    with pytest.raises(ValueError): decimal(bad)


@pytest.mark.parametrize('cell,bad',[('F10','-1'),('D44','-1'),('D31','1.1'),('D31','-0.1')])
def test_negative_original_inputs_and_invalid_margin_rejected(cell,bad):
    data=copy.deepcopy(FIXTURES['COMPLETE-REFERENCE']['inputs']);data[cell]=bad
    with pytest.raises(ValueError): calculate_reference(data)


@pytest.mark.parametrize('cell,value_,state',[('D32','0','blocked_invalid_rate'),('D32','-0.01','blocked_invalid_rate'),('D30','0','blocked_invalid_term')])
def test_invalid_rate_or_term_does_not_fall_back_to_a_valid_floor(cell,value_,state):
    data=copy.deepcopy(FIXTURES['COMPLETE-REFERENCE']['inputs']);data[cell]=value_
    result=calculate_reference(data)
    assert value(result,'E28')['state']==state
    assert value(result,'D48')['value'] is None and result['reference_limit'] is None


def test_missing_one_route_is_not_two_route_median():
    result=calculate_reference(FIXTURES['MISSING-ROUTE']['inputs'])
    assert value(result,'E23')['value'] is not None and value(result,'E25')['value'] is not None
    assert value(result,'D48')['missing']==['E28']
    assert result['reference_limit'] is None


@pytest.mark.parametrize('routes,expected',[
    (['-3','-2','-1'],'10000'),(['9999','9998','9997'],'10000'),
    (['12345','12346','12344'],'12300'),(['1000050','1000100','1000200'],'1000000'),
    (['10000','10000','10000'],'10000'),(['1000000','1000000','1000000'],'1000000')])
def test_original_d48_clamp_and_truncation_are_reference_only(routes,expected):
    result=calculate_reference(dict(zip(['E23','E25','E28'],routes)))
    assert value(result,'D48')['value']==expected
    assert result['effective_limit'] is None


def test_model_preserves_all_39_rules_and_exact_rating_coefficients():
    model,_=load_model()
    assert len(model['score_indicators'])==39 and len(model['formulae'])==7
    assert sum(Decimal(g['weight']) for g in model['groups'])==1
    assert reference_rating('70')['coefficient']=='0.9125'
    assert reference_rating('69.999')['coefficient']=='0.825'
    assert reference_rating('80')['rating']=='AAA'
    assert reference_rating('-0.1')['rating']=='C'
    assert model['effective_enabled'] is False


@pytest.mark.parametrize('condition,point,answer',[
    ('[0,10]','0',True),('[0,10]','10',True),('[0,10]','10.001',False),
    ('(10,20]','10',False),('(10,20]','20',True),('（0,3）','0',False),('（0,3）','3',False),
    ('(24+]月','24',False),('(24+]月','25',True),(' (0-3]月','3',True),
    ('[0,5万]','50000',True),('[0,5万]','50000.01',False),
    ('(-inf，10)','-100',True),('(-inf，10)','10',False),('I||J||K','J',True),
    ('固定合作','固定合作',True),('空值',None,None),('空值','0',False),
])
def test_source_band_boundaries_and_no_missing_defaults(condition,point,answer):
    assert band_matches(condition,point) is answer


def source_band_boundary_examples():
    model,_=load_model()
    from due_app.calculations import normalize_condition,endpoint
    import re
    rows=[]
    for indicator in model['score_indicators']:
        for band in indicator['bins']:
            text=normalize_condition(band['condition'])
            match=re.fullmatch(r'([\[(])([^,]+),([^,]+)([\])])',text or '')
            if not match: continue
            for raw,closed in [(match[2],match[1]=='['),(match[3],match[4]==']')]:
                edge=endpoint(raw)
                if not edge.is_finite(): continue
                rows.append((band['condition'],format(edge,'f'),closed))
                lo,hi=endpoint(match[2]),endpoint(match[3])
                for neighbor in [edge-Decimal('0.0001'),edge+Decimal('0.0001')]:
                    included=lo<neighbor<hi
                    rows.append((band['condition'],format(neighbor,'f'),included))
    return rows


@pytest.mark.parametrize('condition,point,included',source_band_boundary_examples())
def test_every_normalized_numeric_source_bin_endpoint(condition,point,included):
    assert band_matches(condition,point) is included


def test_gap_is_not_interpolated_or_given_missing_score():
    model,_=load_model()
    row=next(r for r in model['score_indicators'] if r['label']=='最近90天贷款机构数')
    assert evaluate_indicator(row,'3')['status']=='blocked_bin_gap'
    assert evaluate_indicator(row,None)['status']=='blocked_missing'
    score=evaluate_score({})
    assert score['score'] is None and score['reference_base_score'] is None
    assert 'scale_selection_pending' in score['missing_or_undefined']


@pytest.fixture
def env(tmp_path):
    store=DueStore(tmp_path/'synthetic-s5')
    diligence=DiligenceService(store,SimpleNamespace(get=lambda *args:None))
    service=CalculationService(store,diligence)
    cid=store.create_case('S5独立合成','farmer','farmer_demo')['id']
    diligence.load_previsit(cid,'SYN-S0-001','base')
    return SimpleNamespace(store=store,diligence=diligence,service=service,cid=cid)


def test_case_inputs_never_borrow_fixture_or_unverified_values(env):
    first=env.service.run(env.cid)
    assert first['kind']=='case_snapshot' and first['effective_limit'] is None
    assert not first['inputs']
    fixture=env.service.run(env.cid,'COMPLETE-REFERENCE')
    assert fixture['reference']['reference_limit']=='133300'
    context=env.service.report_context(env.cid)
    assert context['calculation_id']==first['id'] and context['effective_limit'] is None
    assert all(t['value'] is None for t in context['trace'])


def test_repeated_case_snapshots_have_same_business_hash_and_restart_replay(env):
    runs=[env.service.run(env.cid) for _ in range(3)]
    assert len({r['business_hash'] for r in runs})==1 and len({r['id'] for r in runs})==3
    restarted=CalculationService(env.store,env.diligence)
    assert restarted.run_detail(env.cid,runs[0]['id'])['business_hash']==runs[0]['business_hash']
    assert restarted.run_detail(env.cid,runs[0]['id'])['stale'] is False


def test_same_fixture_is_repeatable_but_never_current_case_result(env):
    a,b=[env.service.run(env.cid,'COMPLETE-REFERENCE') for _ in range(2)]
    assert a['business_hash']==b['business_hash']
    assert env.service.report_context(env.cid)['calculation_id'] is None


def test_changed_context_and_model_hash_mark_old_runs_stale(env,tmp_path,monkeypatch):
    import due_app.calculations as module
    old=env.service.run(env.cid)
    env.diligence.load_previsit(env.cid,'SYN-S0-001','updated')
    assert env.service.run_detail(env.cid,old['id'])['stale'] is True
    fresh=env.service.run(env.cid)
    model=json.loads(module.MODEL_FILE.read_text(encoding='utf-8'));model['version']='synthetic-v2'
    p=tmp_path/'model-v2.json';p.write_text(json.dumps(model),encoding='utf-8');monkeypatch.setattr(module,'MODEL_FILE',p)
    assert env.service.run_detail(env.cid,fresh['id'])['stale'] is True
    assert env.service.report_context(env.cid)['model_version']=='synthetic-v2'


def test_permission_denied_hides_model_inputs_and_old_runs(env):
    old=env.service.run(env.cid,'COMPLETE-REFERENCE')
    env.diligence.load_previsit(env.cid,'SYN-S0-001','denied')
    for action in [lambda:env.service.workspace(env.cid),lambda:env.service.run_detail(env.cid,old['id']),lambda:env.service.report_context(env.cid)]:
        with pytest.raises(DiligenceError) as exc: action()
        assert exc.value.status==403


def test_cross_case_ids_do_not_read_old_snapshots(env):
    other=env.store.create_case('S5独立合成','farmer','farmer_demo')['id']
    old=env.service.run(env.cid)
    with pytest.raises(DiligenceError) as exc: env.service.run_detail(other,old['id'])
    assert exc.value.status==404


def test_review_note_is_not_business_approval_or_model_mutation(env):
    before=load_model()[1]
    value=env.service.review_decision(env.cid,'MODEL-B04','建议先核对D31，请保留待确认。')
    assert value['state']=='review_note_only' and value['effective_enabled'] is False
    assert load_model()[1]==before
    assert env.service.model_view(env.cid)['reviews'][0]['note'].startswith('建议')
    assert env.service.run(env.cid)['effective_limit'] is None


@pytest.mark.parametrize('change,expected',[({'verification':'not_checked'},'fact_not_verified'),({'needs_review':True},'source_changed'),({'conflict':True},'unresolved_conflict'),({'subject_scope':'out_of_scope'},'subject_scope_unresolved'),({'unit':'万元'},'unit_mismatch'),({'period':{'kind':'duration'}},'period_mismatch'),({'presence':'unknown'},'missing_or_unknown')])
def test_input_qualification_is_independent_of_record_accuracy(change,expected):
    fact={'needs_review':False,'conflict':False,'record_confirmation':'record_accurate','verification':'checked_consistent','presence':'known','subject_scope':'context_match','unit_status':'explicit','unit':'元','period_status':'explicit','period':{'kind':'year'},'value':'12000'}
    dep={'unit':'元','period_requirement':'year'}
    assert fact_rejection({**fact,**change},dep)==expected


def test_http_contract_rejects_client_fact_override_and_extra_fields(env):
    app=FastAPI();install_calculation_routes(app,env.store,env.diligence)
    path='/api/due-diligence/cases/'+env.cid+'/calculation'
    with TestClient(app) as client:
        assert client.get(path).status_code==200
        assert client.post(path+'/run',json={'inputs':{'D31':'0.2'}}).status_code==422
        assert client.post(path+'/fixture',json={'fixture_id':'COMPLETE-REFERENCE','effective_enabled':True}).status_code==422
        result=client.post(path+'/fixture',json={'fixture_id':'COMPLETE-REFERENCE'})
        assert result.status_code==201 and result.json()['effective_limit'] is None
        assert client.get(path+'/runs/'+result.json()['id']).status_code==200
        env.diligence.load_previsit(env.cid,'SYN-S0-001','denied')
        assert client.get(path).status_code==403


def test_changed_subject_product_not_applicable(env):
    with env.store.connect() as connection:
        connection.execute('UPDATE cases SET product_id=? WHERE id=?',('other_product',env.cid))
    with pytest.raises(DiligenceError) as exc: env.service.run(env.cid)
    assert exc.value.code=='model_not_applicable'


@pytest.mark.parametrize('change',['formula','margin','effective'])
def test_model_pack_cannot_silently_relabel_old_executor_or_enable_credit(tmp_path,monkeypatch,change):
    import due_app.calculations as module
    model=json.loads(module.MODEL_FILE.read_text(encoding='utf-8'))
    if change=='formula': model['formulae'][0]['formula']='=F10*999'
    if change=='margin': model['industry_profit_rates']['main']='0.10'
    if change=='effective': model['effective_enabled']=True
    path=tmp_path/'bad-model.json';path.write_text(json.dumps(model),encoding='utf-8')
    monkeypatch.setattr(module,'MODEL_FILE',path)
    with pytest.raises(ValueError): module.load_model()
