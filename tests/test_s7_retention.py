"""Disposable synthetic deletion fixtures only; no real device, model or live data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest

from test_s3_http_review import env,case,load,scope,statement,fact_body,post_ok
from due_app.diligence import DiligenceError
from due_app.instance_lock import InstanceLock
from due_app.retention_routes import install_retention_routes


@pytest.fixture
def r_env(env):
    app,client=env
    if not hasattr(app.state,'retention'):
        install_retention_routes(app,app.state.store,app.state.recordings,app.state.maintenance)
    clock=[datetime(2026,10,31,8,tzinfo=timezone.utc)]
    app.state.retention.clock=lambda:clock[0]
    return app,client,clock


def preview(client,cid):
    response=client.get(scope(cid)+'/deletion-preview')
    assert response.status_code==200,response.text
    return response.json()


def delete(client,cid,p=None,title='待删除的虚构案件'):
    p=p or preview(client,cid)
    return client.post(scope(cid)+'/delete',json={'expected_fingerprint':p['fingerprint'],'confirm_title':title})


def populate(app,client,cid,with_recording=False):
    load(client,cid)
    src=statement(client,cid,'仅虚构：家庭4人，秘密标记 SYNTHETIC-ERASE-ME-'+cid+'。')
    post_ok(client,scope(cid)+'/statements/'+src['id']+'/confirm',{'expected_revision':1})
    value=post_ok(client,scope(cid)+'/facts',fact_body(src))
    post_ok(client,scope(cid)+'/facts/'+value['id']+'/confirm',{'expected_revision':1})
    post_ok(client,scope(cid)+'/followups/analyze',{})
    post_ok(client,scope(cid)+'/calculation/run',{})
    report=post_ok(client,scope(cid)+'/reports',{})
    if with_recording:
        interview=post_ok(client,scope(cid)+'/interviews',{'title':'纯合成录音fixture'})
        root=scope(cid)+'/interviews/'+interview['id']+'/recordings'
        rec=post_ok(client,root+'/start',{'device_id':1})
        worker=app.state.recordings.supervisors[rec['id']]
        worker.segments('虚构语音片段：四口人。')
        post_ok(client,root+'/'+rec['id']+'/stop',{})
        worker.raw.update(asr_state='completed')
        app.state.recordings.refresh(rec['id'])
        app.state.recordings.repository.event(rec['id'],{'producer':'capture','run_id':'synthetic-run',
            'seq':1,'event':'synthetic-audit-fixture','payload':{'case_marker':cid}})
        post_ok(client,scope(cid)+'/statements/from-segment',{'interview_id':interview['id'],
            'recording_id':rec['id'],'segment_id':'fixture-segment','segment_revision':1})
        return report,rec
    return report,None


def backup(client):
    return post_ok(client,'/api/due-diligence/maintenance/backups',{})


def test_delete_covers_all_domains_audio_history_restore_and_old_backup(r_env):
    app,client,clock=r_env
    a=case(client,'待删除的虚构案件');b=case(client,'保持可用的虚构案件')
    ar,rec=populate(app,client,a,True);br,_=populate(app,client,b)
    manual=post_ok(client,scope(a)+'/reports/'+ar['id']+'/edits',{'expected_revision':ar['revision'],
        'section_id':'section-1','text':'合成人工修订版本，只属于待删除案。','basis':'删除范围检查'})
    pack=backup(client)
    recovered=post_ok(client,'/api/due-diligence/maintenance/backups/'+pack['id']+'/verify',{})
    p=preview(client,a)
    assert p['can_delete'] and p['backups'][0]['id']==pack['id']
    assert set(p['restore_copies'][0]['case_ids'])=={a,b}
    assert p['restore_copies'][0]['will_remove_entire_copy'] is True
    assert p['counts']['transcript_revisions']>0 and p['counts']['recording_events']>0 and p['counts']['s6_reports']==2
    assert p['shared_report_assets'] and not p['exclusive_report_assets']
    result=delete(client,a,p)
    assert result.status_code==200,result.text
    assert result.json()['state']=='deleted'
    assert result.json()['backup_expires_at']=='2026-11-07T08:00:00+00:00'
    for path in ['/diligence','/statements/'+p['case_id'],'/followups','/calculation','/reports',
                 '/reports/'+ar['id']+'/export.docx','/reports/'+ar['id']+'/evidence.json']:
        assert client.get(scope(a)+path).status_code==404,path
    assert client.get(scope(a)+'/reports/'+manual['id']+'/export.docx').status_code==404
    assert app.state.store.case(a) is None
    assert not (app.state.store.root/'recordings'/rec['id']).exists()
    assert not (Path(recovered['restored_directory'])/'due.sqlite3').exists()
    assert client.get(scope(b)+'/diligence').status_code==200
    assert client.get(scope(b)+'/reports/'+br['id']+'/export.docx').status_code==200
    with app.state.store.connect() as c:
        for table in ['s3_facts','s3_statements','s4_cards','s5_runs','s6_reports','recordings','interviews']:
            assert c.execute('SELECT COUNT(*) FROM '+table+' WHERE case_id=?',(a,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM transcript_revisions WHERE recording_id=?',(rec['id'],)).fetchone()[0]==0
        audit=dict(c.execute('SELECT * FROM s7_deleted_cases WHERE case_id=?',(a,)).fetchone())
        assert '待删除' not in json.dumps(audit,ensure_ascii=False) and 'SYNTHETIC-ERASE' not in json.dumps(audit)
    # secure_delete and the successful WAL checkpoint remove the synthetic
    # plaintext marker from the active database files, not from retained ZIPs.
    for path in [app.state.store.path,Path(str(app.state.store.path)+'-wal')]:
        if path.exists(): assert ('SYNTHETIC-ERASE-ME-'+a).encode() not in path.read_bytes()
    listed=client.get('/api/due-diligence/maintenance/backups').json()['backups'][0]
    assert listed['restricted'] and listed['expires_at']==result.json()['backup_expires_at']
    old='/api/due-diligence/maintenance/backups/'+pack['id']
    assert client.get(old+'/download').status_code==403
    assert client.post(old+'/verify',json={}).status_code==403
    later=backup(client)
    with zipfile.ZipFile(app.state.maintenance._path(later['id'])) as z:
        assert a not in json.loads(z.read('manifest.json'))['case_ids']
        assert b in json.loads(z.read('manifest.json'))['case_ids']


def test_seven_days_before_at_and_after_deadline_with_retry(r_env,monkeypatch):
    app,client,clock=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    pack=backup(client);path=app.state.store.root/'backups'/(pack['id']+'.zip')
    assert delete(client,a).status_code==200
    clock[0]+=timedelta(days=7,microseconds=-1)
    assert post_ok(client,'/api/due-diligence/retention/cleanup',{})['backups_purged']==0
    assert path.is_file()
    clock[0]+=timedelta(microseconds=1)
    original=app.state.retention._remove
    def locked(p):
        if p==path: raise PermissionError('synthetic locked backup')
        return original(p)
    monkeypatch.setattr(app.state.retention,'_remove',locked)
    failed=post_ok(client,'/api/due-diligence/retention/cleanup',{})
    assert failed['errors'] and path.is_file()
    assert client.get('/api/due-diligence/maintenance/backups/'+pack['id']+'/download').status_code==403
    assert app.state.retention.backup_metadata(pack['id'])['restricted']
    monkeypatch.setattr(app.state.retention,'_remove',original)
    assert post_ok(client,'/api/due-diligence/retention/cleanup',{})['backups_purged']==1
    assert not path.exists()
    assert post_ok(client,'/api/due-diligence/retention/cleanup',{})['backups_purged']==0


@pytest.mark.parametrize('error',['title','fingerprint','other_case'])
def test_confirmation_and_coverage_fingerprint_guard(r_env,error):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a);p=preview(client,a)
    cid=a;title='待删除的虚构案件'
    if error=='title': title='错误案件名称'
    elif error=='fingerprint': statement(client,a,'预览后新添虚构原话。')
    else: cid=case(client,title);load(client,cid)
    response=delete(client,cid,p,title)
    assert response.status_code in {409,422}
    assert app.state.store.case(a) is not None and app.state.store.case(cid) is not None


@pytest.mark.parametrize('kind',['capture','asr'])
def test_busy_recording_or_asr_prevents_delete(r_env,monkeypatch,kind):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a);p=preview(client,a)
    if kind=='capture': monkeypatch.setattr(app.state.recordings,'active',lambda:{'id':'synthetic-busy'})
    else: monkeypatch.setattr(app.state.recordings,'asr_busy_count',lambda:1)
    assert delete(client,a,p).status_code==409
    assert app.state.store.case(a)


@pytest.mark.parametrize('unknown',['table','export','recording'])
def test_unknown_coverage_blocks_instead_of_guessing(r_env,unknown):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    if unknown=='table':
        with app.state.store.connect() as c:c.execute('CREATE TABLE future_case_notes(case_id TEXT,body TEXT)')
        response=client.get(scope(a)+'/deletion-preview')
        assert response.status_code==409 and response.json()['detail']['code']=='unknown_case_tables'
        return
    folder=app.state.store.root/('exports' if unknown=='export' else 'recordings')
    (folder/'unknown.txt').write_text('仅虚构未知文件',encoding='utf-8')
    p=preview(client,a)
    assert p['blockers'] and not p['can_delete']
    assert delete(client,a,p).status_code==409 and app.state.store.case(a)


def test_restore_in_use_blocks_and_other_active_cases_untouched(r_env):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    pack=backup(client)
    restored=post_ok(client,'/api/due-diligence/maintenance/backups/'+pack['id']+'/verify',{})
    with InstanceLock(Path(restored['restored_directory'])):
        p=preview(client,a)
        assert any(x['code']=='restore_in_use' for x in p['blockers'])
        assert delete(client,a,p).status_code==409


def test_legacy_backup_without_case_ids_and_false_manifest_ids_are_checked_from_database(r_env):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a);pack=backup(client)
    p=app.state.store.root/'backups'/(pack['id']+'.zip')
    with zipfile.ZipFile(p) as z:members={i.filename:z.read(i.filename) for i in z.infolist()}
    manifest=json.loads(members['manifest.json']);manifest['case_ids']=[]
    members['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(p,'w') as z:
        for name,body in members.items():z.writestr(name,body)
    assert preview(client,a)['backups'][0]['id']==pack['id']
    assert delete(client,a).status_code==200
    assert client.get('/api/due-diligence/maintenance/backups/'+pack['id']+'/download').status_code==403


def test_file_prepare_failure_rolls_back_database_and_files(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');report,rec=populate(app,client,a,True)
    p=preview(client,a);original=app.state.retention._move;calls=[0]
    def fail_second(source,target):
        calls[0]+=1
        if calls[0]==2:raise OSError('synthetic file move failure')
        return original(source,target)
    monkeypatch.setattr(app.state.retention,'_move',fail_second)
    response=delete(client,a,p)
    assert response.status_code==409,response.text
    assert app.state.store.case(a)
    assert (app.state.store.root/'recordings'/rec['id']).is_dir()
    assert client.get(scope(a)+'/reports/'+report['id']+'/export.docx').status_code==200


def test_postcommit_cleanup_failure_is_pending_then_retry_completes(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    original=app.state.retention._remove
    def locked(p):
        if p.name=='payload':raise PermissionError('synthetic payload locked')
        return original(p)
    monkeypatch.setattr(app.state.retention,'_remove',locked)
    response=delete(client,a)
    assert response.status_code==202 and response.json()['state']=='cleanup_pending',response.text
    assert app.state.store.case(a) is None
    assert client.get(scope(a)+'/reports').status_code==404
    refused=client.post('/api/due-diligence/maintenance/backups',json={})
    assert refused.status_code==409 and refused.json()['detail']['code']=='backup_retention_pending'
    monkeypatch.setattr(app.state.retention,'_remove',original)
    assert post_ok(client,'/api/due-diligence/retention/cleanup',{})['jobs_completed']==1
    assert app.state.retention.status()['deletions'][0]['state']=='completed'
    assert client.post('/api/due-diligence/maintenance/backups',json={}).status_code==201


def test_inflight_old_ai_analysis_cannot_resurrect_deleted_case(r_env):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    entered,release=threading.Event(),threading.Event()
    class Waiting:
        def generate(self,source):
            entered.set();assert release.wait(10)
            return {'suggestions':[]}
    app.state.followups.adapter=Waiting()
    app.state.followups.configure(a,True,0)
    with ThreadPoolExecutor() as pool:
        future=pool.submit(app.state.followups.analyze,a)
        assert entered.wait(5)
        assert delete(client,a).status_code==200
        release.set()
        with pytest.raises(DiligenceError) as exc:future.result(5)
        assert exc.value.status==404
    assert app.state.store.case(a) is None


def test_denied_case_can_be_erased_without_exposing_body(r_env):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    load(client,a,variant='denied')
    p=preview(client,a)
    assert 'SYNTHETIC-ERASE-ME' not in json.dumps(p)
    assert delete(client,a,p).status_code==200
    assert client.get(scope(a)+'/diligence').status_code==404


def test_interrupted_uncommitted_preparation_is_recovered_without_deleting_case(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');report,rec=populate(app,client,a,True)
    ret=app.state.retention;p=preview(client,a);original=ret._move;calls=[0]
    class SimulatedProcessExit(BaseException): pass
    def interrupted(source,target):
        calls[0]+=1
        if calls[0]==2:raise SimulatedProcessExit()
        return original(source,target)
    monkeypatch.setattr(ret,'_move',interrupted)
    # This direct call simulates process exit while the HTTP guard is already held;
    # capture behavior has separate HTTP checks and is not invoked here.
    monkeypatch.setattr(ret,'_require_guard',lambda:None)
    with pytest.raises(SimulatedProcessExit):ret.delete(a,p['fingerprint'],'待删除的虚构案件')
    assert app.state.store.case(a)
    assert not (app.state.store.root/'recordings'/rec['id']).exists()
    monkeypatch.setattr(ret,'_move',original)
    assert ret.cleanup()['errors']==[]
    assert (app.state.store.root/'recordings'/rec['id']).is_dir()
    assert client.get(scope(a)+'/reports/'+report['id']+'/export.docx').status_code==200
    assert ret.status()['deletions']==[]


def test_altered_committed_journal_blocks_cleanup_until_repaired(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    ret=app.state.retention;original=ret._remove
    monkeypatch.setattr(ret,'_remove',lambda p: (_ for _ in ()).throw(PermissionError()) if p.name=='payload' else original(p))
    assert delete(client,a).status_code==202
    monkeypatch.setattr(ret,'_remove',original)
    folder=next(ret.jobs.glob('delete_*'));path=folder/'journal.json';saved=path.read_bytes()
    journal=json.loads(saved);journal['moves'][0]['source']='exports/report-assets/'+'0'*64+'.png'
    path.write_text(json.dumps(journal),encoding='utf-8')
    result=ret.cleanup()
    assert result['errors'] and (folder/'payload').exists()
    assert ret.status()['deletions'][0]['state']=='cleanup_pending'
    path.write_bytes(saved)
    assert ret.cleanup()['jobs_completed']==1


def test_later_case_deletion_does_not_extend_earlier_whole_backup_deadline(r_env):
    app,client,clock=r_env;a=case(client,'待删除的虚构案件');b=case(client,'后删除的虚构案件')
    load(client,a);load(client,b);pack=backup(client)
    assert delete(client,a).status_code==200
    first=app.state.retention.backup_metadata(pack['id'])['expires_at']
    clock[0]+=timedelta(days=3)
    assert delete(client,b,title='后删除的虚构案件').status_code==200
    assert app.state.retention.backup_metadata(pack['id'])['expires_at']==first
    assert app.state.store.case(b) is None


def test_linked_recording_tree_is_rejected_without_touching_external_target(r_env,tmp_path):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    outside=tmp_path/'external';outside.mkdir();keep=outside/'keep.txt';keep.write_text('保留',encoding='utf-8')
    link=app.state.store.root/'recordings'/'linked'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:
        import subprocess
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
    try:
        response=client.get(scope(a)+'/deletion-preview')
        assert response.status_code==409
        assert keep.read_text(encoding='utf-8')=='保留' and app.state.store.case(a)
    finally:
        if link.is_symlink():link.unlink()
        elif link.exists():link.rmdir()


def test_postcommit_marker_failure_returns_202_then_later_cleanup_is_recoverable(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');populate(app,client,a)
    ret=app.state.retention;original=ret._journal
    def fail_marker(folder,value):
        if value['state']=='committed':raise OSError('synthetic postcommit marker failure')
        return original(folder,value)
    monkeypatch.setattr(ret,'_journal',fail_marker)
    response=delete(client,a)
    assert response.status_code==202,response.text
    assert response.json()['state']=='cleanup_pending' and response.json()['active_case_closed']
    assert app.state.store.case(a) is None and client.get(scope(a)+'/diligence').status_code==404
    assert ret.status()['deletions'][0]['state']=='cleanup_pending'
    refused=client.post('/api/due-diligence/maintenance/backups',json={})
    assert refused.status_code==409 and refused.json()['detail']['code']=='backup_retention_pending'
    monkeypatch.setattr(ret,'_journal',original)
    assert post_ok(client,'/api/due-diligence/retention/cleanup',{})['jobs_completed']==1
    assert ret.status()['deletions'][0]['state']=='completed'
    assert client.post('/api/due-diligence/maintenance/backups',json={}).status_code==201


def test_unexpected_cleanup_exception_after_commit_also_returns_202(r_env,monkeypatch):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    ret=app.state.retention;original=ret.cleanup
    monkeypatch.setattr(ret,'cleanup',lambda:(_ for _ in ()).throw(RuntimeError('synthetic cleanup crash')))
    response=delete(client,a)
    assert response.status_code==202,response.text
    assert response.json()['state']=='cleanup_pending' and app.state.store.case(a) is None
    monkeypatch.setattr(ret,'cleanup',original)
    assert ret.cleanup()['jobs_completed']==1


@pytest.mark.parametrize('kind',['audit_only','files_only'])
def test_backup_refuses_pending_audit_or_residual_job_independently(r_env,kind):
    app,client,_=r_env
    if kind=='files_only':
        (app.state.retention.jobs/'unknown-residual.txt').write_text('合成残余准备标记',encoding='utf-8')
    else:
        with app.state.store.connect() as c:
            c.execute('INSERT INTO s7_deleted_cases VALUES (?,?,?,?,?,?,?,?,?)',
                ('case_disposable_deleted','2026-10-31T08:00:00+00:00','2026-11-07T08:00:00+00:00',
                 'case-delete-backup-seven-days/1','{}','[]','delete_'+'0'*16,'cleanup_pending',None))
    response=client.post('/api/due-diligence/maintenance/backups',json={})
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='backup_retention_pending'
    assert list(app.state.maintenance.folder.glob('backup_*.zip'))==[]


def test_legacy_package_with_pending_audit_and_no_job_is_rejected_on_restore(r_env,tmp_path):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    pack=backup(client);path=app.state.maintenance._path(pack['id'])
    with zipfile.ZipFile(path) as z:members={i.filename:z.read(i.filename) for i in z.infolist()}
    db=tmp_path/'old-pending.sqlite3';db.write_bytes(members['due.sqlite3'])
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO s7_deleted_cases VALUES (?,?,?,?,?,?,?,?,?)',
            ('case_old_pending','2026-10-31T08:00:00+00:00','2026-11-07T08:00:00+00:00',
             'case-delete-backup-seven-days/1','{}','[]','delete_'+'1'*16,'cleanup_pending',None))
    # sqlite3's transaction context does not close the connection. Close it so
    # the fixture's WAL is checkpointed into the actual member we repackage.
    c.close()
    members['due.sqlite3']=db.read_bytes()
    manifest=json.loads(members['manifest.json'])
    for e in manifest['files']:
        if e['path']=='due.sqlite3':
            e['bytes']=len(members['due.sqlite3']);e['sha256']=hashlib.sha256(members['due.sqlite3']).hexdigest()
    manifest['table_counts']['s7_deleted_cases']+=1
    members['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(path,'w') as z:
        for name,body in members.items():z.writestr(name,body)
    response=client.post('/api/due-diligence/maintenance/backups/'+pack['id']+'/verify',json={})
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='backup_retention_pending'
    assert app.state.store.case(a) is not None


def start_and_close_restored_fixture(app,client):
    """Actually start a new temporary restored app; its constructors create dirs."""
    import importlib
    from fastapi.testclient import TestClient
    pack=backup(client)
    result=post_ok(client,'/api/due-diligence/maintenance/backups/'+pack['id']+'/verify',{})
    root=Path(result['restored_directory'])
    restored=importlib.import_module('due_app.main').create_app(data_root=root,
        recording_supervisor_factory=app.state.recordings.factory,capture_lease_probe=lambda:None)
    with TestClient(restored,raise_server_exceptions=False) as copy:
        assert copy.get('/api/due-diligence/cases').status_code==200
    return root


def test_previously_started_restore_empty_app_maintenance_folders_allow_covered_delete(r_env):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    restored=start_and_close_restored_fixture(app,client)
    for name in ('backups','retention-jobs'):
        assert (restored/name).is_dir() and list((restored/name).iterdir())==[]
    p=preview(client,a)
    assert p['can_delete'],p['blockers']
    entries=p['restore_copies'][0]['entries']
    assert (restored/'backups').relative_to(app.state.store.root).as_posix() in entries
    assert (restored/'retention-jobs').relative_to(app.state.store.root).as_posix() in entries
    result=delete(client,a,p)
    assert result.status_code==200,result.text
    assert not (restored/'due.sqlite3').exists()
    assert not (restored/'backups').exists() and not (restored/'retention-jobs').exists()


@pytest.mark.parametrize('name',['backups','retention-jobs','unknown-empty-folder'])
def test_restore_nested_nonempty_or_unknown_empty_directories_still_block(r_env,name):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    restored=start_and_close_restored_fixture(app,client)
    folder=restored/name;folder.mkdir(exist_ok=True)
    if name!='unknown-empty-folder':(folder/'synthetic-content.txt').write_text('仅合成，不能忽略。',encoding='utf-8')
    p=preview(client,a)
    assert not p['can_delete']
    assert {'code':'unknown_restore_content','path':folder.relative_to(app.state.store.root).as_posix()} in p['blockers']
    assert delete(client,a,p).status_code==409
    assert (restored/'due.sqlite3').is_file() and app.state.store.case(a)


@pytest.mark.parametrize('name',['backups','retention-jobs'])
def test_restore_known_empty_folder_name_cannot_be_a_link(r_env,tmp_path,name):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    restored=start_and_close_restored_fixture(app,client)
    link=restored/name
    assert link.is_dir() and not list(link.iterdir())
    link.rmdir()  # only this newly generated and verified-empty fixture directory
    outside=tmp_path/('outside-'+name);outside.mkdir()
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:
        import subprocess
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
    try:
        response=client.get(scope(a)+'/deletion-preview')
        assert response.status_code==409 and response.json()['detail']['code']=='unsafe_data_path'
        assert app.state.store.case(a) and list(outside.iterdir())==[]
    finally:
        if link.is_symlink():link.unlink()
        elif link.exists():link.rmdir()


@pytest.mark.parametrize('name',['backups','retention-jobs'])
def test_restore_service_folder_filled_after_inventory_is_not_removed(r_env,monkeypatch,name):
    app,client,_=r_env;a=case(client,'待删除的虚构案件');load(client,a)
    restored=start_and_close_restored_fixture(app,client)
    p=preview(client,a);assert p['can_delete']
    ret=app.state.retention;original=ret._preview;added=restored/name/'new-synthetic-content.txt'
    def changed_after_check(c,cid):
        result=original(c,cid)
        added.write_text('合成新增内容，不能被当成空目录处理。',encoding='utf-8')
        return result
    monkeypatch.setattr(ret,'_preview',changed_after_check)
    response=delete(client,a,p)
    assert response.status_code==409 and response.json()['detail']['code']=='coverage_changed'
    assert added.is_file() and (restored/'due.sqlite3').is_file() and app.state.store.case(a)
