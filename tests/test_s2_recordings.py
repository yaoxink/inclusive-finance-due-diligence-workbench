"""S2 HTTP adapter tests: synthetic archives and fake workers, no mic/model/network."""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = APP_ROOT.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(WORKSPACE / "shared-recording-core"))
from shared_recording_core.archive import ArchiveWriter, atomic_json, read_manifest, utc_now

API = "/api/due-diligence"


class FakeSupervisor:
    """Mirrors core state fields but writes known synthetic PCM only."""
    instances = []

    def __init__(self, python_path, asr_home, on_event=None):
        self.python_path, self.asr_home, self.on_event = python_path, asr_home, on_event
        self.config, self.writer = None, None
        self.stop_calls = self.shutdown_calls = self.start_asr_calls = 0
        self.raw = {"capture_state": "not_started", "saved_frames": 0, "capture_error": "",
                    "asr_state": "not_started", "transcribed_frames": 0, "asr_error": "", "segments": [],
                    "audio_available": False, "level": 0, "last_saved_at": None,
                    "started_at": None, "stopped_at": None, "capture_worker_pid": 111,
                    "asr_worker_pid": 222, "checkpoint_run_id": "fixture-asr-run"}
        type(self).instances.append(self)

    async def list_devices(self):
        return [{"index": 1, "name": "fixture microphone", "default": True, "hostapi": "fixture"}]

    async def start(self, config, **kwargs):
        assert set(kwargs) <= {"duration_seconds"}, "HTTP must not pass fixture or capture overrides"
        self.config = config
        self.writer = ArchiveWriter(config, "fixture-capture-run", source="synthetic")
        self.writer.started()
        self.writer.commit(b"\x01\x00" * 16000)
        self.writer.commit(b"\x02\x00" * 16000)
        self.raw.update(capture_state="recording", saved_frames=32000, audio_available=True,
                        asr_state="disabled" if config.asr_config.get("engine") == "disabled" else "loading",
                        started_at=self.writer.manifest["started_at"], last_saved_at=self.writer.manifest["last_saved_at"],
                        level=15)
        return self.snapshot()

    async def stop(self):
        self.stop_calls += 1
        if self.writer is not None and not self.writer._file.closed:
            self.writer.close()
        self.raw.update(capture_state="stopped", stopped_at=utc_now())
        return self.snapshot()

    async def shutdown(self, asr_timeout=30):
        self.shutdown_calls += 1
        await self.stop()
        if self.raw["asr_state"] in {"not_started", "loading", "running", "draining"}:
            self.raw.update(asr_state="failed", asr_error="synthetic shutdown interruption")
        return self.snapshot()

    async def start_asr(self, config):
        self.start_asr_calls += 1
        if self.config is None:
            self.config = config
            manifest = read_manifest(config, verify=True)
            self.raw.update(manifest, audio_available=manifest['saved_frames'] > 0)
        self.raw.update(asr_state="loading", asr_error="")
        return self.snapshot()

    def snapshot(self):
        return copy.deepcopy(self.raw)

    def segments(self, text="合成原话", revision=1):
        self.raw.update(asr_state="running", transcribed_frames=32000, segments=[{
            "segment_id": "fixture-segment", "text": text, "start_frame": 0,
            "end_frame": 32000, "revision": revision, "produced_run_id": "fixture-asr-run"}])


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("DUE_DATA_DIR", str(tmp_path / "import-only"))
    module = importlib.import_module("due_app.main")
    class IsolatedFake(FakeSupervisor):
        instances = []
    def make(root=None):
        return module.create_app(data_root=root or tmp_path / "active-due",
                                 recording_supervisor_factory=IsolatedFake,
                                 capture_lease_probe=lambda: None)
    make.fake = IsolatedFake
    make.root = tmp_path / "active-due"
    return make


@pytest.fixture
def env(factory):
    app = factory()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client, factory


