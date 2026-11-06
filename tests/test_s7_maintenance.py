"""Meaningful backup/recovery and archive-boundary checks with synthetic local data."""
import hashlib
import asyncio
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared-recording-core'))
from due_app.diligence import DiligenceError
from due_app.maintenance import MaintenanceService, restore_archive
from due_app.store import DueStore
from due_app.recordings import RecordingManager


class IdleRecordings:
    def active(self): return None
    def asr_busy_count(self): return 0


@pytest.fixture
def archive_env(tmp_path):
    store = DueStore(tmp_path / 'owned')
    item = store.create_case('合成恢复检查', 'farmer', 'farmer_demo')
    with store.connect() as c:
        c.execute('CREATE TABLE custom_versions (id INTEGER PRIMARY KEY,case_id TEXT,body TEXT)')
        c.execute('INSERT INTO custom_versions VALUES (1,?,?)', (item['id'], '合成原话历史版本'))
    audio = store.root / 'recordings' / 'synthetic' / 'chunk.bin'
    audio.parent.mkdir()
    audio.write_bytes(b'FAKE-TEST-AUDIO-NOT-A-MICROPHONE' * 20)
    (store.root / 'exports' / 'synthetic.txt').write_text('合成报告', encoding='utf-8')
    return store, MaintenanceService(store, IdleRecordings())


def test_full_backup_restores_database_versions_and_recording_bytes(archive_env, tmp_path):
    store, service = archive_env
    backup = service.create()
    result = service.verify(backup['id'])
    restored = Path(result['restored_directory'])
    assert result['state'] == 'verified' and result['active_workspace_changed'] is False
    assert result['table_counts']['custom_versions'] == 1
    assert (restored / 'recordings/synthetic/chunk.bin').read_bytes() == (store.root / 'recordings/synthetic/chunk.bin').read_bytes()
    assert (restored / 'exports/synthetic.txt').read_text(encoding='utf-8') == '合成报告'
    assert backup['encrypted'] is False
    assert service.list()['backups'][0]['id'] == backup['id']


@pytest.mark.parametrize('kind', ['capture', 'asr'])
def test_backup_refuses_active_capture_or_transcription(archive_env, kind):
    _, service = archive_env
    if kind == 'capture': service.recordings.active = lambda: {'id': 'synthetic-active'}
    else: service.recordings.asr_busy_count = lambda: 1
    with pytest.raises(DiligenceError, match='停止录音'):
        service.create()
    assert service.list()['backups'] == []


def test_recovery_never_overwrites_existing_target(archive_env, tmp_path):
    _, service = archive_env
    backup = service.create()
    target = tmp_path / 'restore-existing'
    target.mkdir()
    keep = target / 'keep.txt'
    keep.write_text('保留', encoding='utf-8')
    with pytest.raises(DiligenceError) as exc:
        restore_archive(service._path(backup['id']), target, tmp_path)
    assert exc.value.code == 'restore_target_not_empty'
    assert keep.read_text(encoding='utf-8') == '保留'


def rewrite_archive(source, output, change):
    with zipfile.ZipFile(source) as original:
        members = {i.filename: original.read(i.filename) for i in original.infolist()}
    change(members)
    with zipfile.ZipFile(output, 'w') as result:
        for name, content in members.items(): result.writestr(name, content)


def test_corrupt_audio_hash_fails_recovery_without_switching_workspace(archive_env, tmp_path):
    store, service = archive_env
    backup = service.create()
    tampered = tmp_path / 'bad.zip'
    rewrite_archive(service._path(backup['id']), tampered,
                    lambda m: m.__setitem__('recordings/synthetic/chunk.bin', b'X' * len(m['recordings/synthetic/chunk.bin'])))
    with pytest.raises(DiligenceError) as exc:
        restore_archive(tampered, tmp_path / 'isolated-bad', tmp_path)
    assert exc.value.code == 'backup_hash_mismatch'
    assert store.cases()[0]['title'] == '合成恢复检查'


