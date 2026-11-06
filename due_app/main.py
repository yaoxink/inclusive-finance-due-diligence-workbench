"""Independent S1 local service. Uses no meeting database, integrations or configuration."""
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from .config import APP_ID, APP_ROOT, WORKSPACE, VERSION, Settings
from .instance_lock import InstanceLock
from .store import DueStore

CORE_ROOT = WORKSPACE / 'shared-recording-core'
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    subject_type: Literal['farmer'] = 'farmer'
    product_id: Literal['farmer_demo'] = 'farmer_demo'

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('请填写不含控制字符的案件名称。')
        return value


class InterviewCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='现场访谈', min_length=1, max_length=160)

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        return CaseCreate.clean_title(value)


def _synthetic_customer_identity():
    """Random demo identity: structurally valid 18-digit ID (Qionglai 510183,
    correct checksum), local town/village address. Synthetic data only."""
    import random
    towns = ['固驿镇', '羊安镇', '牟礼镇', '桑园镇', '平乐镇', '火井镇', '临济镇']
    villages = ['东岳村', '楠杨村', '泉水村', '范店村', '青华村', '龙岗村']
    given = ['建国', '秀芳', '志强', '春花', '德福', '桂英', '文军', '玉华', '大明', '素芬', '树林', '淑芬']
    base = ('510183' + f"{random.randint(1962, 1976)}{random.randint(1, 12):02d}{random.randint(1, 28):02d}"
            + f"{random.randint(0, 999):03d}" + str(random.randint(0, 9)))
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    checksum = '10X98765432'[sum(int(c) * w for c, w in zip(base, weights)) % 11]
    address = f"四川省成都市邛崃市{random.choice(towns)}{random.choice(villages)}{random.randint(1, 20)}组"
    return {'customer_name': random.choice('李王张刘陈杨黄周吴徐') + random.choice(given),
            'id_type': '居民身份证', 'id_number': base + checksum,
            'phone': '1' + random.choice('35789') + f"{random.randint(0, 10**9 - 1):09d}",
            'home_address': address, 'contact_address': address}


class RecordingStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device_id: StrictInt = Field(ge=0, le=10000)
    duration_seconds: float | None = Field(default=None, gt=0, le=28800)
    variant: StrictStr = 'mandarin'  # mandarin (streaming) | sichuan (offline batches)


class RecordingRetry(BaseModel):
    model_config = ConfigDict(extra='forbid')


def capabilities():
    return {'capabilities': [
        {'id': 'cases', 'label': '案件与访谈', 'status': 'ready', 'reason': '独立合成案件、访谈上下文可创建和重开。'},
        {'id': 'capture', 'label': '可靠录音', 'status': 'under_verification', 'reason': '采集与识别已分离，目前仅供已授权设备测试，S2验收尚未完成。'},
        {'id': 'asr', 'label': '现场转写', 'status': 'under_verification', 'reason': '本地流式转写独立运行，现场延迟及语音质量尚待验收。'},
        {'id': 'previsit', 'label': '事前资料', 'status': 'under_verification', 'reason': '只读合成快照开发中，真实上游未接通；保留来源、版本与处理状态。'},
        {'id': 'questionnaire', 'label': '问卷', 'status': 'under_verification', 'reason': '农户候选题库正在合成联调，条件与接入范围仍需业务复核。'},
        {'id': 'facts', 'label': '事实确认', 'status': 'under_verification', 'reason': '原文、候选和记录准确确认分别保存，人工确认不自动升级事实核验。'},
        {'id': 'followup', 'label': '动态追问', 'status': 'under_verification', 'reason': '规则补问和按案可选本地AI已接入；建议有依据、可采纳或忽略，业务质量仍待复核。'},
        {'id': 'model', 'label': '评分与额度', 'status': 'under_verification', 'reason': '优化版辅助模型按明确输入检查评分、评级和三路额度；缺项或来源待核验时阻断，业务验收尚待完成。'},
        {'id': 'report', 'label': '报告', 'status': 'under_verification', 'reason': 'A4封面、带缺口初稿、人工版本和DOCX导出已接入，完整业务验收仍待完成。'}]}


