"""S2 interview recording adapter. Audio manifests, capture and ASR remain independent."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import unicodedata
import json
import math
import shutil
import sqlite3
import struct
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException

from .config import APP_ID
from .store import now
from .archive_paths import bound_archive_config
from .store import now

CAPTURE_ACTIVE = {'starting', 'recording', 'stopping'}
ASR_ACTIVE = {'not_started', 'loading', 'running', 'draining'}


def validated_punctuation(raw, segments, asr_state, transcribed, frames):
    """Use final punctuation only when it preserves the complete stable transcript."""
    final = raw.get('final_text')
    if (asr_state != 'completed' or transcribed != frames or not isinstance(final, str)
            or not final.strip() or any(not s.get('stable') for s in segments if s.get('text'))):
        return None
    original = ''.join(s['text'] for s in segments)
    def content(value):
        return ''.join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith('P'))
    return final if original and content(final) == content(original) else None


class RecordingRepository:
    def __init__(self, store):
        self.store = store
        with store.lock, store.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),
                interview_id TEXT NOT NULL REFERENCES interviews(id),app_id TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                context_json TEXT NOT NULL,snapshot_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS rec_interview ON recordings(case_id,interview_id,created_at);
              CREATE TABLE IF NOT EXISTS recording_events (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,recording_id TEXT NOT NULL REFERENCES recordings(id),
                event_key TEXT NOT NULL,event_json TEXT NOT NULL,
                UNIQUE(recording_id,event_key));
              CREATE TABLE IF NOT EXISTS transcript_revisions (
                recording_id TEXT NOT NULL REFERENCES recordings(id),segment_id TEXT NOT NULL,
                revision INTEGER NOT NULL,source_run_id TEXT,text TEXT NOT NULL,
                start_frame INTEGER NOT NULL,end_frame INTEGER NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(recording_id,segment_id,revision));
              CREATE INDEX IF NOT EXISTS transcript_rec_seg_rev ON transcript_revisions(recording_id,segment_id,revision);
            ''')

    def create(self, context, snapshot):
        with self.store.lock, self.store.connect() as connection:
            connection.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)',
                (snapshot['id'], snapshot['case_id'], snapshot['interview_id'], APP_ID,
                 snapshot['created_at'], now(), json.dumps(context, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False)))
        return snapshot

    def get(self, case_id, interview_id, recording_id):
        with self.store.lock, self.store.connect() as connection:
            row = connection.execute('SELECT * FROM recordings WHERE id=? AND case_id=? AND interview_id=? AND app_id=?',
                                     (recording_id, case_id, interview_id, APP_ID)).fetchone()
            if row is None:
                raise HTTPException(404, '本案访谈中不存在该录音。')
            return {'context': json.loads(row['context_json']), 'snapshot': json.loads(row['snapshot_json'])}

    def all(self):
        with self.store.lock, self.store.connect() as connection:
            return [{'context': json.loads(row['context_json']), 'snapshot': json.loads(row['snapshot_json'])}
                    for row in connection.execute('SELECT * FROM recordings WHERE app_id=? ORDER BY created_at DESC', (APP_ID,))]

    def list(self, case_id, interview_id):
        with self.store.lock, self.store.connect() as connection:
            return [json.loads(row['snapshot_json']) for row in connection.execute(
                'SELECT snapshot_json FROM recordings WHERE app_id=? AND case_id=? AND interview_id=? ORDER BY created_at DESC',
                (APP_ID, case_id, interview_id))]

    def save(self, snapshot):
        with self.store.lock, self.store.connect() as connection:
            connection.execute('UPDATE recordings SET updated_at=?,snapshot_json=? WHERE id=? AND app_id=?',
                               (now(), json.dumps(snapshot, ensure_ascii=False), snapshot['id'], APP_ID))
            connection.execute('UPDATE interviews SET state=?,capture_state=?,asr_state=? WHERE id=? AND case_id=? AND app_id=?',
                ('ready' if snapshot['state'] == 'starting' else 'recording' if snapshot['state'] in CAPTURE_ACTIVE else 'recorded',
                 snapshot['state'], snapshot['asr']['state'], snapshot['interview_id'], snapshot['case_id'], APP_ID))

    def event(self, recording_id, event):
        identity = f"{event.get('producer')}:{event.get('run_id')}:{event.get('seq')}"
        with self.store.lock, self.store.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO recording_events(recording_id,event_key,event_json) VALUES (?,?,?)',
                               (recording_id, identity, json.dumps(event, ensure_ascii=False)))

    def save_segment(self, recording_id, segment, source_run_id):
        with self.store.lock, self.store.connect() as connection:
            previous = connection.execute('SELECT * FROM transcript_revisions WHERE recording_id=? AND segment_id=? ORDER BY revision DESC LIMIT 1',
                                          (recording_id, segment['id'])).fetchone()
            start, end = round(segment['start_seconds']*16000), round(segment['end_seconds']*16000)
            if previous and (previous['text'], previous['start_frame'], previous['end_frame']) == (segment['text'], start, end):
                return previous['revision']
            revision = previous['revision'] + 1 if previous else 1
            connection.execute('INSERT INTO transcript_revisions VALUES (?,?,?,?,?,?,?,?)',
                (recording_id, segment['id'], revision, source_run_id, segment['text'], start, end, now()))
            return revision

    def history(self, recording_id):
        with self.store.lock, self.store.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT segment_id,revision,source_run_id,text,start_frame,end_frame,created_at FROM transcript_revisions WHERE recording_id=? ORDER BY segment_id,revision',
                (recording_id,))]


class RecordingManager:
    def __init__(self, store, settings, supervisor_factory=None, capture_lease_probe=None):
        from shared_recording_core.supervisor import Supervisor
        self.store, self.settings = store, settings
        self.repository = RecordingRepository(store)
        self.factory = supervisor_factory or Supervisor
        self.lease_probe = capture_lease_probe
        self.supervisors, self.configs, self.snapshots, self.monitors = {}, {}, {}, {}
        self._segments, self._hashes = {}, {}
        self._deferred = {}
        self._state_lock = threading.RLock()
        self._deletion_lock = threading.Lock()
        with store.lock, store.connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s7_deleted_cases'").fetchone()
            self._deleted_cases = {r[0] for r in connection.execute('SELECT case_id FROM s7_deleted_cases')} if exists else set()
        self._retired_supervisors = []
        self._closing = False
        self._stop_requested = set()
        self._capture_lock, self._asr_lock = asyncio.Lock(), asyncio.Lock()

    def _new_supervisor(self, recording_id=None):
        async def event_received(event):
            if recording_id and event.get('event') not in ('level', 'heartbeat', 'progress'):
                await asyncio.to_thread(self.repository.event, recording_id, event)
        return self.factory(python_path=self.settings.capture_python, asr_home=self.settings.asr_home,
                            on_event=event_received if recording_id else None)

    async def devices(self):
        worker = self._new_supervisor()
        try:
            values = await asyncio.wait_for(worker.list_devices(), 20)
        except Exception as exc:
            raise HTTPException(409, '无法读取本机麦克风，请检查设备及本地语音运行环境。') from exc
        devices = [{'id': int(item.get('index', item.get('id'))), 'name': item['name'],
                    'hostapi': item.get('hostapi', ''), 'is_default': bool(item.get('default', item.get('is_default', False)))}
                   for item in values]
        return {'devices': devices, 'default_device_id': next((item['id'] for item in devices if item['is_default']), None)}

    def _compose(self, recording_id, raw):
        old = self.snapshots[recording_id]
        states = {'not_started': 'starting', 'starting': 'starting', 'recording': 'recording',
                  'stopping': 'stopping', 'stopped': 'saved', 'completed': 'saved',
                  'interrupted': 'interrupted', 'failed': 'failed', 'error': 'failed'}
        state = states.get(raw.get('capture_state'), old['state'])
        error = raw.get('capture_error') or None
        if error and state == 'saved':
            state = 'failed'
        if recording_id in self._stop_requested and state in ('starting', 'recording'):
            state = 'stopping'
        frames = max(0, int(raw.get('saved_frames') or 0))
        transcribed = raw.get('current_run_transcribed_frames', raw.get('transcribed_frames')) or 0
        progress_invalid = type(transcribed) is not int or not 0 <= transcribed <= frames
        if progress_invalid:
            transcribed = 0
        checkpoint_run = raw.get('checkpoint_run_id')
        asr_state = raw.get('asr_state') or old['asr']['state']
        if asr_state not in ASR_ACTIVE | {'completed', 'failed', 'disabled'}:
            asr_state = 'failed'
        if recording_id in self._deferred:
            asr_state = 'not_started'
        segments, cache = [], self._segments.setdefault(recording_id, {})
        revision_error = None
        transcript_error = '转写进度无法验证；音频保存范围独立保留。' if progress_invalid else None
        for ordinal, item in enumerate(raw.get('segments') or [], 1):
            if not isinstance(item, dict):
                transcript_error = '部分转写片段无法验证，已保留音频并隐藏无效文字。'
                continue
            identifier = item.get('segment_id') or item.get('id') or f"frames_{item.get('start_frame', 0)}_{item.get('end_frame', 0)}"
            try:
                start = int(item['start_frame']) if 'start_frame' in item else round(float(item.get('start_seconds', 0))*16000)
                end = int(item['end_frame']) if 'end_frame' in item else round(float(item.get('end_seconds', 0))*16000)
            except (ValueError, TypeError, OverflowError):
                transcript_error = '部分转写片段无法验证，已保留音频并隐藏无效文字。'
                continue
            if not isinstance(identifier, str) or start < 0 or end < start or end > frames or not isinstance(item.get('text'), str):
                transcript_error = '部分转写片段无法验证，已保留音频并隐藏无效文字。'
                continue
            value = {'id': identifier, 'ordinal': ordinal, 'start_seconds': start/16000, 'end_seconds': end/16000,
                     'text': item['text'], 'stable': asr_state != 'loading' and end <= transcribed and
                         (not checkpoint_run or not item.get('verified_run_id') or item['verified_run_id'] == checkpoint_run),
                     'verified_run_id': item.get('verified_run_id'),
                     # Explicit null means legacy provenance is unknown. Do not
                     # attribute old text to the run that merely rechecked it.
                     'source_run_id': item['produced_run_id'] if 'produced_run_id' in item else item.get('run_id')}
            signature = (value['text'], start, end)
            previous = cache.get(identifier)
            if previous and previous['signature'] == signature:
                value['revision'] = previous['revision']
            else:
                try:
                    value['revision'] = self.repository.save_segment(recording_id, value, value['source_run_id'])
                except (OSError, sqlite3.DatabaseError):
                    value['revision'] = previous['revision'] if previous else int(item.get('revision') or 1)
                    revision_error = '文本修订尚未写入业务库；录音与转写清单保留，请检查存储。'
                else:
                    cache[identifier] = {'signature': signature, 'revision': value['revision']}
            segments.append(value)
        available = bool(raw.get('audio_available')) and frames > 0 and state not in CAPTURE_ACTIVE
        message = '原始录音独立保存，识别状态单独显示。'
        if recording_id in self._deferred:
            message = '录音继续保存，识别等待前段任务释放资源。'
        elif raw.get('asr_error'):
            message = '识别未完成；已保存音频保留，可稍后重新转写。'
        if transcript_error:
            asr_state = 'failed'
        value = {**old, 'state': state, 'capture_state': state, 'saved_frames': frames,
                 'saved_seconds': frames/16000, 'last_saved_at': raw.get('last_saved_at'),
                 'started_at': raw.get('started_at'), 'stopped_at': raw.get('stopped_at'),
                 'first_received_at': raw.get('first_received_at'),
                 'audio_available': available, 'error': error, 'level': max(0, min(100, float(raw.get('level') or 0))),
                 'capture_diagnostics': {'counters_exact_available': raw.get('capture_counters_exact_available'),
                    'received_device_frames': raw.get('received_device_frames'),
                    'unconfirmed_tail_frames_exact': raw.get('unconfirmed_tail_frames_exact'),
                    'unconfirmed_tail_upper_bound_frames': raw.get('unconfirmed_tail_upper_bound_frames'),
                    'tail_bound_basis': raw.get('tail_bound_basis'),
                    'tail_bound_assumptions': raw.get('tail_bound_assumptions'),
                    'unsaved_output_frames': raw.get('unsaved_output_frames'),
                    'unsaved_output_frames_upper': raw.get('unsaved_output_frames_upper')},
                 'asr': {'state': asr_state, 'error': transcript_error or raw.get('asr_error') or None, 'message': message,
                         'transcribed_frames': transcribed, 'transcribed_seconds': transcribed/16000,
                         'current_run_transcribed_frames': transcribed, 'current_run_transcribed_seconds': transcribed/16000,
                         'checkpoint_run_id': checkpoint_run,
                         'retry_queued': recording_id in self._deferred,
                         'historical_transcribed_frames': raw.get('historical_transcribed_frames', transcribed)},
                 'segments': segments, 'punctuated_text': validated_punctuation(raw, segments, asr_state, transcribed, frames),
                 'model_info': raw.get('model_info'), 'offline_policy': raw.get('offline_policy'),
                 'worker_pids': {'capture': raw.get('capture_worker_pid'), 'asr': raw.get('asr_worker_pid')},
                 'transcript_is_from_previous_run': raw.get('transcript_is_from_previous_run', False),
                 'data_error': raw.get('callback_error') or revision_error}
        return value

    def refresh(self, recording_id):
        with self._state_lock:
            return self._refresh(recording_id)

    def _try_park_asr(self, recording_id, value):
        """Park a finished ASR worker so the next recording reuses the model.

        State-driven: the completed transcript state (persisted and already
        read here) is the parking trigger. The worker's own asr_idle stdout
        event is unreliable on Windows pipes, so we never depend on it."""
        if value.get('asr', {}).get('state') != 'completed':
            return
        worker = self.supervisors.get(recording_id)
        if worker is None:
            return
        # test supervisors may not expose the process handle; treat as parked
        process = getattr(worker, 'asr_process', None)
        if process is None:
            return
        if getattr(process, '_asr_parked', False):
            return
        from shared_recording_core.supervisor import park_asr_process
        task = worker._tasks.get('asr')
        if task is not None and not task.done():
            # retire the old watcher BEFORE offering the process for reuse:
            # two concurrent stdout readers race for the next session's events
            # and the adopting watcher starves (attach then times out).
            task.get_loop().call_soon_threadsafe(task.cancel)
            deadline = time.monotonic() + 3
            while not task.done() and time.monotonic() < deadline:
                time.sleep(0.02)
        if park_asr_process(process, worker.asr_home):
            worker.asr_process = None
            self.supervisors.pop(recording_id, None)

    def _refresh(self, recording_id):
        cached = self.snapshots.get(recording_id)
        if cached and self._case_deleted(cached['case_id']):
            return None
        worker = self.supervisors.get(recording_id)
        if worker is None:
            return self.snapshots.get(recording_id)
        value = self._compose(recording_id, worker.snapshot())
        if not self._cache_if_live(value):
            return None
        important = {key: item for key, item in value.items() if key not in ('level',)}
        fingerprint = hashlib.sha256(json.dumps(important, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if self._hashes.get(recording_id) != fingerprint:
            try:
                self.repository.save(value)
                self._hashes[recording_id] = fingerprint
            except (OSError, sqlite3.DatabaseError):
                value['data_error'] = '本机业务状态保存异常；请保留录音目录及文件清单进行恢复。'
        if self._case_deleted(value['case_id']):
            return None
        self._try_park_asr(recording_id, value)
        return value

    def _case_deleted(self, case_id):
        with self._deletion_lock:
            return case_id in self._deleted_cases

    def mark_case_deleted(self, case_id):
        """Publish only an ID; safe under store.lock, never takes _state_lock."""
        with self._deletion_lock:
            self._deleted_cases.add(case_id)

    def _cache_if_live(self, value):
        # Stop cannot wait for the state lock (a refresh may be waiting for a
        # failed business DB). This short lock makes publication atomic with
        # tombstones without coupling stop to SQLite or the refresh state lock.
        with self._deletion_lock:
            if value['case_id'] in self._deleted_cases:
                return False
            self.snapshots[value['id']] = value
            return True

    def _reject_deleted(self, case_id):
        if self._case_deleted(case_id):
            raise HTTPException(404, '本案已经删除，录音及历史正文不可访问。')

    def _check_persistent_tombstone(self, case_id):
        self._reject_deleted(case_id)
        with self.store.lock, self.store.connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s7_deleted_cases'").fetchone()
            found = exists and connection.execute('SELECT 1 FROM s7_deleted_cases WHERE case_id=?', (case_id,)).fetchone()
        if found:
            self.mark_case_deleted(case_id)
            raise HTTPException(404, '本案已经删除，录音及历史正文不可访问。')

    def forget_deleted_recordings(self, case_id):
        """Call after releasing store.lock. Old monitor results cannot refill it."""
        self.mark_case_deleted(case_id)
        with self._state_lock:
            ids=[rid for rid,value in self.snapshots.items() if value['case_id']==case_id]
            for rid in ids:
                for cache in (self.snapshots,self.configs,self._segments,self._hashes,self._deferred):
                    cache.pop(rid,None)
                self._stop_requested.discard(rid)
                worker=self.supervisors.pop(rid,None)
                if worker is not None: self._retired_supervisors.append(worker)
                task=self.monitors.pop(rid,None)
                if task is not None and not task.done():
                    task.get_loop().call_soon_threadsafe(task.cancel)

    def active(self):
        for recording_id in list(self.supervisors):
            value = self.snapshots.get(recording_id)
            if value is None:
                continue
            if not self._case_deleted(value['case_id']) and value['state'] in CAPTURE_ACTIVE:
                return value
        return None

    def asr_busy_count(self):
        count=0
        for recording_id in list(self.supervisors):
            value=self.snapshots.get(recording_id)
            if (value is not None and recording_id not in self._deferred
                    and not self._case_deleted(value['case_id']) and value['asr']['state'] in ASR_ACTIVE):
                count+=1
        return count

    @asynccontextmanager
    async def maintenance_guard(self):
        # Same order as capture startup. Never acquire these under store.lock.
        async with self._capture_lock:
            async with self._asr_lock:
                if self.active() is not None or self.asr_busy_count():
                    raise HTTPException(409, '请先停止录音并等待转写完成，再创建备份。')
                self._maintenance_held = True
                try:
                    yield
                finally:
                    self._maintenance_held = False

    async def start(self, case_id, interview_id, device_id, duration_seconds=None, variant='mandarin'):
        if getattr(self, '_maintenance_held', False):
            raise HTTPException(409, '正在创建本地备份，请完成后再开始录音。')
        if variant not in ('mandarin', 'sichuan'):
            raise HTTPException(422, '识别语言仅支持普通话（mandarin）或四川话（sichuan）。')
        if variant == 'sichuan':
            # fail fast with a clear message instead of an ASR error mid-recording
            import json as _json
            from pathlib import Path as _Path
            home = _Path(self.settings.asr_home)
            try:
                cfg = _json.loads((home / 'dialect-models.json').read_text(encoding='utf-8-sig'))
                model = _Path(cfg['asr_model'])
                model = model if model.is_absolute() else home / model
                installed = (model / 'model.pt').is_file() and (model / 'config.yaml').is_file()
            except (OSError, ValueError, KeyError):
                installed = False
            if not installed:
                raise HTTPException(409, '四川话识别模型尚未安装到本机识别目录，请先安装后再选择四川话。')
        self._reject_deleted(case_id)
        from shared_recording_core import CaptureFormat, CaptureLease, SessionConfig
        if self.store.interview(case_id, interview_id) is None:
            raise HTTPException(404, '本案不存在该访谈。')
        async with self._capture_lock:
            self._reject_deleted(case_id)
            if self._closing:
                raise HTTPException(503, '应用正在停止，不能开始新的录音。')
            if self.active() is not None:
                raise HTTPException(409, '本工作台已有录音正在进行，请先停止当前录音。')
            occupied = (self.lease_probe() if self.lease_probe is not None else
                        CaptureLease(APP_ID, self.settings.instance_id, 'inspection').inspect())
            if occupied is not None:
                raise HTTPException(409, '其他已接入应用正在录音，请先结束其录音。')
            # Both immutable chunks and the combined WAV consume storage. For
            # open-ended capture this is a 30-minute initial headroom check,
            # rather than a promise that the remaining disk cannot run out.
            required_bytes = max(256 * 1024**2, math.ceil((duration_seconds or 1800) * 64000) + 64 * 1024**2)
            try:
                free_bytes = shutil.disk_usage(self.store.root).free
            except OSError as exc:
                raise HTTPException(409, '无法确认本机录音目录的可用空间，请检查存储后重试。') from exc
            if free_bytes < required_bytes:
                raise HTTPException(409, f'录音目录可用空间不足，本次开始前至少需要 {math.ceil(required_bytes/1024**2)} MB。请先释放空间。')
            if device_id not in [item['id'] for item in (await self.devices())['devices']]:
                raise HTTPException(422, '请选择当前可用的麦克风。')
            recording_id = 'rec_' + uuid.uuid4().hex
            config = SessionConfig(app_id=APP_ID, instance_id=self.settings.instance_id, session_id=recording_id,
                output_root=(self.store.root/'recordings').resolve(), device_id=device_id,
                capture_format=CaptureFormat(16000, 1, 2),
                asr_config={'engine': 'local-funasr', 'home': str(self.settings.asr_home), 'punctuate': True,
                            'cpu_threads': 2, 'variant': variant})
            snapshot = {'id': recording_id, 'case_id': case_id, 'interview_id': interview_id,
                'device_id': device_id, 'purpose': 'authorized_device_test', 'created_at': now(),
                'variant': variant,
                'state': 'starting', 'capture_state': 'starting', 'saved_frames': 0, 'saved_seconds': 0,
                'sample_rate': 16000, 'started_at': None, 'stopped_at': None, 'last_saved_at': None,
                'audio_available': False, 'error': None, 'asr': {'state': 'loading', 'message': '正在准备本地识别。', 'error': None},
                'segments': [], 'timing': 'approximate', 'level': 0}
            self.repository.create(config.to_dict(), snapshot)
            self.snapshots[recording_id], self.configs[recording_id] = snapshot, config
            worker = self._new_supervisor(recording_id)
            self.supervisors[recording_id] = worker
            try:
                async with self._asr_lock:
                    if self.asr_busy_count() > 2:
                        from dataclasses import replace
                        self._deferred[recording_id] = config
                        run_config = replace(config, asr_config={'engine': 'disabled'})
                    else:
                        run_config = config
                    await worker.start(run_config, duration_seconds=duration_seconds)
                self.monitors[recording_id] = asyncio.create_task(self._monitor(recording_id))
                return await asyncio.to_thread(self.refresh, recording_id)
            except Exception as exc:
                snapshot.update(state='failed', capture_state='failed', error='无法启动本机录音，请检查设备与运行环境。')
                snapshot['asr']['state'] = 'failed'
                self.repository.save(snapshot)
                try:
                    await worker.shutdown(asr_timeout=1)
                except Exception:
                    pass
                raise HTTPException(409, snapshot['error']) from exc

    async def _monitor(self, recording_id):
        while not self._closing:
            value = await asyncio.to_thread(self.refresh, recording_id)
            if value is None or self._case_deleted(value['case_id']): return
            if recording_id in self._deferred:
                async with self._asr_lock:
                    if not self._closing and self.asr_busy_count() < 2:
                        created = recording_id not in self.supervisors
                        try:
                            worker = self.supervisors.get(recording_id)
                            if worker is None:
                                worker = self._new_supervisor(recording_id)
                                self.supervisors[recording_id] = worker
                            await worker.start_asr(self._deferred[recording_id])
                            self._deferred.pop(recording_id, None)
                        except Exception:
                            self._deferred.pop(recording_id, None)
                            if created:
                                self.supervisors.pop(recording_id, None)
                            failed = {**value, 'asr': {**value['asr'], 'state': 'failed', 'retry_queued': False,
                                'error': '转写未能启动；已保存录音保留，请检查音频清单与本地识别环境。'}}
                            self.snapshots[recording_id] = failed
                            try:
                                await asyncio.to_thread(self.repository.save, failed)
                            except (OSError, sqlite3.DatabaseError):
                                failed['data_error'] = '转写失败状态尚未写入业务库，请保留录音目录。'
            if value['state'] not in CAPTURE_ACTIVE and value['asr']['state'] not in ASR_ACTIVE and recording_id not in self._deferred:
                break
            await asyncio.sleep(.5)
        if not self._closing:
            await asyncio.to_thread(self.refresh, recording_id)

    def get(self, case_id, interview_id, recording_id):
        self._reject_deleted(case_id)
        row = self.repository.get(case_id, interview_id, recording_id)
        value = row['snapshot']
        if recording_id in self._deferred:
            value = self.snapshots.get(recording_id, value)
        if recording_id in self.supervisors:
            value = self.refresh(recording_id)
            self._reject_deleted(case_id)
            return value
        if value.get('audio_available'):
            config = self._archive_config(row['context'], recording_id)
            if not config.resolve_output('recording.wav').is_file():
                value = {**value, 'audio_available': False, 'error': '录音文件当前不可用，请保留清单或从备份恢复。'}
        self._reject_deleted(case_id)
        return value

    def _archive_config(self, context, recording_id):
        try:
            return bound_archive_config(self.store, context, recording_id)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise HTTPException(409, '录音归档位置或身份无法验证，请保留本案目录和备份。') from exc

    def list(self, case_id, interview_id):
        self._reject_deleted(case_id)
        if self.store.interview(case_id, interview_id) is None:
            raise HTTPException(404, '本案不存在该访谈。')
        values = [self.get(case_id, interview_id, item['id']) for item in self.repository.list(case_id, interview_id)]
        active = next((item['id'] for item in values if item['state'] in CAPTURE_ACTIVE), None)
        return {'recordings': values, 'active_recording_id': active}

    async def stop(self, case_id, interview_id, recording_id):
        # Confirm ownership before touching a worker, then stop independently of
        # transcript/DB update success so storage failures cannot hide stop.
        self._reject_deleted(case_id)
        cached = self.snapshots.get(recording_id)
        if cached and cached['case_id'] == case_id and cached['interview_id'] == interview_id:
            if cached['state'] not in CAPTURE_ACTIVE:
                # Idle replay has no emergency-capture reason to bypass durable
                # ownership. Active stop remains independent of DB availability.
                await asyncio.to_thread(self._check_persistent_tombstone, case_id)
            row = {'snapshot': cached}
        else:
            row = self.repository.get(case_id, interview_id, recording_id)
        worker = self.supervisors.get(recording_id)
        value = self.snapshots.get(recording_id, row['snapshot'])
        if worker is not None:
            self._stop_requested.add(recording_id)
            raw = await worker.stop()
            self._reject_deleted(case_id)
            # No SQLite writes or transcript revision work on the stop response
            # path. The independent monitor persists the next authoritative state.
            source_state = raw.get('capture_state', 'stopping') if isinstance(raw, dict) else 'stopping'
            state = {'stopped': 'saved', 'completed': 'saved', 'failed': 'failed',
                     'interrupted': 'interrupted'}.get(source_state, 'stopping')
            frames = int(raw.get('saved_frames', value['saved_frames'])) if isinstance(raw, dict) else value['saved_frames']
            value = {**value, 'state': state, 'capture_state': state, 'saved_frames': frames,
                     'saved_seconds': frames/16000,
                     'audio_available': bool(isinstance(raw, dict) and raw.get('audio_available') and frames > 0 and state not in CAPTURE_ACTIVE)}
            if isinstance(raw, dict):
                value.update(stopped_at=raw.get('stopped_at'), last_saved_at=raw.get('last_saved_at'))
                value['asr'] = {**value['asr'], 'state': raw.get('asr_state') or value['asr']['state'],
                                'error': raw.get('asr_error') or None}
            if not self._cache_if_live(value):
                raise HTTPException(404, '本案已经删除，录音及历史正文不可访问。')
        self._reject_deleted(case_id)
        return value

    # ---------- imported recordings ----------

    MAX_IMPORT_BYTES = 300 * 1024 * 1024

    @staticmethod
    def _normalise_wav(payload):
        """Validate a WAV file and convert it to 16 kHz mono PCM16.

        Phone/recorded meeting WAVs come in many rates and channel counts; the
        archive and ASR pipeline are fixed at 16k mono. Pure-Python resampling
        (linear interpolation, channel averaging) — a one-off import cost."""
        import io
        import wave
        try:
            with wave.open(io.BytesIO(payload)) as reader:
                channels, rate, width = reader.getnchannels(), reader.getframerate(), reader.getsampwidth()
                if width != 2:
                    raise HTTPException(422, '仅支持 16 位 PCM WAV；请先转换为未压缩 WAV。')
                if channels < 1 or channels > 2 or rate < 8000 or rate > 96000:
                    raise HTTPException(422, '不支持的声道数或采样率。')
                frames = reader.readframes(reader.getnframes())
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, '无法读取 WAV 文件；仅支持未压缩 PCM WAV。') from None
        samples = struct.unpack(f'<{len(frames)//2}h', frames)
        if channels == 2:
            samples = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)]
        if rate == 16000:
            return samples
        step = rate / 16000.0
        count = int(len(samples) / step)
        out = []
        pos = 0.0
        for _ in range(count):
            base = int(pos)
            frac = pos - base
            a = samples[base]
            b = samples[base + 1] if base + 1 < len(samples) else a
            out.append(int(a + (b - a) * frac))
            pos += step
        return out

    async def import_wav(self, case_id, interview_id, payload, filename):
        """Import an external WAV as a finished recording of this interview.

        Writes a real archive (playback + manifest verify), marks ASR as
        failed-with-no-error so the normal retry path starts transcription."""
        if len(payload) > self.MAX_IMPORT_BYTES:
            raise HTTPException(422, '导入文件超过 300MB 限制，请截取访谈片段后导入。')
        if self.active() is not None:
            raise HTTPException(409, '正在现场录音，请先停止再导入。')
        with self.store.lock, self.store.connect() as connection:
            interview = self.store.interview(case_id, interview_id)
            if interview is None:
                raise HTTPException(404, '本案不存在该访谈。')
        samples = await asyncio.to_thread(self._normalise_wav, payload)
        if len(samples) < 1600:
            raise HTTPException(422, '音频过短（少于 0.1 秒），未导入。')
        recording_id = 'rec_' + uuid.uuid4().hex
        stamp = now()

        def build_archive():
            from shared_recording_core import SessionConfig
            from shared_recording_core.archive import ArchiveWriter
            config = SessionConfig(app_id=APP_ID, instance_id=getattr(self.settings, 'instance_id', 'import'),
                                   session_id=recording_id, output_root=(self.store.root / 'recordings').resolve(),
                                   device_id=0, capture_format={'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
                                   asr_config={'engine': 'imported'})
            writer = ArchiveWriter(config, 'imported-recording', source='import')
            writer.started()
            blob = b''
            cursor = 0
            total = len(samples)
            while cursor < total:
                chunk = samples[cursor:cursor + 16000]
                blob = struct.pack(f'<{len(chunk)}h', *chunk)
                writer.commit(blob)
                cursor += len(chunk)
            writer.close()
            return config

        config = await asyncio.to_thread(build_archive)
        total_frames = len(samples)
        snapshot = {'id': recording_id, 'case_id': case_id, 'interview_id': interview_id, 'device_id': 0,
                    'purpose': 'imported-interview', 'imported_from': (filename or '')[:120],
                    'created_at': stamp, 'state': 'saved', 'capture_state': 'saved',
                    'saved_frames': total_frames, 'saved_seconds': round(total_frames / 16000, 2),
                    'sample_rate': 16000, 'started_at': stamp, 'stopped_at': stamp, 'last_saved_at': stamp,
                    'audio_available': True, 'error': None, 'data_error': '',
                    'asr': {'state': 'failed', 'error': None,
                            'message': '导入完成；点击“重新转写本段”或等待自动转写。', 'transcribed_frames': 0},
                    'segments': []}
        context = {'app_id': APP_ID, 'instance_id': config.instance_id, 'session_id': recording_id,
                   'output_root': str((self.store.root / 'recordings').resolve()), 'device_id': 0,
                   'capture_format': {'sample_rate': 16000, 'channels': 1, 'sample_width': 2},
                   'asr_config': {'engine': 'imported'}}
        with self.store.lock, self.store.connect() as connection:
            connection.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)',
                               (recording_id, case_id, interview_id, APP_ID, stamp, stamp,
                                json.dumps(context, ensure_ascii=False),
                                json.dumps(snapshot, ensure_ascii=False)))
            connection.execute("UPDATE interviews SET capture_state='saved',asr_state='failed' "
                               'WHERE id=? AND case_id=?', (interview_id, case_id))
        return {'id': recording_id, 'saved_seconds': snapshot['saved_seconds'],
                'imported_from': snapshot['imported_from']}

    async def retry_transcription(self, case_id, interview_id, recording_id):
        if getattr(self, '_maintenance_held', False):
            raise HTTPException(409, '正在创建本地备份，请完成后再重试转写。')
        self._reject_deleted(case_id)
        row = await asyncio.to_thread(self.repository.get, case_id, interview_id, recording_id)
        value = await asyncio.to_thread(self.get, case_id, interview_id, recording_id)
        if value['saved_frames'] <= 0:
            raise HTTPException(409, '还没有确认保存的音频，暂不能重新转写。')
        async with self._asr_lock:
            self._reject_deleted(case_id)
            if self._closing:
                raise HTTPException(503, '应用正在停止，请重开后重试。')
            if recording_id in self._deferred:
                return self.snapshots[recording_id]
            value = self.snapshots.get(recording_id, value)
            if value['asr']['state'] not in {'failed', 'disabled'}:
                raise HTTPException(409, '本段识别正在运行或已经完成，无需重复开始。')
            config = self._archive_config(row['context'], recording_id)
            self.configs[recording_id] = config
            self.snapshots.setdefault(recording_id, value)
            worker = self.supervisors.get(recording_id)
            if self.asr_busy_count() >= 2:
                self._deferred[recording_id] = config
                self.snapshots[recording_id] = {**value, 'asr': {**value['asr'], 'state': 'not_started',
                    'retry_queued': True, 'message': '录音保留，识别等待前段任务释放资源。'}}
            else:
                created = worker is None
                try:
                    if worker is None:
                        worker = self._new_supervisor(recording_id)
                        self.supervisors[recording_id] = worker
                    await worker.start_asr(config)
                except Exception as exc:
                    if created:
                        self.supervisors.pop(recording_id, None)
                        self.snapshots[recording_id] = value
                    raise HTTPException(409, '本次转写未能启动；已保存录音保留，请检查本地识别环境。') from exc
            task = self.monitors.get(recording_id)
            if task is None or task.done():
                self.monitors[recording_id] = asyncio.create_task(self._monitor(recording_id))
            return await asyncio.to_thread(self.refresh, recording_id)

    def audio(self, case_id, interview_id, recording_id):
        self._reject_deleted(case_id)
        from shared_recording_core.archive import read_manifest
        row = self.repository.get(case_id, interview_id, recording_id)
        value = self.get(case_id, interview_id, recording_id)
        if value['state'] in CAPTURE_ACTIVE or not value['audio_available']:
            raise HTTPException(409, '录音尚未完成保存或没有可回听音频。')
        config = self._archive_config(row['context'], recording_id)
        try:
            manifest = read_manifest(config, verify=True)
            path = config.resolve_output('recording.wav')
            if not path.is_file() or manifest['saved_frames'] <= 0:
                raise ValueError('no audio')
        except Exception as exc:
            raise HTTPException(409, '录音文件或清单无法验证，请保留原目录进行恢复。') from exc
        self._reject_deleted(case_id)
        return path

    async def recover_interrupted(self):
        from shared_recording_core.archive import recover_archive
        for row in self.repository.all():
            value = row['snapshot']
            if value['state'] not in CAPTURE_ACTIVE and value['asr']['state'] not in ASR_ACTIVE:
                continue
            try:
                config = bound_archive_config(self.store, row['context'], value['id'])
                manifest = await asyncio.to_thread(recover_archive, config)
                capture_state = manifest['capture_state']
                raw = {**manifest, 'capture_state': capture_state, 'audio_available': manifest['saved_frames'] > 0,
                       'asr_state': value['asr']['state'], 'transcribed_frames': value['asr'].get('transcribed_frames', 0),
                       'segments': value.get('segments', [])}
                checkpoint_path = config.resolve_output('transcript.json')
                if checkpoint_path.is_file():
                    try:
                        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
                        if checkpoint.get('owner') != config.owner:
                            raise ValueError('transcript owner does not match')
                        processed = checkpoint.get('current_run_transcribed_frames', checkpoint.get('transcribed_frames', 0))
                        if type(processed) is not int or not 0 <= processed <= manifest['saved_frames']:
                            raise ValueError('transcript range is outside confirmed audio')
                        raw.update(asr_state=checkpoint.get('asr_state', 'failed'),
                                   transcribed_frames=processed, current_run_transcribed_frames=processed,
                                   historical_transcribed_frames=checkpoint.get('transcribed_frames', processed),
                                   segments=checkpoint.get('segments', []), asr_error=checkpoint.get('asr_error', ''),
                                   checkpoint_run_id=checkpoint.get('active_run_id'),
                                   model_info=checkpoint.get('model_info'), offline_policy=checkpoint.get('offline_policy'))
                        if (checkpoint.get('asr_state') == 'completed' and
                                checkpoint.get('final_text_produced_run_id') == checkpoint.get('active_run_id')):
                            raw['final_text'] = checkpoint.get('final_text')
                    except (OSError, ValueError, TypeError, AttributeError):
                        # A damaged text checkpoint cannot revoke independently
                        # verified audio or prevent recovery of the saved range.
                        raw.update(asr_state='failed', asr_error='转写清单无法验证；已保存录音保留，请保留原目录。')
                self.snapshots[value['id']] = value
                value = await asyncio.to_thread(self._compose, value['id'], raw)
            except Exception:
                value.update(state='failed', capture_state='failed', audio_available=False,
                             error='上次录音中断，文件清单尚未验证；请保留录音目录。')
            if value['asr']['state'] in ASR_ACTIVE:
                value['asr'].update(state='failed', error='服务已重启，识别未完成；已有文字与录音保留。')
            self.snapshots[value['id']] = value
            self.repository.save(value)

    async def shutdown(self):
        self._closing = True
        self._deferred.clear()
        workers = [*self.supervisors.values(),*self._retired_supervisors]
        # Stop every capture first; an older ASR backlog must not postpone the
        # active microphone's stop request. Drain independent workers in parallel.
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
        # Let any already-dispatched ASR start finish acquiring a tracked process
        # before cancelling monitors. No new dispatch is permitted while closing.
        async with self._asr_lock:
            pass
        for task in self.monitors.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.monitors.values(), return_exceptions=True)
        await asyncio.gather(*(worker.shutdown(asr_timeout=10) for worker in workers), return_exceptions=True)
        for recording_id in list(self.supervisors):
            await asyncio.to_thread(self.refresh, recording_id)
