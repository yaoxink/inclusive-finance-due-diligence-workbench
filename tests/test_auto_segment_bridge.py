"""Stable committed ASR becomes pending source text without a microphone/LLM."""
import sys
from pathlib import Path
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'shared-recording-core'))

from test_s3_domain import env, make_case, values
from due_app.followups import FollowupService
from due_app.diligence import DiligenceError
from asr_bridge_fixture import seed_archive, advance_segment


class Adapter:
    def generate(self,value): return {'suggestions':[]}


def service(env): return FollowupService(env.store,env.service,Adapter())


def statements(env,cid): return env.service.workspace(cid)['statements']


def test_stable_asr_is_automatically_pending_and_offered_to_ai(env):
    cid=make_case(env)
    seed_archive(env.store,env.recordings,cid)
    seen=[]
    class Adapter:
        def generate(self,value): seen.append(value);return {'suggestions':[]}
    service=FollowupService(env.store,env.service,Adapter())
    service.configure(cid,True,0)
    service.analyze(cid)
    statements=env.service.workspace(cid)['statements']
    assert len(statements)==1
    assert statements[0]['kind']=='asr_segment'
    assert statements[0]['record_confirmation']=='pending'
    assert statements[0]['subject_id'] is None and statements[0]['speaker_role']=='unknown'
    assert seen[0]['statements'][0]['id']==statements[0]['id']
    assert not any(f['origin']=='onsite' for f in env.service.workspace(cid)['facts'])