def test_traversal_manifest_is_rejected_before_writing_files(archive_env, tmp_path):
    _, service = archive_env
    backup = service.create()
    bad = tmp_path / 'traversal.zip'
    def change(members):
        members['../escape.txt'] = b'no'
        m = json.loads(members['manifest.json'])
        m['files'].append({'path':'../escape.txt','bytes':2,'sha256':hashlib.sha256(b'no').hexdigest()})
        members['manifest.json'] = json.dumps(m).encode()
    rewrite_archive(service._path(backup['id']), bad, change)
    with pytest.raises(DiligenceError): restore_archive(bad, tmp_path / 'safe', tmp_path)
    assert not (tmp_path / 'escape.txt').exists() and not (tmp_path / 'safe').exists()


def test_restore_target_must_stay_inside_explicit_parent(archive_env, tmp_path):
    _, service = archive_env
    backup = service.create()
    with pytest.raises(DiligenceError) as exc:
        restore_archive(service._path(backup['id']), tmp_path.parent / 'outside-owned-test', tmp_path)
    assert exc.value.code == 'invalid_restore_target'


def test_backup_download_identifier_cannot_select_arbitrary_file(archive_env):
    _, service = archive_env
    for identifier in ('../due.sqlite3', '..', '/etc/passwd', 'backup_not_real'):
        with pytest.raises(DiligenceError): service._path(identifier)


def test_permission_denial_blocks_new_backup_old_download_and_verification(archive_env):
    store, service = archive_env
    backup = service.create()
    with store.connect() as c:
        c.execute('CREATE TABLE s3_bindings (case_id TEXT,access_state TEXT)')
        c.execute("INSERT INTO s3_bindings VALUES ('synthetic','denied')")
    for action in (service.create, lambda: service._path(backup['id']), lambda: service.verify(backup['id'])):
        with pytest.raises(DiligenceError) as exc: action()
        assert exc.value.code == 'backup_access_denied'


def test_backup_guard_serializes_capture_and_asr_start(archive_env):
    store, _ = archive_env
    class Guarded(IdleRecordings):
        maintenance_guard = RecordingManager.maintenance_guard
    async def scenario():
        recording = Guarded()
        recording._capture_lock, recording._asr_lock = asyncio.Lock(), asyncio.Lock()
        service = MaintenanceService(store, recording)
        entered = []
        async def start_capture():
            async with recording._capture_lock: entered.append('capture')
        async def start_asr():
            async with recording._asr_lock: entered.append('asr')
        with pytest.raises(DiligenceError): service.create()
        async with recording.maintenance_guard():
            capture = asyncio.create_task(start_capture())
            asr = asyncio.create_task(start_asr())
            await asyncio.sleep(0)
            assert entered == []
            backup = await asyncio.to_thread(service.create)
            assert backup['id'] and entered == []
        await asyncio.gather(capture, asr)
        assert sorted(entered) == ['asr','capture']
        assert recording._maintenance_held is False
    asyncio.run(scenario())


def test_backup_parent_link_is_rejected(archive_env, tmp_path):
    store, _ = archive_env
    outside = tmp_path / 'outside'
    outside.mkdir()
    linked = store.root / 'exports' / 'linked'
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        # This system permits junction creation without changing OS privileges.
        import subprocess
        subprocess.run(['cmd','/c','mklink','/J',str(linked),str(outside)], check=True, capture_output=True)
    try:
        service = MaintenanceService(store, IdleRecordings())
        with pytest.raises(DiligenceError) as exc: service.create()
        assert exc.value.code in ('unsafe_data_path','backup_unsafe_link')
        assert list(outside.iterdir()) == []
    finally:
        # Remove only our known link itself, never its target tree.
        if linked.is_symlink(): linked.unlink()
        elif linked.exists(): linked.rmdir()


def test_null_case_identity_fails_database_inspection(tmp_path):
    import sqlite3
    from due_app.maintenance import inspect_database
    path = tmp_path / 'malformed.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE app_meta(key TEXT,value TEXT)')
        connection.execute("INSERT INTO app_meta VALUES ('app_id','due-diligence')")
        connection.execute('CREATE TABLE cases(app_id TEXT,data_class TEXT)')
        connection.execute("INSERT INTO cases VALUES (NULL,'synthetic')")
    with pytest.raises(DiligenceError) as exc: inspect_database(path)
    assert exc.value.code == 'invalid_backup_database'


