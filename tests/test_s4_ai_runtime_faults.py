"""Injected AI faults and fake capture concurrency; never real hardware/models."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.request
import subprocess

import pytest
from fastapi.testclient import TestClient

from test_s2_recordings import factory, path
from test_s3_http_review import API, case, fact_body, load, post_ok, scope, statement, workspace


class InjectedFault:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def generate(self, source):
        self.calls += 1
        raise self.error


class SourceBoundRecovery:
    def generate(self, source):
        ref = next(s for s in source['sources'] if s.get('field_id') == 'household.medical_count')
        return {'suggestions': [{'field_id': 'household.medical_count',
            'question': '请说明医保参保人数的统计范围和截至时点？',
            'reason': '根据本次提供的医保人数记载澄清范围。', 'source_ids': [ref['id']]}]}


class BlockingRecovery(SourceBoundRecovery):
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def generate(self, source):
        self.entered.set()
        if not self.release.wait(20):
            raise TimeoutError('Synthetic blocking-adapter deadline; no network request')
        return super().generate(source)


def prepared(client, title):
    cid = case(client, title)
    load(client, cid)
    source = statement(client, cid, '合成现场原话：我们家常住四口人，医保缴费人数要按今年的记录核对。')
    # missing-item cards are hidden on HTTP; an unconfirmed-period fact yields a
    # genuine rule card so the degraded run has something meaningful to show
    post_ok(client, scope(cid) + '/facts',
            fact_body(source, value='3', period=None, period_status='unknown'))
    post_ok(client, scope(cid) + '/followups/ai-settings', {'enabled': True, 'expected_revision': 0})
    return cid


def business_signature(client, cid):
    state = workspace(client, cid)
    value = {k: state[k] for k in ['previsit', 'questionnaire', 'facts', 'statements', 'credit']}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def model_signature():
    model = Path(__file__).resolve().parents[1] / 'due_app/data/model_farmer_reference_v1.json'
    return hashlib.sha256(model.read_bytes()).hexdigest()


def analyze(client, cid):
    started = time.perf_counter()
    response = client.post(scope(cid) + '/followups/analyze', json={'retry_ai': True})
    assert response.status_code == 200, response.text
    return response.json(), time.perf_counter() - started


def run_injected_fault(app, client, cid, error):
    before = business_signature(client, cid)
    formula = model_signature()
    adapter = InjectedFault(error)
    app.state.followups.adapter = adapter
    state, elapsed = analyze(client, cid)
    assert adapter.calls == 1 and state['ai']['state'] == 'degraded'
    assert state['ai']['cloud_fallback'] is False
    assert state['ai']['accepted_count'] == 0
    assert state['active_cards'] and all(c['kind'] == 'rule' for c in state['active_cards'])
    assert business_signature(client, cid) == before and model_signature() == formula
    health = client.get(API + '/health')
    active = client.get(API + '/capture/active')
    devices = client.get(API + '/capture/devices')
    assert health.status_code == active.status_code == devices.status_code == 200
    assert active.json()['recording'] is None
    return {'injected_exception': type(error).__name__, 'injected_message': str(error),
            'fault_is_simulated_not_actual_network_or_inventory_loss': True,
            'elapsed_seconds': elapsed, 'ai': state['ai'], 'active_cards': state['active_cards'],
            'business_before_sha256': before, 'business_after_sha256': business_signature(client, cid),
            'formula_before_after_sha256': formula,
            'read_only_controls_http': {'health': health.status_code, 'active': active.status_code,
                                        'fake_devices': devices.status_code}}


def run_blocked_capture(app, client):
    cid = prepared(client, '阻塞AI与模拟录音并发·无真实设备')
    interview = post_ok(client, scope(cid) + '/interviews', {'title': '程序模拟采集'})['id']
    adapter = BlockingRecovery()
    app.state.followups.adapter = adapter
    timings = {}
    formula = model_signature()
    facts_before = workspace(client, cid)['facts']
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, scope(cid) + '/followups/analyze', json={'retry_ai': True})
        try:
            assert adapter.entered.wait(3), 'AI request did not enter the controlled adapter'
            started = time.perf_counter()
            capture = client.post(path(cid, interview) + '/start', json={'device_id': 1})
            timings['fake_capture_start_seconds'] = time.perf_counter() - started
            assert capture.status_code == 201, capture.text
            rec = capture.json()
            assert not adapter.release.is_set() and not future.done()
            started = time.perf_counter()
            stopped = client.post(path(cid, interview, rec['id']) + '/stop')
            timings['fake_capture_stop_seconds'] = time.perf_counter() - started
            assert stopped.status_code == 200 and stopped.json()['state'] == 'saved', stopped.text
            assert not adapter.release.is_set() and not future.done()
            started = time.perf_counter()
            saved = statement(client, cid, '合成补充：刚才人数的统计范围只包括日常共同生活的家人。')
            timings['manual_statement_save_seconds'] = time.perf_counter() - started
            assert saved['kind'] == 'manual' and not future.done()
            health = client.get(API + '/health')
            audio = client.get(path(cid, interview, rec['id']) + '/audio')
            active = client.get(API + '/capture/active')
            assert health.status_code == audio.status_code == active.status_code == 200
            assert audio.content.startswith(b'RIFF') and len(audio.content) >= 64044
            assert active.json()['recording'] is None
            assert all(value < 2 for value in timings.values()), timings
        finally:
            adapter.release.set()
        response = future.result(timeout=5)
    # Saving a new source while the old inference waits must invalidate it.
    assert response.status_code == 409 and response.json()['detail']['code'] == 'source_changed'
    state = workspace(client, cid)
    assert any(s['id'] == saved['id'] for s in state['statements']) and state['facts'] == facts_before
    assert model_signature() == formula
    worker = app.state.recordings.supervisors[rec['id']]
    assert worker.stop_calls == 1 and worker.config is not None
    return {'scope': 'FakeSupervisor synthetic PCM only; no hardware/ASR worker',
            'case_id': cid, 'recording_id': rec['id'], 'timings': timings,
            'responses_completed_while_ai_was_blocked': True,
            'fake_saved_frames': stopped.json()['saved_frames'], 'fake_audio_bytes': len(audio.content),
            'manual_statement_id': saved['id'], 'ai_old_response_http': response.status_code,
            'ai_old_response': response.json(), 'facts_count': len(state['facts']),
            'fact_list_unchanged': True,
            'formula_before_after_sha256': formula,
            'does_not_accept_real_microphone_or_ui_recording_interference': True}


@pytest.fixture
def runtime_env(factory):
    app = factory()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


@pytest.mark.parametrize('kind', ['timeout', 'missing_model'])
def test_injected_fault_degrades_and_explicit_retry_recovers_without_business_change(runtime_env, monkeypatch, kind):
    app, client = runtime_env
    cid = prepared(client, '合成AI故障与恢复')
    def forbidden_io(*args, **kwargs):
        raise AssertionError('Deterministic injected fault/recovery must not open a network connection or spawn a downloader')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden_io)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_io)
    error = TimeoutError('injected_local_timeout') if kind == 'timeout' else RuntimeError('configured_local_model_missing')
    run_injected_fault(app, client, cid, error)
    before = business_signature(client, cid)
    app.state.followups.adapter = SourceBoundRecovery()
    recovered, _ = analyze(client, cid)
    assert recovered['ai']['state'] == 'ready' and recovered['ai']['accepted_count'] == 1
    assert recovered['ai']['cloud_fallback'] is False and business_signature(client, cid) == before


def test_blocked_ai_does_not_block_fake_capture_stop_or_manual_source_save(runtime_env, monkeypatch):
    app, client = runtime_env
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError('This test must never start a hardware/model subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden_spawn)
    run_blocked_capture(app, client)