def test_polling_replay_and_recreated_services_do_not_duplicate_source(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    first=service(env);first.list(cid)
    sid=statements(env,cid)[0]['id']
    for _ in range(3):
        assert service(env).list(cid)['transcription_bridge']['statement_ids']==[sid]
    assert len(statements(env,cid))==1


def test_parallel_polling_is_atomically_idempotent(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    services=[service(env) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda s:s.list(cid),services))
    assert len(statements(env,cid))==1
    assert len({r['transcription_bridge']['statement_ids'][0] for r in results})==1


def test_existing_manual_asr_metadata_and_confirmation_are_reused_not_overwritten(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    request={k:identity[k] for k in ('interview_id','recording_id','segment_id','segment_revision')}
    original=env.service.statement_from_segment(cid,{**request,'speaker_role':'applicant',
        'speaker_label':'合成人员','subject_id':'C1-HOUSEHOLD','subject_basis':'人工明确的合成家庭归属'})
    env.service.confirm_statement(cid,original['id'],1)
    before=statements(env,cid)[0]
    result=service(env).list(cid)
    assert statements(env,cid)==[before]
    assert result['transcription_bridge']['statement_ids']==[original['id']]
    assert before['speaker_role']=='applicant' and before['record_confirmation']=='record_accurate'


def test_new_revision_keeps_old_source_and_fact_and_invalidates_old_analysis(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    s=service(env);s.analyze(cid)
    old=statements(env,cid)[0]
    env.service.confirm_statement(cid,old['id'],1)
    fact=env.service.create_fact(cid,values(old))
    env.service.confirm_fact(cid,fact['id'],1,'合成记录核对')
    baseline=s.analyze(cid)
    advance_segment(env.store,env.recordings,identity,text='合成修订：家庭五口人。')
    new=s.list(cid)
    after=statements(env,cid)
    assert len(after)==2
    assert after[0]['text']==old['text'] and after[0]['needs_review'] is True
    assert after[1]['source_ref']['segment_revision']==2
    assert after[1]['record_confirmation']=='pending' and after[1]['subject_id'] is None
    assert new['fingerprint']!=baseline['fingerprint'] and not new['analysis_current']
    saved=env.service.fact(cid,fact['id'])
    assert saved['value']=='4' and saved['revision']==2 and saved['needs_review']
    assert saved['verification']=='not_checked'


@pytest.mark.parametrize('mode',['unstable','pending_frames','data_error'])
def test_unstable_uncommitted_or_storage_error_is_not_imported(env,mode):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    if mode=='unstable': advance_segment(env.store,env.recordings,identity,stable=False)
    elif mode=='pending_frames': advance_segment(env.store,env.recordings,identity,saved_frames=16000)
    else: advance_segment(env.store,env.recordings,identity,data_error='合成写盘错误')
    out=service(env).list(cid)
    assert not statements(env,cid)
    assert out['transcription_bridge']['observed_count']==0


def test_unstable_becomes_stable_without_client_import_or_duplicating_revision(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid,stable=False)
    s=service(env);assert s.list(cid)['transcription_bridge']['observed_count']==0
    advance_segment(env.store,env.recordings,identity,stable=True)
    s.list(cid)
    assert len(statements(env,cid))==1 and statements(env,cid)[0]['source_ref']['segment_revision']==1


def test_failed_asr_keeps_its_already_stable_committed_source(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    advance_segment(env.store,env.recordings,identity,asr_state='failed')
    out=service(env).list(cid)
    assert len(statements(env,cid))==1 and out['transcription_bridge']['state']=='ready'


def test_denied_case_never_enters_recording_reader_or_creates_source(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    env.service.load_previsit(cid,'SYN-S0-001','denied')
    def forbidden(*args): raise AssertionError('denied must not read recording')
    env.recordings.get=forbidden
    with pytest.raises(DiligenceError) as exc: service(env).list(cid)
    assert exc.value.status==403
    with env.store.connect() as c: assert c.execute('SELECT COUNT(*) FROM s3_statements').fetchone()[0]==0


def test_permission_revoked_between_resolve_and_write_prevents_import(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    resolve=env.service._resolve_segment
    def revoke(*args):
        value=resolve(*args)
        env.service.load_previsit(cid,'SYN-S0-001','denied')
        return value
    env.service._resolve_segment=revoke
    with pytest.raises(DiligenceError) as exc: service(env).list(cid)
    assert exc.value.status==403
    with env.store.connect() as c: assert c.execute('SELECT COUNT(*) FROM s3_statements').fetchone()[0]==0


def test_cross_case_sources_are_never_read_or_imported(env):
    a=make_case(env);b=make_case(env)
    seed_archive(env.store,env.recordings,a,text='合成A案')
    seed_archive(env.store,env.recordings,b,text='合成B案')
    calls=[];get=env.recordings.get
    def reader(cid,*args): calls.append(cid);return get(cid,*args)
    env.recordings.get=reader
    service(env).list(a)
    assert set(calls)=={a} and statements(env,a)[0]['text']=='合成A案'
    assert not statements(env,b)


def test_recording_reader_is_never_called_under_store_lock(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    get=env.recordings.get
    def reader(*args):
        assert not env.store.lock._is_owned()
        return get(*args)
    env.recordings.get=reader
    service(env).list(cid)
    assert len(statements(env,cid))==1


def test_unavailable_one_recording_is_partial_and_does_not_hide_other_rules(env):
    cid=make_case(env)
    bad=seed_archive(env.store,env.recordings,cid,text='合成坏来源')
    good=seed_archive(env.store,env.recordings,cid,text='合成正常来源')
    get=env.recordings.get
    def reader(case_id,interview_id,recording_id):
        if recording_id==bad['recording_id']: raise HTTPException(409,'合成错误根')
        return get(case_id,interview_id,recording_id)
    env.recordings.get=reader
    out=service(env).analyze(cid)
    assert out['transcription_bridge']['state']=='partial' and out['active_cards']
    assert len(statements(env,cid))==1 and statements(env,cid)[0]['text']=='合成正常来源'


def test_missing_or_tampered_committed_chunks_do_not_import_or_confirm(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    path=identity['config'].resolve_output('chunks/00000001.wav')
    path.write_bytes(b'invalid synthetic chunk')
    out=service(env).list(cid)
    assert out['transcription_bridge']['state']=='partial' and not statements(env,cid)


def test_same_revision_run_mismatch_is_not_silently_reused(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    row=env.recordings.repository.get(cid,identity['interview_id'],identity['recording_id'])
    row['snapshot']['segments'][0]['source_run_id']='different-synthetic-run'
    env.recordings.repository.save(row['snapshot'])
    out=service(env).list(cid)
    assert out['transcription_bridge']['observed_count']==0 and not statements(env,cid)


@pytest.mark.parametrize('change',['revision','new_recording'])
def test_old_response_after_new_stable_asr_cannot_be_saved(env,change):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    class Advancing:
        def generate(self,value):
            if change=='revision':
                advance_segment(env.store,env.recordings,identity,text='本次分析期间新增合成修订。')
            else:
                seed_archive(env.store,env.recordings,cid,text='推理期间新增完全不同录音来源。')
            return {'suggestions':[]}
    s=FollowupService(env.store,env.service,Advancing());s.configure(cid,True,0)
    with pytest.raises(DiligenceError) as exc: s.analyze(cid)
    assert exc.value.code=='source_changed'
    assert len(statements(env,cid))==2
    assert not s.list(cid)['analysis_current']


def test_window_uses_newest_stable_sources_and_never_backfills_old_history(env):
    cid=make_case(env)
    for i in range(7): seed_archive(env.store,env.recordings,cid,text=f'合成按时间第{i}段')
    # Recent unstable items cannot crowd committed stable items out of the window.
    for i in range(4): seed_archive(env.store,env.recordings,cid,text=f'合成待稳定{i}',stable=False)
    seen=[]
    class Collect:
        def generate(self,value):seen.append(value);return {'suggestions':[]}
    s=FollowupService(env.store,env.service,Collect());s.configure(cid,True,0);s.analyze(cid)
    assert [v['text'] for v in statements(env,cid)]==[f'合成按时间第{i}段' for i in range(3,7)]
    assert [v['text_excerpt'] for v in seen[0]['statements']]==['合成按时间第5段','合成按时间第6段']
    s.list(cid);s.list(cid)
    assert len(statements(env,cid))==4


def test_adding_audio_frames_or_level_without_new_text_keeps_analysis_current(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    s=service(env);before=s.analyze(cid)
    advance_segment(env.store,env.recordings,identity,level=55,saved_seconds=3,saved_frames=48000)
    after=s.list(cid)
    assert after['analysis_current'] and after['fingerprint']==before['fingerprint']


def test_permission_revoked_after_snapshot_still_blocks_card_body(env):
    cid=make_case(env);seed_archive(env.store,env.recordings,cid)
    s=service(env);snapshot=s._snapshot
    def revoke(*args,**kwargs):
        value=snapshot(*args,**kwargs)
        env.service.load_previsit(cid,'SYN-S0-001','denied')
        return value
    s._snapshot=revoke
    with pytest.raises(DiligenceError) as exc:s.list(cid)
    assert exc.value.status==403


def test_archive_corruption_after_import_is_rechecked_and_blocks_confirmation(env):
    cid=make_case(env);identity=seed_archive(env.store,env.recordings,cid)
    s=service(env);before=s.analyze(cid)
    source=statements(env,cid)[0]
    path=identity['config'].resolve_output('chunks/00000001.wav')
    content=bytearray(path.read_bytes());content[-1]^=1;path.write_bytes(content)
    out=s.list(cid)
    assert not out['analysis_current'] and out['fingerprint']!=before['fingerprint']
    assert statements(env,cid)[0]['needs_review']
    with pytest.raises(DiligenceError) as exc:env.service.confirm_statement(cid,source['id'],1)
    assert exc.value.code=='source_changed'


def test_recent_s4_window_does_not_revalidate_unrelated_old_statement_audio(env):
    cid=make_case(env)
    old=seed_archive(env.store,env.recordings,cid,text='更早归档不在自动窗口')
    req={key:old[key] for key in ('interview_id','recording_id','segment_id','segment_revision')}
    old_source=env.service.statement_from_segment(cid,req)
    for i in range(6):seed_archive(env.store,env.recordings,cid,text=f'当前合成段{i}')
    calls=[];get=env.recordings.get
    def reader(c,i,r):calls.append(r);return get(c,i,r)
    env.recordings.get=reader
    s=service(env);s.list(cid);calls.clear();s.list(cid)
    assert old['recording_id'] not in calls
    # Normal source detail remains strict even when S4 excludes this old source.
    old['config'].resolve_output('chunks/00000001.wav').unlink()
    assert env.service.statement(cid,old_source['id'])['needs_review']