def test_zip_symbolic_link_member_is_rejected(archive_env, tmp_path):
    _, service = archive_env
    backup = service.create()
    path = service._path(backup['id'])
    with zipfile.ZipFile(path) as source:
        members = {i.filename:source.read(i.filename) for i in source.infolist()}
    bad = tmp_path / 'symlink.zip'
    with zipfile.ZipFile(bad,'w') as archive:
        for name, body in members.items():
            info = zipfile.ZipInfo(name)
            if name == 'recordings/synthetic/chunk.bin':
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
            archive.writestr(info, body)
    with pytest.raises(DiligenceError) as exc: restore_archive(bad,tmp_path/'reject-symlink',tmp_path)
    assert exc.value.code == 'invalid_backup'


def test_capture_and_retry_fail_fast_during_backup_without_touching_devices():
    from fastapi import HTTPException
    manager = RecordingManager.__new__(RecordingManager)
    manager._maintenance_held = True
    async def scenario():
        for operation in (manager.start('case','interview',1),manager.retry_transcription('case','interview','recording')):
            with pytest.raises(HTTPException) as exc: await operation
            assert exc.value.status_code == 409
    asyncio.run(scenario())


@pytest.fixture
def portable_archive(tmp_path):
    """A real archive format filled with generated PCM; no capture process."""
    from types import SimpleNamespace
    from shared_recording_core import SessionConfig, CaptureFormat
    from shared_recording_core.archive import ArchiveWriter
    from due_app.diligence import DiligenceService
    from due_app.recordings import RecordingRepository
    from due_app.store import now
    store = DueStore(tmp_path / 'original')
    item = store.create_case('合成便携恢复案例', 'farmer', 'farmer_demo')
    interview = store.create_interview(item['id'], '仅生成PCM，不使用麦克风')
    config = SessionConfig('due-diligence', 'source-instance', 'rec_portable',
        store.root / 'recordings', 0, CaptureFormat(16000,1,2), {'engine':'disabled'})
    writer = ArchiveWriter(config, 'synthetic-run', source='synthetic-generated-pcm')
    writer.started()
    writer.commit(b'\x01\x00' * 16000)
    writer.close()
    segment = {'id':'segment_portable', 'revision':1, 'text':'合成原话：家庭四人。',
        'start_seconds':0, 'end_seconds':1, 'stable':True, 'source_run_id':'synthetic-run'}
    snapshot = {'id':'rec_portable', 'case_id':item['id'], 'interview_id':interview['id'],
        'created_at':now(), 'state':'saved', 'capture_state':'saved', 'saved_frames':16000,
        'saved_seconds':1, 'audio_available':True, 'data_error':None, 'segments':[segment],
        'asr':{'state':'disabled','transcribed_frames':16000}}
    repository = RecordingRepository(store)
    repository.create(config.to_dict(), snapshot)
    repository.save_segment('rec_portable', segment, 'synthetic-run')
    settings = SimpleNamespace(capture_python=None, asr_home=tmp_path)
    manager = RecordingManager(store, settings)
    diligence = DiligenceService(store, manager)
    diligence.load_previsit(item['id'], 'SYN-S0-001', 'base')
    request = {'recording_id':'rec_portable', 'interview_id':interview['id'],
        'segment_id':'segment_portable', 'segment_revision':1}
    source = diligence.statement_from_segment(item['id'], request)
    diligence.confirm_statement(item['id'], source['id'], 1)
    service = MaintenanceService(store, IdleRecordings())
    return SimpleNamespace(store=store, config=config, snapshot=snapshot, settings=settings,
        case_id=item['id'], interview_id=interview['id'], request=request, source=source, service=service)


def isolated_restore_without_original(env, tmp_path):
    backup = env.service.create()
    result = restore_archive(env.service._path(backup['id']), tmp_path / 'recovered', tmp_path)
    # Both paths are newly generated children of this test's dedicated directory.
    origin, held = env.store.root.resolve(), (tmp_path / 'source-held').resolve()
    assert origin.is_relative_to(tmp_path.resolve()) and held.is_relative_to(tmp_path.resolve())
    assert origin != tmp_path.resolve() and not held.exists()
    origin.rename(held)
    assert not origin.exists()
    return result


