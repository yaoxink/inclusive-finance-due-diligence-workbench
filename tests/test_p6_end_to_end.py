"""P6 end-to-end demonstration over HTTP: the whole loop on an isolated
synthetic case — prepare (customer info) → questions (manual + LLM candidate
adoption) → progress → bridge into model inputs → calculation → report with
gaps → reopen — plus the degraded path when the LLM is unavailable.

The extraction adapter is a deterministic fake here; the real cloud endpoint
was validated separately in the frozen-sample evaluation (P4)."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared-recording-core"))

from due_app.main import create_app  # noqa: E402


class FakeAdapter:
    def __init__(self):
        self.failing = False

    def extract(self, catalog_rows, segment_rows):
        if self.failing:
            raise TimeoutError('simulated cloud outage')
        by_field = {row['field_id'] for row in catalog_rows}
        text = ''.join(row['text'] for row in segment_rows)
        candidates = []
        if 'household.member_count' in by_field and '五口人' in text:
            seg = next(row for row in segment_rows if '五口人' in row['text'])
            candidates.append({'field_id': 'household.member_count', 'value_kind': 'number', 'value': 5,
                               'unit': '人', 'period': None, 'subject': '家庭',
                               'quote': '我们家一共五口人', 'recording_id': seg['recording_id'],
                               'segment_id': seg['segment_id'], 'segment_revision': seg['revision'],
                               'reason': '原话明确'})
        return {'candidates': candidates}


@pytest.fixture
def client(tmp_path):
    app = create_app(data_root=str(tmp_path / 'p6-e2e'))
    app.state.extraction.adapter = FakeAdapter()
    with TestClient(app) as test_client:
        yield test_client, app.state.extraction.adapter


def seed_recording(client, case_id, interview_id):
    import json as _json
    segments = [{'id': 'seg_a', 'ordinal': 1, 'revision': 1, 'stable': True,
                 'text': '我们家一共五口人。', 'start_seconds': 0.0, 'end_seconds': 5.0,
                 'source_run_id': 'demo'},
                {'id': 'seg_b', 'ordinal': 2, 'revision': 1, 'stable': True,
                 'text': '今年种了十二亩柑橘。', 'start_seconds': 5.0, 'end_seconds': 12.0,
                 'source_run_id': 'demo'}]
    snapshot = {'id': 'rec_e2e', 'state': 'saved', 'capture_state': 'saved',
                'saved_seconds': 12.0, 'saved_frames': 192000, 'data_error': '',
                'asr': {'state': 'completed'}, 'segments': segments}
    app_db = client.app
    from due_app.store import APP_ID
    with app_db.state.store.lock, app_db.state.store.connect() as connection:
        connection.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)',
                           ('rec_e2e', case_id, interview_id, APP_ID,
                            '2026-11-04T00:00:00Z', '2026-11-04T00:00:12Z', '{}',
                            _json.dumps(snapshot, ensure_ascii=False)))


def test_full_loop_prepare_questions_candidates_progress_calculation_report(client):
    test, adapter = client

    # --- prepare: case + interview + customer info ---
    case = test.post('/api/due-diligence/cases', json={'title': 'P6端到端演示'}).json()['id']
    interview = test.post(f'/api/due-diligence/cases/{case}/interviews', json={'title': '现场访谈'}).json()['id']
    # new cases arrive with a randomly generated synthetic identity (revision 1)
    pre = test.get(f'/api/due-diligence/cases/{case}/customer-info').json()
    assert pre['customer_info'] and pre['customer_info']['fields']['customer_name']
    info = test.put(f'/api/due-diligence/cases/{case}/customer-info',
                    json={'fields': {'customer_name': '演示农户', 'id_type': '居民身份证',
                                     'id_number': '510123199001011234'},
                          'expected_revision': pre['customer_info']['revision'], 'actor': 'manager'})
    assert info.status_code == 200 and info.json()['revision'] == 2

    # --- onsite: stable transcript seeded, extraction run, candidate adopted ---
    seed_recording(test, case, interview)
    run = test.post(f'/api/due-diligence/cases/{case}/extraction/run', json={})
    assert run.status_code == 200 and run.json()['candidates_accepted'] == 1
    candidate = run.json()['candidates'][0]
    adopted = test.post(f'/api/due-diligence/cases/{case}/answers/adopt-candidate',
                        json={'candidate_id': candidate['candidate_id'], 'status': 'confirmed',
                              'actor': 'manager'})
    assert adopted.status_code == 200 and adopted.json()['origin'] == 'llm_candidate_adopted'
    # the adopted candidate carries no period; the person supplements it before
    # the answer can enter model inputs (the bridge lists it as a gap otherwise)
    supplemented = test.put(f'/api/due-diligence/cases/{case}/answers/{adopted.json()["answer_id"]}',
                            json={'expected_revision': 1, 'status': 'confirmed', 'actor': 'manager',
                                  'period': {'kind': 'year', 'start': '2025-01-01', 'end': '2025-12-31',
                                             'label': '2025年度'}})
    assert supplemented.status_code == 200

    # --- manual answer with unit+period for the model bridge ---
    manual = test.post(f'/api/due-diligence/cases/{case}/answers', json={
        'field_id': 'crop.main.area', 'status': 'confirmed', 'actor': 'manager',
        'value': {'number': 12, 'unit': '亩'},
        'period': {'kind': 'year', 'start': '2025-01-01', 'end': '2025-12-31', 'label': '2025年度'},
        'basis': [{'type': 'manual', 'id': 'note', 'note': '农户口述'}]})
    assert manual.status_code == 201

    # --- progress reflects confirmed answers ---
    progress = test.get(f'/api/due-diligence/cases/{case}/progress').json()
    assert progress['required']['done'] == 2

    # --- bridge into model inputs, then run: gaps visible, no fake limit ---
    proposal = test.get(f'/api/due-diligence/cases/{case}/calculation/v2/answers-bridge').json()
    assert 'household.member_count' in proposal['mapped'] and 'crop.main.area' in proposal['mapped']
    applied = test.post(f'/api/due-diligence/cases/{case}/calculation/v2/from-answers',
                        json={'expected_revision': 0})
    assert applied.status_code == 200
    run_result = test.post(f'/api/due-diligence/cases/{case}/calculation/v2/run',
                           json={'expected_revision': applied.json()['draft']['revision']})
    assert run_result.status_code == 201
    assert run_result.json()['state'] == 'blocked', '不完整输入必须阻断，不得出现伪有效额度'
    workspace = test.get(f'/api/due-diligence/cases/{case}/calculation/v2').json()
    assert workspace['qualification']['state'] == 'blocked'
    assert workspace['answers_recheck']['pending_recheck'] is False

    # --- report with gaps can still be exported as draft ---
    report = test.post(f'/api/due-diligence/cases/{case}/reports', json={})
    assert report.status_code == 201, report.text
    report_id = report.json()['id']
    docx = test.get(f'/api/due-diligence/cases/{case}/reports/{report_id}/export.docx')
    assert docx.status_code == 200 and docx.headers['content-type'].startswith('application/vnd')

    # --- answer edited afterwards -> recheck pending, old run/report readable ---
    answer_id = manual.json()['answer_id']
    revised = test.put(f'/api/due-diligence/cases/{case}/answers/{answer_id}',
                       json={'expected_revision': 1, 'value': {'number': 15, 'unit': '亩'},
                             'status': 'confirmed', 'actor': 'manager'})
    assert revised.status_code == 200
    workspace2 = test.get(f'/api/due-diligence/cases/{case}/calculation/v2').json()
    assert workspace2['answers_recheck']['pending_recheck'] is True
    old_report = test.get(f'/api/due-diligence/cases/{case}/reports/{report_id}')
    assert old_report.status_code == 200, '旧报告保持可读'

    # --- reopen: the case and its history stay queryable ---
    history = test.get(f'/api/due-diligence/cases/{case}/answers/{answer_id}/history')
    assert [r['revision'] for r in history.json()['revisions']] == [1, 2]


def test_llm_outage_manual_fallback_full_path(client):
    test, adapter = client
    adapter.failing = True
    case = test.post('/api/due-due'[:0] or '/api/due-diligence/cases', json={'title': 'P6降级链路'}).json()['id']
    test.post(f'/api/due-diligence/cases/{case}/interviews', json={'title': '现场访谈'})
    seed_recording(test, case, test.get(f'/api/due-diligence/cases/{case}').json()['interviews'][0]['id'])
    run = test.post(f'/api/due-diligence/cases/{case}/extraction/run', json={})
    assert run.status_code == 503, '模型故障必须显式失败'
    # manual answering still works and drives the same progress
    manual = test.post(f'/api/due-diligence/cases/{case}/answers', json={
        'field_id': 'household.member_count', 'status': 'confirmed', 'actor': 'manager',
        'value': {'number': 5, 'unit': '人'},
        'period': {'kind': 'year', 'start': '2025-01-01', 'end': '2025-12-31', 'label': '2025年度'},
        'basis': [{'type': 'manual', 'id': 'n'}]})
    assert manual.status_code == 201
    progress = test.get(f'/api/due-diligence/cases/{case}/progress').json()
    assert progress['required']['done'] == 1
