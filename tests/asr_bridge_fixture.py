"""Owned synthetic archive builder for tests/private QA, never a production API."""
import copy
import json
import uuid

from due_app.config import APP_ID
from due_app.store import now
from shared_recording_core import SessionConfig
from shared_recording_core.archive import ArchiveWriter


def seed_archive(store, recordings, case_id, *, text='虚构测试：家庭四口人，医保三个人。',
                 stable=True, seconds=2, recording_id=None):
    interview = store.create_interview(case_id, '合成转写桥接 · 无麦克风')
    rid = recording_id or 'rec_bridge_' + uuid.uuid4().hex
    config = SessionConfig(app_id=APP_ID, instance_id='synthetic-bridge-only', session_id=rid,
                           output_root=(store.root / 'recordings').resolve(), device_id=1,
                           capture_format={'sample_rate':16000,'channels':1,'sample_width':2},
                           asr_config={'engine':'disabled'})
    writer = ArchiveWriter(config, 'synthetic-capture', source='synthetic')
    writer.started()
    for _ in range(seconds):
        writer.commit(b'\x01\x00' * 16000)
    writer.close()
    segment = {'id':'bridge-segment','text':text,'revision':1,'stable':stable,
               'start_seconds':0,'end_seconds':seconds,'source_run_id':'synthetic-asr'}
    stamp = now()
    snapshot = {'id':rid,'case_id':case_id,'interview_id':interview['id'],'device_id':1,
                'purpose':'synthetic-bridge-only','created_at':stamp,'state':'saved','capture_state':'saved',
                'saved_frames':seconds*16000,'saved_seconds':seconds,'sample_rate':16000,
                'started_at':stamp,'stopped_at':stamp,'last_saved_at':stamp,'audio_available':True,
                'error':None,'asr':{'state':'completed','error':None,'transcribed_frames':seconds*16000,
                                  'transcribed_seconds':seconds,'current_run_transcribed_frames':seconds*16000,
                                  'current_run_transcribed_seconds':seconds,'historical_transcribed_frames':seconds*16000,
                                  'checkpoint_run_id':'synthetic-asr','retry_queued':False},
                'segments':[segment],'timing':{},'level':0,'capture_diagnostics':{},
                'model_info':{'engine':'synthetic-fixture'},'offline_policy':{'fixture':True},
                'worker_pids':{},'transcript_is_from_previous_run':False,'data_error':None}
    recordings.repository.create(config.to_dict(), snapshot)
    recordings.repository.save_segment(rid, segment, 'synthetic-asr')
    return {'case_id':case_id,'interview_id':interview['id'],'recording_id':rid,
            'segment_id':segment['id'],'segment_revision':1,'config':config}


def advance_segment(store, recordings, identity, *, text=None, stable=None, asr_state=None, **changes):
    row=recordings.repository.get(identity['case_id'],identity['interview_id'],identity['recording_id'])
    snapshot=copy.deepcopy(row['snapshot']);segment=snapshot['segments'][0]
    if text is not None:
        segment['text']=text
        segment['revision']=recordings.repository.save_segment(snapshot['id'],segment,segment['source_run_id'])
    if stable is not None: segment['stable']=stable
    if asr_state is not None: snapshot['asr']['state']=asr_state
    snapshot.update(changes)
    recordings.repository.save(snapshot)
    return snapshot