def test_restored_audio_and_confirmed_source_work_with_original_root_unavailable(portable_archive, tmp_path):
    from due_app.diligence import DiligenceService
    env = portable_archive
    result = isolated_restore_without_original(env, tmp_path)
    restored = Path(result['restored_directory'])
    store = DueStore(restored)
    manager = RecordingManager(store, env.settings)
    persisted = manager.repository.get(env.case_id, env.interview_id, 'rec_portable')
    assert persisted['context'] == env.config.to_dict()  # immutable original location/identity retained
    assert manager.get(env.case_id, env.interview_id, 'rec_portable')['audio_available'] is True
    audio = manager.audio(env.case_id, env.interview_id, 'rec_portable')
    assert audio == restored / 'recordings/rec_portable/recording.wav'
    assert audio.is_file() and not env.store.root.exists()
    diligence = DiligenceService(store, manager)
    source = diligence.statement(env.case_id, env.source['id'])
    assert source['needs_review'] is False and source['record_confirmation'] == 'record_accurate'
    assert source['source_ref'] == env.source['source_ref']
    assert source['text'] == env.source['text']
    assert diligence.statement_from_segment(env.case_id, env.request)['text'] == env.source['text']
    migration = result['database_migration']
    assert migration['performed'] is True and migration['source_contexts_rewritten'] is False
    assert migration['source_sha256'] != migration['restored_sha256']
    assert result['restored_table_counts']['app_meta'] == result['table_counts']['app_meta'] + 1


def test_restored_interrupted_archive_rebuilds_only_in_new_workspace(portable_archive, tmp_path):
    env = portable_archive
    changed = {**env.snapshot, 'state':'recording', 'capture_state':'recording',
        'asr':{'state':'loading','transcribed_frames':16000}}
    with env.store.connect() as connection:
        connection.execute('UPDATE recordings SET snapshot_json=? WHERE id=?', (json.dumps(changed), 'rec_portable'))
    result = isolated_restore_without_original(env, tmp_path)
    restored = Path(result['restored_directory'])
    store = DueStore(restored)
    manager = RecordingManager(store, env.settings)
    asyncio.run(manager.recover_interrupted())
    saved = manager.get(env.case_id, env.interview_id, 'rec_portable')
    assert saved['state'] == 'saved' and saved['saved_frames'] == 16000
    assert saved['audio_available'] is True and saved['asr']['state'] == 'failed'
    assert manager.audio(env.case_id, env.interview_id, 'rec_portable').is_relative_to(restored)
    assert not env.store.root.exists()


def test_relocation_metadata_cannot_authorize_a_different_destination(portable_archive, tmp_path):
    from due_app.archive_paths import bound_archive_config, RESTORED_ROOTS_KEY
    env = portable_archive
    with env.store.connect() as connection:
        connection.execute('INSERT INTO app_meta(key,value) VALUES (?,?)',
            (RESTORED_ROOTS_KEY, json.dumps({'schema':'due-archive-relocation/1',
                'target_root':str(tmp_path/'another/recordings'),
                'original_roots':[str(tmp_path/'foreign/recordings')]})))
    wrong = {**env.config.to_dict(), 'output_root':str(tmp_path/'foreign/recordings')}
    with pytest.raises(ValueError): bound_archive_config(env.store, wrong, 'rec_portable')


def test_rebacked_restored_workspace_can_relocate_again_without_originals(portable_archive, tmp_path):
    env = portable_archive
    first = isolated_restore_without_original(env, tmp_path)
    first_root = Path(first['restored_directory'])
    first_store = DueStore(first_root)
    service = MaintenanceService(first_store, IdleRecordings())
    backup = service.create()
    second = restore_archive(service._path(backup['id']), tmp_path/'recovered-again', tmp_path)
    held = (tmp_path/'first-restore-held').resolve()
    assert first_root.resolve().is_relative_to(tmp_path.resolve()) and held.is_relative_to(tmp_path.resolve())
    assert first_root.resolve() != tmp_path.resolve() and not held.exists()
    first_root.rename(held)
    second_root = Path(second['restored_directory'])
    manager = RecordingManager(DueStore(second_root), env.settings)
    assert manager.audio(env.case_id, env.interview_id, 'rec_portable').is_relative_to(second_root)
    assert not env.store.root.exists() and not first_root.exists()
    assert manager.repository.get(env.case_id, env.interview_id, 'rec_portable')['context'] == env.config.to_dict()
    assert second['table_counts']['app_meta'] == second['restored_table_counts']['app_meta']
