"""Isolated UI acceptance harness; generates synthetic PCM and never opens a mic.

Run from due-diligence-app with the engineering interpreter. This module is not
used by start.ps1, launch.ps1, or any production HTTP control.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT.parent / 'shared-recording-core'))

from due_app.main import create_app
from fastapi.responses import JSONResponse
from shared_recording_core.archive import ArchiveWriter, utc_now


class SyntheticSupervisor:
    def __init__(self, **_kwargs):
        self.config = self.writer = None
        self.task = None
        self.raw = {}

    async def list_devices(self):
        return [{'index': 1, 'name': '合成麦克风（验收夹具，不访问设备）', 'default': True}]

    async def start(self, config, **_kwargs):
        self.config = config
        self.writer = ArchiveWriter(config, 'synthetic-capture', source='synthetic')
        self.writer.started()
        self.writer.commit(b'\x01\x00' * 16000)
        self.writer.commit(b'\x01\x00' * 16000)
        self.raw = {**self.writer.manifest, 'capture_state': 'recording', 'asr_state': 'loading',
                    'transcribed_frames': 0, 'audio_available': True, 'level': 0,
                    'callback_error': '合成界面验收：业务状态保存失败；音频清单独立保留。',
                    'checkpoint_run_id': 'synthetic-current-run', 'transcript_is_from_previous_run': True,
                    'segments': [{'segment_id': 'synthetic-segment', 'start_frame': 0, 'end_frame': 32000,
                                  'text': '合成测试文字：两亩测试田块。', 'produced_run_id': 'synthetic-old-run',
                                  'verified_run_id': 'synthetic-old-run'}]}
        self.task = asyncio.create_task(self.complete_asr())
        return self.snapshot()

    async def complete_asr(self):
        await asyncio.sleep(8)
        self.raw.update(asr_state='completed', transcribed_frames=32000, current_run_transcribed_frames=32000,
                        transcript_is_from_previous_run=False, callback_error=None)
        self.raw['segments'][0]['verified_run_id'] = 'synthetic-current-run'

    async def stop(self):
        if self.writer is not None and not self.writer._file.closed:
            self.writer.close()
        self.raw.update(capture_state='stopped', stopped_at=utc_now())
        return self.snapshot()

    async def shutdown(self, **_kwargs):
        await self.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return self.snapshot()

    def snapshot(self):
        return deepcopy(self.raw)


app = create_app(data_root=APP_ROOT / 'qa' / 's2-ui' / 'data',
                 recording_supervisor_factory=SyntheticSupervisor,
                 capture_lease_probe=lambda: None)


@app.middleware('http')
async def synthetic_lease(request, call_next):
    if request.url.path == '/api/due-diligence/capture/lease':
        return JSONResponse({'occupied': False, 'owner': None, 'scope': 'synthetic_no_device_access'})
    return await call_next(request)
