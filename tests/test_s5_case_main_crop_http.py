"""Public API contract for the independent B01 category field; no hardware/LLM."""
import copy
import hashlib
import json

import pytest

from test_s3_http_review import env, case, load, scope
from test_s5_case_main_crop import DATA, PERIOD


def post(client,path,body,status=201):
    response=client.post(path,json=body)
    assert response.status_code==status,response.text
    return response.json()


def record(client,cid,field='crop.main.scale_category',value='fruit',**changes):
    prefix=scope(cid)
    source=post(client,prefix+'/statements',{'text':'纯合成2025年主营水果30亩，明确该单元为主营。','speaker_role':'applicant'})
    post(client,prefix+'/statements/'+source['id']+'/confirm',{'expected_revision':1},200)
    return {'field_id':field,'statement_id':source['id'],'statement_revision':1,'value_type':'enum' if field.endswith('scale_category') else 'decimal',
            'value':value,'presence':'known','unit':None if field.endswith('scale_category') else '亩',
            'unit_status':'not_applicable' if field.endswith('scale_category') else 'explicit',
            'period':copy.deepcopy(PERIOD),'period_status':'explicit','subject_id':'C1-MAIN','subject_status':'explicit',
            'subject_basis':'原话明确本案主营，不按列表位置或面积猜选',**changes}


def setup_case(app,client):
    app.state.diligence.fixtures=copy.deepcopy(app.state.diligence.fixtures)
    context=app.state.diligence.fixtures['SYN-S0-001']['previsit_context']
    context['facts']=[f for f in context['facts'] if f['field_id']!='crop.main.area']
    cid=case(client,'新字段API·程序合成')
    load(client,cid)
    return cid


def test_new_api_lists_independent_scale_field_without_replacing_original_object(env):
    app,client=env
    cid=setup_case(app,client)
    state=client.get(scope(cid)+'/diligence').json()
    fields={f['field_id']:f for f in state['questionnaire']['fields']}
    assert len(fields)==57 and state['questionnaire']['progress']['interview_count']==34  # +5 calc questions (2026-11-05)
    assert fields['crop.main.category']['value_type']=='object'
    assert fields['crop.main.category']['handling']=='extension_required'
    assert fields['crop.main.scale_category']['value_type']=='enum'
    assert 'default_value' not in fields['crop.main.scale_category']
    assert not [f for f in state['facts'] if f['field_id']=='crop.main.scale_category']


@pytest.mark.parametrize('changes',[{'value':'banana'}, {'subject_status':'unknown','subject_id':None,'subject_basis':None}])
def test_api_refuses_unmapped_category_or_unknown_subject(env,changes):
    app,client=env;cid=setup_case(app,client)
    response=client.post(scope(cid)+'/facts',json=record(client,cid,**changes))
    assert response.status_code==422
    assert not [f for f in client.get(scope(cid)+'/diligence').json()['facts'] if f['field_id']=='crop.main.scale_category']


def test_confirmed_sources_enter_separate_reference_and_export_has_no_effective_score(env):
    app,client=env;cid=setup_case(app,client);prefix=scope(cid)
    for field,value in [('crop.main.scale_category','fruit'),('crop.main.area','30')]:
        candidate=post(client,prefix+'/facts',record(client,cid,field,value))
        post(client,prefix+'/facts/'+candidate['id']+'/confirm',{'expected_revision':1},200)
    workspace=client.get(prefix+'/calculation').json()
    assert workspace['primary_crop_scale']['reference_score']=='60'
    run=post(client,prefix+'/calculation/run',{})
    assert run['primary_crop_scale']['state']=='reference_only' and run['score'] is None
    report=post(client,prefix+'/reports',{})
    assert all(report['credit'][key] is None for key in ['score','rating','effective_limit'])
    assert client.get(prefix+'/reports/'+report['id']+'/export.docx').status_code==200


def test_api_old_source_version_rejected_and_new_version_does_not_rewrite_history(env):
    app,client=env;cid=setup_case(app,client);prefix=scope(cid)
    data=record(client,cid)
    candidate=post(client,prefix+'/facts',data)
    patch=client.patch(prefix+'/statements/'+data['statement_id'],json={'expected_revision':1,'text':'纯合成修订：原品类待核对。'})
    assert patch.status_code==200
    rejected=client.post(prefix+'/facts',json=data)
    assert rejected.status_code==409
    history=client.get(prefix+'/facts/'+candidate['id']+'/history').json()['versions']
    assert history[0]['value']=='fruit' and history[0]['needs_review'] is True


def test_old_case_remains_original_54_and_rejects_new_independent_field(env):
    app,client=env;service=app.state.diligence
    active,active_hash=service.questionnaire,service.catalog_source_sha256
    service.questionnaire=json.loads((DATA/'questionnaire_v1.json').read_text(encoding='utf-8'))
    service.catalog_source_sha256=hashlib.sha256((DATA/'questionnaire_v1.json').read_bytes()).hexdigest()
    old=case(client,'旧冻结题库·程序合成');load(client,old)
    before=client.get(scope(old)+'/diligence').json()['questionnaire']
    service.questionnaire,service.catalog_source_sha256=active,active_hash
    after=client.get(scope(old)+'/diligence').json()['questionnaire']
    assert before==after and len(after['fields'])==54 and after['progress']['interview_count']==28
    assert client.post(scope(old)+'/facts',json=record(client,old)).status_code==422
    new=case(client,'新题库·程序合成');load(client,new)
    assert len(client.get(scope(new)+'/diligence').json()['questionnaire']['fields'])==57
