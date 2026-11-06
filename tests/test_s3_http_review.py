"""Independent HTTP review: synthetic contexts only, never opens a microphone."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'shared-recording-core'))
from test_s2_recordings import FakeSupervisor

API = '/api/due-diligence'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('DUE_DATA_DIR', str(tmp_path / 'import-only'))
    module = importlib.import_module('due_app.main')
    class SyntheticSupervisor(FakeSupervisor):
        instances = []
    app = module.create_app(data_root=tmp_path / 'review',
                            recording_supervisor_factory=SyntheticSupervisor,
                            capture_lease_probe=lambda: None)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


def case(client, title='S3合成审查·无真实资料'):
    response = client.post(API + '/cases', json={'title': title})
    assert response.status_code == 201, response.text
    return response.json()['id']


def scope(case_id):
    return API + f'/cases/{case_id}'


def load(client, case_id, fixture='SYN-S0-001', variant='base'):
    response = client.post(scope(case_id) + '/previsit/load', json={'fixture_id': fixture, 'variant': variant})
    assert response.status_code == 200, response.text
    return response.json()


def post_ok(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def statement(client, case_id, text='我们家有四口人。'):
    return post_ok(client, scope(case_id) + '/statements', {'text': text})


def fact_body(source, value='4', **changes):
    return {'field_id': 'household.member_count', 'statement_id': source['id'],
            'statement_revision': source['revision'], 'value_type': 'integer', 'value': value,
            'presence': 'known', 'unit': '人', 'unit_status': 'explicit',
            'period': {'kind': 'as_of', 'label': '合成测试基准日', 'start': '2026-10-30', 'end': '2026-10-30'},
            'period_status': 'explicit', 'subject_id': 'C1-HOUSEHOLD', 'subject_status': 'explicit',
            'subject_basis': '仅合成演示：本条明确记录申请人家庭总人数。', **changes}


def workspace(client, case_id):
    response = client.get(scope(case_id) + '/diligence')
    assert response.status_code == 200, response.text
    return response.json()


def find_fact(state, identifier):
    return next(f for f in state['facts'] if f['id'] == identifier)


def test_all_five_fixture_contexts_preserve_synthetic_status_and_block_credit(env):
    _, client = env
    examples = client.get(API + '/previsit/examples')
    assert examples.status_code == 200
    assert {e['fixture_id'] for e in examples.json()['examples']} == {f'SYN-S0-00{i}' for i in range(1, 6)}
    for i in range(1, 6):
        cid = case(client, f'S3合成场景{i}')
        state = load(client, cid, f'SYN-S0-00{i}')
        assert state['case_id'] == cid and state['mode'] == 'synthetic_development'
        assert len(state['questionnaire']['fields']) == 57
        assert len({f['field_id'] for f in state['questionnaire']['fields']}) == 57
        assert state['credit']['state'] == 'blocked' and state['credit']['effective_limit'] is None


def test_binding_is_not_by_title_and_cannot_switch_fixture_identity(env):
    _, client = env
    a, b = case(client, '同名合成农户'), case(client, '同名合成农户')
    load(client, a, 'SYN-S0-001'); load(client, b, 'SYN-S0-002')
    refused = client.post(scope(a) + '/previsit/load', json={'fixture_id': 'SYN-S0-002', 'variant': 'base'})
    assert refused.status_code == 409
    assert workspace(client, a)['previsit']['fixture_id'] == 'SYN-S0-001'
    assert workspace(client, b)['previsit']['fixture_id'] == 'SYN-S0-002'


def test_context_versions_cache_and_permission_denial_do_not_masquerade_as_live_data(env):
    _, client = env
    cid = case(client)
    first = load(client, cid)
    updated = load(client, cid, variant='updated')
    assert updated['previsit']['context_version'] > first['previsit']['context_version']
    assert len(updated['previsit']['history']) >= 2
    cached = load(client, cid, variant='unavailable')
    assert cached['previsit']['state'] == 'unavailable' and cached['previsit']['cached']
    denied = client.post(scope(cid) + '/previsit/load', json={'fixture_id': 'SYN-S0-001', 'variant': 'denied'})
    # Switching a local synthetic scenario may return a redacted denied
    # workspace (200). Attempts to read its protected body must still be 403.
    assert denied.status_code in {200, 403}
    hidden = workspace(client, cid)
    assert hidden['previsit']['state'] == 'denied'
    assert not hidden['previsit']['facts'] and not hidden['previsit']['materials']
    assert not [f for f in hidden['facts'] if f.get('origin') == 'previsit']
    for snapshot in hidden['previsit']['history']:
        assert not snapshot.get('facts') and not snapshot.get('materials')
    assert hidden['credit']['effective_limit'] is None
    # A subsequent outage cannot restore access revoked by the denied state.
    down = client.post(scope(cid) + '/previsit/load', json={'fixture_id': 'SYN-S0-001', 'variant': 'unavailable'})
    assert down.status_code in {200, 403}
    still_hidden = workspace(client, cid)
    assert still_hidden['previsit']['state'] == 'denied' and not still_hidden['previsit']['facts']
    old_id = first['previsit']['snapshot_id']
    assert client.get(scope(cid) + f'/previsit/snapshots/{old_id}').status_code == 403


def test_manual_statement_defaults_unknown_and_never_claims_audio(env):
    _, client = env
    cid = case(client);load(client, cid)
    source = statement(client, cid)
    assert source['speaker_role'] == 'unknown' and source['subject_id'] is None
    assert source['has_audio'] is False and source['source_ref'] is None and source['kind'] == 'manual'
    assert source['record_confirmation'] == 'pending'


def test_correcting_candidate_preserves_versions_and_does_not_verify_fact(env):
    _, client = env
    cid = case(client);load(client, cid)
    source = statement(client, cid)
    fact = post_ok(client, scope(cid) + '/facts', fact_body(source, value='9'))
    premature = client.post(scope(cid) + f"/facts/{fact['id']}/confirm", json={'expected_revision': fact['revision']})
    assert premature.status_code == 409
    post_ok(client, scope(cid) + f"/statements/{source['id']}/confirm", {'expected_revision': source['revision']})
    corrected_body = fact_body(source);corrected_body.pop('field_id')
    corrected_body['expected_revision'] = fact['revision']
    corrected = post_ok(client, scope(cid) + f"/facts/{fact['id']}/revise", corrected_body)
    confirmed = post_ok(client, scope(cid) + f"/facts/{fact['id']}/confirm", {'expected_revision': corrected['revision'], 'basis': '已对照合成原文纠正为四人。'})
    assert confirmed['value'] == '4' and confirmed['unit'] == '人'
    assert confirmed['record_confirmation'] == 'record_accurate' and confirmed['verification'] == 'not_checked'
    assert workspace(client, cid)['credit']['effective_limit'] is None
    history = client.get(scope(cid) + f"/facts/{fact['id']}/history")
    assert history.status_code == 200
    assert {'9', '4'} <= {row['value'] for row in history.json()['versions']}
    stale = client.post(scope(cid) + f"/facts/{fact['id']}/revoke", json={'expected_revision': fact['revision']})
    assert stale.status_code == 409


def test_source_revision_does_not_silently_retarget_confirmed_fact(env):
    _, client = env
    cid = case(client);load(client, cid)
    source = statement(client, cid)
    post_ok(client, scope(cid) + f"/statements/{source['id']}/confirm", {'expected_revision': source['revision']})
    fact = post_ok(client, scope(cid) + '/facts', fact_body(source))
    post_ok(client, scope(cid) + f"/facts/{fact['id']}/confirm", {'expected_revision': fact['revision']})
    changed = client.patch(scope(cid) + f"/statements/{source['id']}", json={'expected_revision': source['revision'], 'text': '更正，我们家有五口人。'})
    assert changed.status_code == 200, changed.text
    current = find_fact(workspace(client, cid), fact['id'])
    assert current['value'] == '4' and current['needs_review']
    assert current['verification'] == 'not_checked'
    history = client.get(scope(cid) + f"/statements/{source['id']}/history").json()['versions']
    assert any(row['text'] == '我们家有四口人。' for row in history)


@pytest.mark.parametrize('extra', [{'verification': 'checked_consistent'}, {'calculation_eligibility': 'eligible'}, {'confirmed': True}, {'audio_path': 'arbitrary.wav'}])
def test_client_cannot_inject_verified_or_credit_eligibility_states(env, extra):
    _, client = env
    cid = case(client);load(client, cid)
    source = statement(client, cid)
    response = client.post(scope(cid) + '/facts', json={**fact_body(source), **extra})
    assert response.status_code == 422


def test_cross_case_statement_fact_and_history_access_is_refused(env):
    _, client = env
    a, b = case(client), case(client);load(client, a);load(client, b, 'SYN-S0-002')
    source = statement(client, a)
    fact = post_ok(client, scope(a) + '/facts', fact_body(source))
    for path in [f"/statements/{source['id']}/history", f"/facts/{fact['id']}/history"]:
        assert client.get(scope(b) + path).status_code == 404
    for path in [f"/statements/{source['id']}/confirm", f"/facts/{fact['id']}/confirm", f"/facts/{fact['id']}/revoke"]:
        assert client.post(scope(b) + path, json={'expected_revision': 1}).status_code == 404


def test_stable_segment_reference_is_server_owned_and_revisions_stay_traceable(env):
    app, client = env
    cid = case(client);load(client, cid)
    interview = post_ok(client, scope(cid) + '/interviews', {'title': 'S3合成音频夹具·不访问设备'})
    prefix = scope(cid) + f"/interviews/{interview['id']}/recordings"
    rec = post_ok(client, prefix + '/start', {'device_id': 1})
    worker = app.state.recordings.supervisors[rec['id']]
    worker.segments('我们家有四口人。');client.get(prefix + '/' + rec['id'])
    client.post(prefix + '/' + rec['id'] + '/stop')
    segment_payload = {'interview_id': interview['id'], 'recording_id': rec['id'], 'segment_id': 'fixture-segment', 'segment_revision': 1}
    forged = client.post(scope(cid) + '/statements/from-segment', json={**segment_payload, 'text': '伪造的引用正文'})
    assert forged.status_code == 422
    source = post_ok(client, scope(cid) + '/statements/from-segment', segment_payload)
    assert source['text'] == '我们家有四口人。' and source['has_audio'] and source['kind'] == 'asr_segment'
    assert source['speaker_role'] == 'unknown' and source['subject_id'] is None
    assert source['source_ref']['segment_revision'] == 1
    worker.segments('我们家有五口人。', revision=2);client.get(prefix + '/' + rec['id'])
    state = workspace(client, cid)
    old = next(row for row in state['statements'] if row['id'] == source['id'])
    assert old['text'] == '我们家有四口人。' and old['needs_review']
    stale = client.post(scope(cid) + '/statements/from-segment', json=segment_payload)
    assert stale.status_code == 409


def test_only_fixture_adapter_is_exposed_and_no_attachment_upload_action_exists(env):
    _, client = env
    cid = case(client)
    for payload in [
        {'fixture_id': 'SYN-S0-001', 'variant': 'base', 'url': 'http://example.invalid'},
        {'fixture_id': 'SYN-S0-001', 'variant': 'base', 'snapshot': {'data_class': 'real'}},
        {'fixture_id': 'SYN-S0-001', 'variant': 'base', 'read_originals': True},
    ]:
        assert client.post(scope(cid) + '/previsit/load', json=payload).status_code == 422
    for path in ['/attachments/upload', '/ocr', '/decrypt']:
        assert client.post(scope(cid) + path, json={}).status_code == 404
