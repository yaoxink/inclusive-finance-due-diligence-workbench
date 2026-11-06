"""Whole-workbench synthetic HTTP paths, deliberately without microphone capture."""
from io import BytesIO
import json
import zipfile

import pytest

from test_s3_http_review import env, case, load, scope, statement, fact_body


@pytest.mark.parametrize('fixture',[f'SYN-S0-00{i}' for i in range(1,6)])
def test_five_cases_flow_through_questions_calculation_draft_and_export(env,fixture):
    app, client=env
    cid=case(client,'S7全工作台合成场景 '+fixture)
    state=load(client,cid,fixture)
    prefix=scope(cid)
    follow=client.post(prefix+'/followups/analyze',json={})
    assert follow.status_code==200,follow.text
    assert len(follow.json()['active_cards'])<=3
    assert follow.json()['ai']['state']=='disabled'
    calculated=client.post(prefix+'/calculation/run',json={})
    assert calculated.status_code==201,calculated.text
    assert calculated.json()['effective_limit'] is None
    report=client.post(prefix+'/reports',json={})
    assert report.status_code==201,report.text
    report=report.json()
    assert report['credit']['effective_limit'] is None
    assert report['branch']=='no_limit_draft'
    assert report['snapshot']['calculation']['calculation_id']==calculated.json()['id']
    assert len(report['sections'])==9 and sum(len(s['tables']) for s in report['sections'])==40
    index=client.get(prefix+'/reports/'+report['id']+'/evidence.json')
    assert index.status_code==200
    assert cid in json.dumps(index.json(),ensure_ascii=False)
    doc=client.get(prefix+'/reports/'+report['id']+'/export.docx')
    assert doc.status_code==200,doc.text[:200]
    with zipfile.ZipFile(BytesIO(doc.content)) as archive:
        assert archive.testzip() is None
        assert 'word/document.xml' in archive.namelist()
    reopened=client.get(prefix+'/reports/'+report['id'])
    assert reopened.status_code==200 and reopened.json()['content_hash']==report['content_hash']
    assert app.state.recordings.active() is None


def test_correction_invalidates_questions_calculation_and_report_together(env):
    _,client=env
    cid=case(client)
    load(client,cid)
    prefix=scope(cid)
    src=statement(client,cid,'虚构记录：家庭成员4人。')
    assert client.post(prefix+'/statements/'+src['id']+'/confirm',json={'expected_revision':1}).status_code==200
    fact=client.post(prefix+'/facts',json=fact_body(src)).json()
    assert client.post(prefix+'/facts/'+fact['id']+'/confirm',json={'expected_revision':1}).status_code==200
    analyzed=client.post(prefix+'/followups/analyze',json={})
    assert analyzed.status_code==200,analyzed.text
    calc=client.post(prefix+'/calculation/run',json={}).json()
    report=client.post(prefix+'/reports',json={}).json()
    changed=client.patch(prefix+'/statements/'+src['id'],json={'expected_revision':1,'text':'虚构修订：家庭成员5人，原记录遗漏1人。'})
    assert changed.status_code==200
    assert client.get(prefix+'/calculation/runs/'+calc['id']).json()['stale'] is True
    assert client.get(prefix+'/reports/'+report['id']).json()['stale'] is True
    follow=client.get(prefix+'/followups').json()
    assert follow['analysis_current'] is False
    refreshed=client.post(prefix+'/followups/analyze',json={})
    assert refreshed.status_code==200,refreshed.text
    assert any(c['field_id']=='household.member_count' and c['rule']=='source_changed' for c in refreshed.json()['active_cards'])
    assert client.get(prefix+'/diligence').json()['credit']['effective_limit'] is None


def test_ai_failure_keeps_rules_draft_export_and_recording_independent(env):
    app,client=env
    class Offline:
        def generate(self,source): raise TimeoutError('synthetic injected local-model timeout')
    app.state.followups.adapter=Offline()
    cid=case(client)
    load(client,cid)
    prefix=scope(cid)
    # missing-item cards are hidden on HTTP now; seed an unconfirmed-period fact
    # so the degraded run still has a genuine rule card to show
    source=statement(client,cid,'合成原话：承包地10亩。')
    client.post(prefix+'/facts',json=fact_body(source,field_id='land.contracted_area',
        value_type='decimal',value='10',unit='亩',subject_id='C1-HOUSEHOLD',
        period=None,period_status='unknown'))
    assert client.post(prefix+'/followups/ai-settings',json={'enabled':True,'expected_revision':0}).status_code==200
    follow=client.post(prefix+'/followups/analyze',json={})
    assert follow.status_code==200
    assert follow.json()['ai']['state']=='degraded' and follow.json()['active_cards']
    report=client.post(prefix+'/reports',json={}).json()
    assert client.get(prefix+'/reports/'+report['id']+'/export.docx').status_code==200
    assert app.state.recordings.active() is None and app.state.recordings.asr_busy_count()==0


def test_denied_context_cannot_escape_via_new_features_or_full_backup(env):
    _,client=env
    cid=case(client)
    load(client,cid)
    prefix=scope(cid)
    report=client.post(prefix+'/reports',json={}).json()
    calc=client.post(prefix+'/calculation/run',json={}).json()
    backup=client.post('/api/due-diligence/maintenance/backups',json={})
    assert backup.status_code==201,backup.text
    bid=backup.json()['id']
    load(client,cid,variant='denied')
    paths=[prefix+'/followups',prefix+'/calculation',prefix+'/calculation/runs/'+calc['id'],
           prefix+'/reports',prefix+'/reports/'+report['id'],
           prefix+'/reports/'+report['id']+'/evidence.json',prefix+'/reports/'+report['id']+'/export.docx',
           '/api/due-diligence/maintenance/backups/'+bid+'/download']
    assert all(client.get(path).status_code==403 for path in paths)
    assert client.post('/api/due-diligence/maintenance/backups',json={}).status_code==403


def test_backup_restores_all_new_domain_tables_and_frozen_cover(env):
    _,client=env
    cid=case(client)
    load(client,cid)
    prefix=scope(cid)
    client.post(prefix+'/followups/analyze',json={})
    client.post(prefix+'/calculation/run',json={})
    report=client.post(prefix+'/reports',json={})
    assert report.status_code==201
    backup=client.post('/api/due-diligence/maintenance/backups',json={})
    assert backup.status_code==201,backup.text
    verified=client.post('/api/due-diligence/maintenance/backups/'+backup.json()['id']+'/verify',json={})
    assert verified.status_code==200,verified.text
    body=verified.json()
    assert body['active_workspace_changed'] is False
    assert any(name.startswith('s4_') and count>0 for name,count in body['table_counts'].items())
    assert any(name.startswith('s5_') and count>0 for name,count in body['table_counts'].items())
    assert any(name.startswith('s6_') and count>0 for name,count in body['table_counts'].items())