def context(client, title="S2合成案件"):
    case = client.post(API + "/cases", json={"title": title}).json()
    interview = client.post(API + f"/cases/{case['id']}/interviews", json={"title": "合成访谈"}).json()
    return case["id"], interview["id"]


def path(case_id, interview_id, recording_id=None):
    root = API + f"/cases/{case_id}/interviews/{interview_id}/recordings"
    return root + ("/" + recording_id if recording_id else "")


def start(env, pair=None):
    app, client, factory = env
    case_id, interview_id = pair or context(client)
    response = client.post(path(case_id, interview_id) + "/start", json={"device_id": 1})
    assert response.status_code == 201, response.text
    recording = response.json()
    worker = app.state.recordings.supervisors[recording["id"]]
    return case_id, interview_id, recording, worker


def test_devices_active_start_stop_audio_and_case_refresh(env):
    app, client, _ = env
    devices = client.get(API + "/capture/devices").json()
    assert devices["default_device_id"] == 1 and devices["devices"][0]["name"] == "fixture microphone"
    assert client.get(API + "/capture/active").json() == {"recording": None}
    case_id, interview_id, rec, worker = start(env)
    assert rec["state"] == "recording" and rec["saved_frames"] == 32000
    assert rec["asr"]["state"] == "loading" and not rec["audio_available"]
    assert client.get(API + "/capture/active").json()["recording"]["id"] == rec["id"]
    assert client.get(path(case_id, interview_id, rec["id"]) + "/audio").status_code == 409
    refreshed = client.get(API + f"/cases/{case_id}").json()["interviews"][0]
    assert refreshed["recordings"][0]["id"] == rec["id"] and refreshed["active_recording_id"] == rec["id"]
    stopped = client.post(path(case_id, interview_id, rec["id"]) + "/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "saved" and stopped.json()["audio_available"]
    assert stopped.json()["asr"]["state"] == "loading"  # ASR never gates stop or audio
    assert worker.stop_calls == 1
    audio = client.get(path(case_id, interview_id, rec["id"]) + "/audio")
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    assert client.get(API + "/capture/active").json() == {"recording": None}
    assert client.get(path(case_id, interview_id)).json()["recordings"][0]["id"] == rec["id"]


@pytest.mark.parametrize("payload", [{}, {"device_id": True}, {"device_id": "1"}, {"device_id": 1.0},
    {"device_id": -1}, {"device_id": 10001}, {"device_id": 2},
    {"device_id": 1, "duration_seconds": 0}, {"device_id": 1, "duration_seconds": 28801},
    {"device_id": 1, "capture_options": {"namespace": "test-bypass"}},
    {"device_id": 1, "asr_config": {"engine": "fixture"}},
    {"device_id": 1, "replay_path": "fake.wav"}, {"device_id": 1, "output_root": "other"}])
def test_invalid_devices_and_fixture_overrides_are_rejected(env, payload):
    app, client, _ = env
    pair = context(client)
    response = client.post(path(*pair) + "/start", json=payload)
    assert response.status_code == 422, response.text
    assert client.get(path(*pair)).json()["recordings"] == []


def test_ownership_prevents_cross_case_read_stop_audio_and_history(env):
    app, client, _ = env
    case_a, interview_a, rec, worker = start(env)
    case_b, interview_b = context(client, "S2其他合成案件")
    for pair in [(case_b, interview_b), (case_b, interview_a), (case_a, interview_b)]:
        root = path(*pair, rec["id"])
        for suffix in ["", "/audio", "/segments/history"]:
            assert client.get(root + suffix).status_code == 404
        assert client.post(root + "/stop").status_code == 404
    assert worker.stop_calls == 0
    assert client.post(path(case_b, interview_b) + "/start", json={"device_id": 1}).status_code == 409


def test_asr_failure_does_not_prevent_audio_or_next_recording(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    worker.raw.update(asr_state="failed", asr_error="synthetic ASR crash")
    result = client.post(path(case_id, interview_id, rec["id"]) + "/stop").json()
    assert result["state"] == "saved" and result["audio_available"]
    assert result["asr"]["state"] == "failed" and "synthetic ASR crash" in result["asr"]["error"]
    assert client.get(path(case_id, interview_id, rec["id"]) + "/audio").status_code == 200
    assert client.post(path(case_id, interview_id) + "/start", json={"device_id": 1}).status_code == 201


def test_zero_saved_frames_never_advertises_audio_even_if_worker_flag_is_true(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    client.post(path(case_id, interview_id, rec["id"]) + "/stop")
    worker.raw.update(saved_frames=0, audio_available=True)
    value = client.get(path(case_id, interview_id, rec["id"])).json()
    assert not value["audio_available"]
    assert client.get(path(case_id, interview_id, rec["id"]) + "/audio").status_code == 409


def test_segment_revisions_are_persistent_and_duplicate_refresh_does_not_add_history(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    root = path(case_id, interview_id, rec["id"])
    worker.segments("第一版")
    first = client.get(root).json()["segments"][0]
    assert first["id"] == "fixture-segment" and first["revision"] == 1 and first["stable"]
    client.get(root)
    worker.segments("第二版", revision=2)
    second = client.get(root).json()["segments"][0]
    assert second["id"] == first["id"] and second["revision"] == 2
    history = client.get(root + "/segments/history").json()["revisions"]
    assert [(row["revision"], row["text"]) for row in history] == [(1, "第一版"), (2, "第二版")]


def test_loading_old_text_is_not_stable_and_out_of_saved_range_is_ignored(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    worker.segments()
    worker.raw["asr_state"] = "loading"
    worker.raw["segments"].append({"segment_id": "outside", "text": "不应出现", "start_frame": 32000, "end_frame": 64000})
    values = client.get(path(case_id, interview_id, rec["id"])).json()["segments"]
    assert len(values) == 1 and not values[0]["stable"]


def test_old_run_text_waits_for_current_run_reverification_without_new_revision(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    root = path(case_id, interview_id, rec["id"])
    worker.segments("已有转写")
    segment = worker.raw["segments"][0]
    segment["verified_run_id"] = "fixture-asr-run"
    assert client.get(root).json()["segments"][0]["stable"]
    worker.raw.update(checkpoint_run_id="new-run", current_run_transcribed_frames=0,
                      historical_transcribed_frames=32000, transcript_is_from_previous_run=True)
    value = client.get(root).json()
    assert not value["segments"][0]["stable"] and value["asr"]["transcribed_frames"] == 0
    assert value["asr"]["historical_transcribed_frames"] == 32000
    worker.raw["current_run_transcribed_frames"] = 32000
    assert not client.get(root).json()["segments"][0]["stable"]
    segment["verified_run_id"] = "new-run"
    value = client.get(root).json()["segments"][0]
    assert value["stable"] and value["source_run_id"] == "fixture-asr-run"
    assert len(client.get(root + "/segments/history").json()["revisions"]) == 1


def test_asr_resource_queue_never_blocks_next_capture_and_resumes_later(env):
    app, client, _ = env
    pair = context(client)
    first = start(env, pair)
    client.post(path(*pair, first[2]["id"]) + "/stop")
    second = start(env, pair)
    client.post(path(*pair, second[2]["id"]) + "/stop")
    third = start(env, pair)
    assert third[2]["state"] == "recording"
    assert third[3].config.asr_config["engine"] == "disabled"
    assert third[2]["id"] in app.state.recordings._deferred
    first[3].raw["asr_state"] = "completed"
    deadline = time.monotonic() + 3
    while third[3].start_asr_calls == 0 and time.monotonic() < deadline:
        time.sleep(.05)
    assert third[3].start_asr_calls == 1
    assert third[2]["id"] not in app.state.recordings._deferred


def test_stop_request_survives_snapshot_save_failure(env, monkeypatch):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    def fail(snapshot):
        raise sqlite3.OperationalError("synthetic snapshot database failure")
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings.repository, "save", fail)
        result = client.post(path(case_id, interview_id, rec["id"]) + "/stop")
        # Stop acknowledges the worker command immediately. The independent
        # monitor/refresh reports business persistence failures afterwards.
        refreshed = client.get(path(case_id, interview_id, rec["id"]))
    assert result.status_code == 200, result.text
    assert worker.stop_calls == 1 and refreshed.json()["data_error"]


def test_stop_uses_verified_cached_ownership_if_database_read_is_temporarily_unavailable(env, monkeypatch):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    def fail(*args):
        raise sqlite3.OperationalError("synthetic database read failure")
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings.repository, "get", fail)
        result = client.post(path(case_id, interview_id, rec["id"]) + "/stop")
    assert result.status_code == 200, result.text
    assert worker.stop_calls == 1


def test_stop_response_does_not_wait_for_slow_business_database(env, monkeypatch):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    entered, release = threading.Event(), threading.Event()
    original_save = app.state.recordings.repository.save
    def slow_save(snapshot):
        entered.set()
        release.wait(5)
        return original_save(snapshot)
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(app.state.recordings.repository, "save", slow_save)
        future = pool.submit(client.post, path(case_id, interview_id, rec["id"]) + "/stop")
        try:
            ready_deadline = time.monotonic() + 1
            while not entered.is_set() and not future.done() and time.monotonic() < ready_deadline:
                time.sleep(.01)
            assert worker.stop_calls == 1
            deadline = time.monotonic() + 2
            while not future.done() and time.monotonic() < deadline:
                time.sleep(.02)
            assert future.done(), "stop HTTP response waited over 2 seconds for the business database"
        finally:
            release.set()
            result = future.result(timeout=5)
    assert result.status_code == 200


def test_failed_revision_write_is_retried_after_database_recovers(env, monkeypatch):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    root = path(case_id, interview_id, rec["id"])
    worker.segments("已保存原文")
    assert client.get(root).json()["segments"][0]["revision"] == 1
    def fail(*args):
        raise sqlite3.OperationalError("synthetic revision write failure")
    worker.segments("识别修订文字", revision=2)
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings.repository, "save_segment", fail)
        response = client.get(root)
        assert response.status_code == 200 and response.json()['data_error']
    assert client.get(root).json()['data_error'] is None
    history = client.get(root + "/segments/history").json()["revisions"]
    assert [(row["revision"], row["text"]) for row in history] == [(1, "已保存原文"), (2, "识别修订文字")]


def test_restart_preserves_saved_audio_text_and_revision_history(factory):
    app = factory()
    with TestClient(app) as client:
        case_id, interview_id, rec, worker = start((app, client, factory))
        root = path(case_id, interview_id, rec["id"])
        worker.segments("重开可见的合成文字")
        client.get(root)
        client.post(root + "/stop")
        original_audio = client.get(root + "/audio").content
    with TestClient(factory()) as reopened:
        value = reopened.get(root).json()
        assert value["state"] == "saved" and value["audio_available"]
        assert value["segments"][0]["text"] == "重开可见的合成文字"
        assert reopened.get(root + "/audio").content == original_audio
        assert len(reopened.get(root + "/segments/history").json()["revisions"]) == 1


def test_shutdown_stops_active_capture_before_waiting_for_older_asr(env, monkeypatch):
    app, client, _ = env
    pair = context(client)
    first = start(env, pair)
    client.post(path(*pair, first[2]["id"]) + "/stop")
    second = start(env, pair)
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_asr_shutdown(asr_timeout=30):
            entered.set()
            await release.wait()
        with monkeypatch.context() as patch:
            patch.setattr(first[3], "shutdown", slow_asr_shutdown)
            task = asyncio.create_task(app.state.recordings.shutdown())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.sleep(0)
                assert second[3].stop_calls > 0, "active capture waited behind an older ASR drain"
            finally:
                release.set()
                await task
    client.portal.call(check)


def test_restart_recovers_newer_checkpoint_text_if_business_snapshot_lagged(factory, monkeypatch):
    app = factory()
    async def no_shutdown():
        pass
    async def no_monitor(recording_id):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings, "shutdown", no_shutdown)
        patch.setattr(app.state.recordings, "_monitor", no_monitor)
        with TestClient(app) as client:
            case_id, interview_id, rec, worker = start((app, client, factory))
            root = path(case_id, interview_id, rec["id"])
            # Disk has a later confirmed block and stable text; simulate a backend
            # ending before the next business snapshot, without any real process.
            worker.writer.commit(b"\x03\x00" * 16000)
            worker.writer.close("synthetic backend termination")
            checkpoint = {"v": 1, "owner": worker.config.owner, "active_run_id": "checkpoint-run",
                          "asr_state": "completed", "transcribed_frames": 48000, "asr_error": "", "runs": [],
                          "segments": [{"segment_id": "checkpoint-only", "text": "清单里的最新稳定文字",
                                        "start_frame": 0, "end_frame": 48000, "revision": 1}]}
            atomic_json(worker.config.resolve_output("transcript.json"), checkpoint)
    with TestClient(factory()) as reopened:
        value = reopened.get(root).json()
        assert value["saved_frames"] == 48000 and value["audio_available"]
        assert any(s["text"] == "清单里的最新稳定文字" for s in value["segments"]), value


@pytest.mark.parametrize('corruption', ['invalid_json', 'foreign_owner', 'outside_audio', 'invalid_segment'])
def test_restart_keeps_verified_audio_when_text_checkpoint_is_invalid(factory, monkeypatch, corruption):
    app = factory()
    async def no_shutdown():
        pass
    async def no_monitor(recording_id):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings, 'shutdown', no_shutdown)
        patch.setattr(app.state.recordings, '_monitor', no_monitor)
        with TestClient(app) as client:
            case_id, interview_id, rec, worker = start((app, client, factory))
            root = path(case_id, interview_id, rec['id'])
            worker.writer.close('synthetic backend termination')
            checkpoint = {'owner': 'other-owner' if corruption == 'foreign_owner' else worker.config.owner,
                          'transcribed_frames': 64000 if corruption == 'outside_audio' else 32000,
                          'asr_state': 'completed', 'segments': []}
            if corruption == 'invalid_segment':
                checkpoint['segments'] = [{'segment_id': 'broken', 'text': '无效清单文字', 'start_frame': 0, 'end_frame': 'not-a-frame'}]
            checkpoint_path = worker.config.resolve_output('transcript.json')
            if corruption == 'invalid_json':
                checkpoint_path.write_text('{broken', encoding='utf-8')
            else:
                atomic_json(checkpoint_path, checkpoint)
    with TestClient(factory()) as reopened:
        value = reopened.get(root).json()
        assert value['saved_frames'] == 32000 and value['audio_available']
        assert value['asr']['state'] == 'failed' and value['asr']['error']
        assert reopened.get(root + '/audio').status_code == 200


def test_unknown_legacy_text_provenance_is_not_replaced_by_rechecking_run(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    worker.segments('旧文字')
    worker.raw['segments'][0].update(produced_run_id=None, verified_run_id='fixture-asr-run')
    value = client.get(path(case_id, interview_id, rec['id'])).json()['segments'][0]
    assert value['source_run_id'] is None and value['stable']


@pytest.mark.parametrize('free_bytes', [0, 80 * 1024**2])
def test_low_space_is_rejected_before_recording_or_worker_start(env, monkeypatch, free_bytes):
    app, client, factory = env
    pair = context(client)
    before = len(factory.fake.instances)
    import due_app.recordings as recording_module
    monkeypatch.setattr(recording_module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=free_bytes))
    response = client.post(path(*pair) + '/start', json={'device_id': 1})
    assert response.status_code == 409 and '空间不足' in response.json()['detail']
    assert len(factory.fake.instances) == before
    assert client.get(path(*pair)).json()['recordings'] == []


def test_failed_transcription_can_retry_without_restarting_capture(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    root = path(case_id, interview_id, rec['id'])
    client.post(root + '/stop')
    worker.raw.update(asr_state='failed', asr_error='synthetic failure')
    client.get(root)
    result = client.post(root + '/transcription/retry', json={})
    assert result.status_code == 200 and result.json()['asr']['state'] == 'loading'
    assert result.json()['saved_frames'] == 32000 and result.json()['state'] == 'saved'
    assert worker.start_asr_calls == 1 and worker.stop_calls == 1
    assert client.get(root + '/audio').status_code == 200
    assert client.post(root + '/transcription/retry', json={}).status_code == 409


def test_retry_rejects_cross_case_and_engine_configuration(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    other_case, other_interview = context(client)
    assert client.post(path(other_case, other_interview, rec['id']) + '/transcription/retry', json={}).status_code == 404
    root = path(case_id, interview_id, rec['id'])
    assert client.post(root + '/transcription/retry', json={'asr_config': {'engine': 'cloud'}}).status_code == 422
    assert worker.start_asr_calls == 0


def test_reopened_failed_recording_can_retry_while_preserving_audio(factory):
    with TestClient(factory()) as client:
        app = client.app
        case_id, interview_id, rec, worker = start((app, client, factory))
        root = path(case_id, interview_id, rec['id'])
        client.post(root + '/stop')
        original = client.get(root + '/audio').content
    reopened_app = factory()
    with TestClient(reopened_app) as client:
        assert client.get(root).json()['asr']['state'] == 'failed'
        result = client.post(root + '/transcription/retry', json={})
        assert result.status_code == 200, result.text
        assert result.json()['state'] == 'saved' and result.json()['saved_frames'] == 32000
        assert client.get(root + '/audio').content == original


def test_retry_waits_for_resources_and_does_not_block_another_capture(env):
    app, client, _ = env
    pair = context(client)
    first = start(env, pair)
    first_root = path(*pair, first[2]['id'])
    client.post(first_root + '/stop')
    first[3].raw.update(asr_state='failed', asr_error='synthetic failure')
    client.get(first_root)
    second = start(env, pair)
    client.post(path(*pair, second[2]['id']) + '/stop')
    third = start(env, pair)
    response = client.post(first_root + '/transcription/retry', json={})
    assert response.status_code == 200 and response.json()['asr']['retry_queued']
    assert response.json()['state'] == 'saved' and first[3].start_asr_calls == 0
    assert client.get(first_root).json()['asr']['retry_queued']
    assert client.post(first_root + '/transcription/retry', json={}).status_code == 200
    assert first[3].start_asr_calls == 0
    stopped = client.post(path(*pair, third[2]['id']) + '/stop')
    assert stopped.status_code == 200 and third[3].stop_calls == 1
    second[3].raw['asr_state'] = 'completed'
    deadline = time.monotonic() + 3
    while first[3].start_asr_calls == 0 and time.monotonic() < deadline:
        time.sleep(.05)
    assert first[3].start_asr_calls == 1


def test_interrupted_tail_keeps_unknown_exact_length_distinct_from_estimated_bound(env):
    app, client, _ = env
    case_id, interview_id, rec, worker = start(env)
    worker.raw.update(capture_state='interrupted', capture_error='synthetic hard termination',
                      capture_counters_exact_available=False, received_device_frames=None,
                      unconfirmed_tail_frames_exact=None, unconfirmed_tail_upper_bound_frames=65601)
    value = client.get(path(case_id, interview_id, rec['id'])).json()
    assert value['saved_frames'] == 32000 and value['audio_available']
    assert value['capture_diagnostics']['unconfirmed_tail_frames_exact'] is None
    assert value['capture_diagnostics']['received_device_frames'] is None
    assert value['capture_diagnostics']['unconfirmed_tail_upper_bound_frames'] == 65601


def test_start_asr_preconfiguration_failure_does_not_replace_saved_snapshot(factory, monkeypatch):
    with TestClient(factory()) as client:
        case_id, interview_id, rec, worker = start((client.app, client, factory))
        root = path(case_id, interview_id, rec['id'])
        client.post(root + '/stop')
    reopened = factory()
    async def fail_before_config(self, config):
        raise OSError('synthetic validation failure before configuration')
    with TestClient(reopened) as client, monkeypatch.context() as patch:
        patch.setattr(factory.fake, 'start_asr', fail_before_config)
        response = client.post(root + '/transcription/retry', json={})
        assert response.status_code == 409
        assert rec['id'] not in reopened.state.recordings.supervisors
        value = client.get(root).json()
        assert value['saved_frames'] == 32000 and value['state'] == 'saved' and value['audio_available']
        assert client.get(root + '/audio').status_code == 200


def test_abrupt_restart_updates_cache_to_failed_and_allows_retry(factory, monkeypatch):
    app = factory()
    async def no_shutdown():
        pass
    async def no_monitor(recording_id):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(app.state.recordings, 'shutdown', no_shutdown)
        patch.setattr(app.state.recordings, '_monitor', no_monitor)
        with TestClient(app) as client:
            case_id, interview_id, rec, worker = start((app, client, factory))
            root = path(case_id, interview_id, rec['id'])
            worker.writer.close('synthetic abrupt backend exit')
            # Persisted business state still says capture/ASR active; no graceful
            # shutdown has translated it to failed before the next service opens.
    reopened = factory()
    with TestClient(reopened) as client:
        assert client.get(root).json()['asr']['state'] == 'failed'
        assert reopened.state.recordings.snapshots[rec['id']]['asr']['state'] == 'failed'
        response = client.post(root + '/transcription/retry', json={})
        assert response.status_code == 200 and response.json()['asr']['state'] == 'loading'


def test_dialect_variant_flows_to_session_config_and_snapshot(env, monkeypatch, tmp_path):
    app, client, _ = env
    case_id, interview_id = context(client)
    root = path(case_id, interview_id)
    # unknown variants never reach capture
    assert client.post(root + "/start", json={"device_id": 1, "variant": "cantonese"}).status_code == 422
    # sichuan refuses with a clear message until the dialect model is installed
    home = tmp_path / "funasr-home"
    home.mkdir()
    monkeypatch.setenv("DUE_ASR_HOME", str(home))  # empty home: no dialect registry
    missing = client.post(root + "/start", json={"device_id": 1, "variant": "sichuan"})
    assert missing.status_code == 409 and "四川话" in missing.json()["detail"]
    # with a registered dialect model the variant flows to config and snapshot
    (home / "dialect-models.json").write_text(json.dumps({"asr_model": "models/dialect"}), encoding="utf-8")
    model = home / "models" / "dialect"
    model.mkdir(parents=True)
    (model / "model.pt").write_bytes(b"x")
    (model / "config.yaml").write_bytes(b"x")
    response = client.post(root + "/start", json={"device_id": 1, "variant": "sichuan"})
    assert response.status_code == 201, response.text
    recording = response.json()
    assert recording["variant"] == "sichuan"
    worker = app.state.recordings.supervisors[recording["id"]]
    assert worker.config.asr_config["variant"] == "sichuan"
    client.post(root + "/" + recording["id"] + "/stop")
    # mandarin stays the default and keeps no variant surprises
    monkeypatch.delenv("DUE_ASR_HOME")
    default = client.post(root + "/start", json={"device_id": 1})
    assert default.status_code == 201 and default.json()["variant"] == "mandarin"
