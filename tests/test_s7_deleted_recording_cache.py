"""Deleted-case replay/monitor races, using only FakeSupervisor archives."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import sqlite3
import threading
import time

import pytest

from test_s7_retention import r_env,env,case,load,scope,populate,preview,delete
from due_app.recordings import RecordingManager
from due_app.store import DueStore


def recording_path(cid,rec):
    return scope(cid)+'/interviews/'+rec['interview_id']+'/recordings/'+rec['id']


def test_deleted_case_evicts_all_recording_views_after_store_lock_is_released(r_env,monkeypatch):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    manager=app.state.recordings;original=manager.forget_deleted_recordings;called=[]
    def checked(case_id):
        assert not app.state.store.lock._is_owned()
        called.append(case_id);original(case_id)
    monkeypatch.setattr(manager,'forget_deleted_recordings',checked)
    assert delete(client,cid).status_code==200
    assert called==[cid]
    for cache in [manager.snapshots,manager.configs,manager._segments,manager._hashes,manager._deferred,manager.supervisors]:
        assert rec['id'] not in cache
    route=recording_path(cid,rec)
    for suffix in ['', '/audio','/segments/history']:
        response=client.get(route+suffix)
        assert response.status_code==404,response.text
        assert '虚构语音片段' not in response.text
    for suffix in ['/stop','/transcription/retry']:
        assert client.post(route+suffix,json={}).status_code==404
    base=scope(cid)+'/interviews/'+rec['interview_id']+'/recordings'
    assert client.get(base).status_code==404
    assert client.post(base+'/start',json={'device_id':1}).status_code==404


def test_idle_cache_replay_checks_durable_tombstone_if_publication_was_unavailable(r_env,monkeypatch):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    manager=app.state.recordings
    monkeypatch.setattr(manager,'mark_case_deleted',lambda cid:None)
    monkeypatch.setattr(manager,'forget_deleted_recordings',lambda cid:None)
    assert delete(client,cid).status_code==200
    assert rec['id'] in manager.snapshots  # deliberate stale cache fault injection
    response=client.post(recording_path(cid,rec)+'/stop',json={})
    assert response.status_code==404,response.text


def test_restart_loads_tombstones_before_any_cached_recording_can_be_used(r_env):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    old=dict(app.state.recordings.snapshots[rec['id']])
    assert delete(client,cid).status_code==200
    restarted=RecordingManager(DueStore(app.state.store.root),app.state.settings)
    restarted.snapshots[rec['id']]=old
    assert restarted._case_deleted(cid)
    with pytest.raises(Exception) as exc:restarted.get(cid,rec['interview_id'],rec['id'])
    assert exc.value.status_code==404


def test_old_refresh_cannot_refill_cache_after_deletion_commit(r_env,monkeypatch):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    manager=app.state.recordings;worker=manager.supervisors[rec['id']]
    entered,release=threading.Event(),threading.Event();original=worker.snapshot
    def delayed():
        entered.set();assert release.wait(10)
        return original()
    monkeypatch.setattr(worker,'snapshot',delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh=pool.submit(manager.refresh,rec['id'])
        assert entered.wait(5)
        deletion=pool.submit(delete,client,cid)
        try:
            end=time.monotonic()+5
            while not manager._case_deleted(cid) and time.monotonic()<end:time.sleep(.01)
            assert manager._case_deleted(cid),'deletion could not publish while refresh held state lock'
        finally:release.set()
        assert refresh.result(5) is None
        result=deletion.result(5)
        assert result.status_code==200,result.text
    assert rec['id'] not in manager.snapshots
    assert manager.refresh(rec['id']) is None
    assert client.post(recording_path(cid,rec)+'/stop',json={}).status_code==404


def test_postcommit_journal_failure_still_publishes_tombstone_and_clears_cache(r_env,monkeypatch):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    original=app.state.retention._journal
    def fail_commit(folder,value):
        if value['state']=='committed':raise OSError('synthetic marker failure')
        return original(folder,value)
    monkeypatch.setattr(app.state.retention,'_journal',fail_commit)
    result=delete(client,cid)
    assert result.status_code==202,result.text
    manager=app.state.recordings
    assert manager._case_deleted(cid) and rec['id'] not in manager.snapshots
    def offline(*args,**kwargs):raise sqlite3.OperationalError('synthetic database unavailable')
    monkeypatch.setattr(manager.repository,'get',offline)
    assert client.get(recording_path(cid,rec)).status_code==404
    assert client.post(recording_path(cid,rec)+'/stop',json={}).status_code==404


def test_old_idle_stop_response_cannot_repopulate_cache_after_delete(r_env,monkeypatch):
    app,client,_=r_env;cid=case(client,'待删除的虚构案件');_,rec=populate(app,client,cid,True)
    manager=app.state.recordings;worker=manager.supervisors[rec['id']]
    entered,release=threading.Event(),threading.Event();original=worker.stop
    async def paused_stop():
        entered.set()
        end=time.monotonic()+10
        while not release.is_set() and time.monotonic()<end:await asyncio.sleep(.01)
        assert release.is_set()
        return await original()
    monkeypatch.setattr(worker,'stop',paused_stop)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_stop=pool.submit(client.post,recording_path(cid,rec)+'/stop',json={})
        assert entered.wait(5)
        try:
            result=delete(client,cid)
            assert result.status_code==200,result.text
        finally:release.set()
        stopped=old_stop.result(5)
        assert stopped.status_code==404,stopped.text
    assert rec['id'] not in manager.snapshots
    assert manager.refresh(rec['id']) is None