def create_app(data_root=None, recording_supervisor_factory=None, capture_lease_probe=None):
    from .recordings import RecordingManager
    instance_id = uuid.uuid4().hex
    settings = Settings.create(data_root, instance_id)
    store = DueStore(settings.data_root)
    recordings = RecordingManager(store, settings, supervisor_factory=recording_supervisor_factory,
                                  capture_lease_probe=capture_lease_probe)

    @asynccontextmanager
    async def lifespan(app):
        ownership = InstanceLock(settings.data_root)
        ownership.acquire()
        pid_file = settings.data_root / 'server.json'
        cleanup_stop = asyncio.Event()
        cleanup_task = None

        async def cleanup_once():
            # Expiry must not compete with live capture or transcription. The
            # same guard also serializes manual deletion, restore and downloads.
            if recordings.active() or recordings.asr_busy_count():
                return
            try:
                async with recordings.maintenance_guard():
                    await asyncio.to_thread(app.state.retention.cleanup)
            except Exception as exc:
                # File/job failures are retained by RetentionService for retry;
                # never put a source document or exception body in the log.
                logging.getLogger(__name__).warning('Retention cleanup deferred (%s)',type(exc).__name__)

        async def cleanup_periodically():
            while not cleanup_stop.is_set():
                try:
                    await asyncio.wait_for(cleanup_stop.wait(),timeout=60)
                except asyncio.TimeoutError:
                    if not cleanup_stop.is_set():
                        await cleanup_once()
        try:
            try:
                # A second service must never recover files still being written
                # by the current owner, even if its PID metadata is stale.
                await recordings.recover_interrupted()
                await cleanup_once()
                cleanup_task = asyncio.create_task(cleanup_periodically())
                pid_file.write_text(json.dumps({'app': APP_ID, 'pid': os.getpid(), 'instance_id': instance_id}), encoding='utf-8')
                yield
            finally:
                cleanup_stop.set()
                if cleanup_task is not None:
                    # Do not abandon a filesystem worker while releasing the
                    # workspace lock to a replacement application instance.
                    await cleanup_task
                await recordings.shutdown()
        finally:
            try:
                # Do not remove a replacement instance's PID record.
                if pid_file.exists():
                    try:
                        if json.loads(pid_file.read_text(encoding='utf-8')).get('instance_id') == instance_id:
                            pid_file.unlink()
                    except (OSError, ValueError):
                        pass
            finally:
                ownership.release()

    app = FastAPI(title='普惠尽调工作台', version=VERSION, lifespan=lifespan)
    app.state.settings, app.state.store, app.state.recordings = settings, store, recordings
    # S3 development uses only the local synthetic snapshot adapter. Its router
    # owns source/fact revisions and cannot open capture devices or call models.
    from .diligence_routes import install_diligence_routes
    install_diligence_routes(app, store, recordings)
    from .followup_routes import install_followup_routes
    from .calculation_routes import install_calculation_routes
    from .report_routes import install_report_routes
    from .maintenance_routes import install_maintenance_routes
    from .retention_routes import install_retention_routes
    install_followup_routes(app, store, app.state.diligence)
    from .answer_routes import install_answer_routes
    install_answer_routes(app, store, app.state.diligence)
    from .answer_routes import install_extraction_routes
    install_extraction_routes(app, store, app.state.diligence)
    from .questionnaire_routes import install_questionnaire_routes
    install_questionnaire_routes(app, store, app.state.diligence)
    install_calculation_routes(app, store, app.state.diligence)
    from .farmer_model_v2_routes import install_farmer_v2_routes
    install_farmer_v2_routes(app, store, app.state.diligence, legacy=app.state.calculations)
    from .answer_routes import install_answer_bridge_routes
    install_answer_bridge_routes(app, store, app.state.answers, app.state.farmer_model_v2)
    from .transcript_repair import install_transcript_repair_routes
    install_transcript_repair_routes(app)
    install_report_routes(app, store, app.state.diligence, app.state.calculations)
    install_maintenance_routes(app, store, recordings)
    install_retention_routes(app, store, recordings, app.state.maintenance)

    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.headers.get('host', '')
        if host.split(':')[0] not in ('localhost', '127.0.0.1', 'testserver'):
            return Response('Local access only', status_code=403)
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != host:
            return Response('Cross-origin requests rejected', status_code=403)
        return await call_next(request)

    @app.get('/api/due-diligence/health')
    def health():
        active = recordings.active()
        return {'app': APP_ID, 'version': VERSION, 'pid': os.getpid(), 'instance_id': instance_id,
                'active_recording_id': active['id'] if active else None, 'asr_busy_count': recordings.asr_busy_count()}

    @app.get('/api/due-diligence/capabilities')
    def capability_list():
        return capabilities()

    @app.get('/api/due-diligence/runtime-policy')
    def policy():
        return settings.public_policy()

    @app.get('/api/due-diligence/cases')
    def case_list():
        return {'cases': store.cases()}

    @app.post('/api/due-diligence/cases', status_code=201)
    def create_case(payload: CaseCreate):
        case = store.create_case(payload.title, payload.subject_type, payload.product_id)
        # New cases start with a random synthetic identity so the prepare tab
        # is never empty; every value is fake by construction (synthetic app).
        try:
            app.state.answers.update_customer_info(case['id'], {
                'fields': _synthetic_customer_identity(), 'expected_revision': 0,
                'actor': 'case_setup'})
        except Exception:
            pass  # a case without pre-filled identity stays fully usable
        return case

    @app.get('/api/due-diligence/cases/{case_id}')
    def get_case(case_id: str):
        case = store.case(case_id)
        if case is None:
            raise HTTPException(404, '本尽调应用中不存在该案件。')
        for interview in case['interviews']:
            values = recordings.list(case_id, interview['id'])
            interview['recordings'], interview['active_recording_id'] = values['recordings'], values['active_recording_id']
        return case

    @app.post('/api/due-diligence/cases/{case_id}/interviews', status_code=201)
    def create_interview(case_id: str, payload: InterviewCreate):
        item = store.create_interview(case_id, payload.title)
        if item is None:
            raise HTTPException(404, '本尽调应用中不存在该案件。')
        return item

    @app.get('/api/due-diligence/cases/{case_id}/interviews/{interview_id}')
    def get_interview(case_id: str, interview_id: str):
        item = store.interview(case_id, interview_id)
        if item is None:
            raise HTTPException(404, '本案不存在该访谈。')
        values = recordings.list(case_id, interview_id)
        return {**item, **values}

    @app.get('/api/due-diligence/capture/devices')
    async def devices():
        return await recordings.devices()

    @app.get('/api/due-diligence/capture/active')
    def active_recording():
        return {'recording': recordings.active()}

    @app.get('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings')
    def recording_list(case_id: str, interview_id: str):
        return recordings.list(case_id, interview_id)

    @app.post('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/start', status_code=201)
    async def start_recording(case_id: str, interview_id: str, payload: RecordingStart):
        return await recordings.start(case_id, interview_id, payload.device_id, payload.duration_seconds,
                                      variant=payload.variant)

    @app.get('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/{recording_id}')
    def recording_detail(case_id: str, interview_id: str, recording_id: str):
        return recordings.get(case_id, interview_id, recording_id)

    @app.post('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/{recording_id}/stop')
    async def stop_recording(case_id: str, interview_id: str, recording_id: str):
        return await recordings.stop(case_id, interview_id, recording_id)

    @app.post('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/import', status_code=201)
    async def import_recording(case_id: str, interview_id: str, request: Request):
        payload = await request.body()
        filename = (request.headers.get('x-import-filename') or 'imported.wav')[:120]
        result = await recordings.import_wav(case_id, interview_id, payload, filename)
        return result

    @app.post('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/{recording_id}/transcription/retry')
    async def retry_transcription(case_id: str, interview_id: str, recording_id: str, payload: RecordingRetry):
        return await recordings.retry_transcription(case_id, interview_id, recording_id)

    @app.get('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/{recording_id}/audio')
    async def recording_audio(case_id: str, interview_id: str, recording_id: str):
        path = await asyncio.to_thread(recordings.audio, case_id, interview_id, recording_id)
        return FileResponse(path, media_type='audio/wav')

    @app.get('/api/due-diligence/cases/{case_id}/interviews/{interview_id}/recordings/{recording_id}/segments/history')
    def segment_history(case_id: str, interview_id: str, recording_id: str):
        recordings.get(case_id, interview_id, recording_id)
        return {'revisions': recordings.repository.history(recording_id)}

    @app.get('/api/due-diligence/capture/lease')
    def capture_lease():
        try:
            from shared_recording_core.lease import CaptureLease
            owner = CaptureLease(APP_ID, instance_id, 'inspection').inspect()
        except ImportError:
            raise HTTPException(503, '共享录音组件尚未准备完成。')
        if owner is None:
            return {'occupied': False, 'owner': None, 'scope': 'shared_apps_only'}
        public = {key: owner.get(key) for key in ('app_id', 'instance_id', 'session_id', 'worker_pid')}
        public['worker_pid'] = public['worker_pid'] or owner.get('pid')
        return {'occupied': True, 'owner': public, 'scope': 'shared_apps_only'}

    dist = APP_ROOT / 'frontend-dist'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/')
    def root_page():
        return RedirectResponse('/due-diligence')

    @app.get('/due-diligence')
    @app.get('/due-diligence/')
    def page():
        target = dist / 'due-diligence.html'
        if not target.is_file():
            raise HTTPException(503, '尽调页面尚未构建，请先构建专用前端。')
        return FileResponse(target)

    return app


app = create_app()
