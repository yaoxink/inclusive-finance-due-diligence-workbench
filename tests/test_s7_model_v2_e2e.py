"""Five actual API workbench flows with no capture, ASR or real model calls."""
from datetime import datetime, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

PROJECT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT/'planning/2026-11-01/implementation/s7-v2'
sys.path.insert(0, str(PROJECT/'due-diligence-app'))
sys.path.insert(0, str(PROJECT/'shared-recording-core'))
SPEC = importlib.util.spec_from_file_location('s7_v2_scenario_runner', EVIDENCE/'scenario_runner.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def assert_frozen_contract(client, receipt):
    frozen, frozen_hash = RUNNER.contract()
    assert receipt['contract_sha256'] == frozen_hash
    scenario = receipt['scenario_id']
    expected = next(item['expected'] for item in frozen['scenarios'] if item['id'] == scenario)
    target = expected['after_recalculation'] if scenario == 'revision' else expected
    prefix = RUNNER.API+'/cases/'+receipt['case_id']
    ws = RUNNER.call(client, 'get', prefix+'/calculation/v2')
    run = ws['latest_run']
    for name in ('state', 'rating', 'stale'):
        if name in target: assert run[name] == target[name]
    for name in ('score', 'effective_limit', 'current_effective_limit'):
        if name not in target: continue
        if target[name] is None: assert run[name] is None
        else: assert Decimal(run[name]) == Decimal(target[name])
    if 'input_field_count' in target:
        assert len(ws['draft']['inputs']['fields']) == target['input_field_count']
    assert ws['draft']['revision'] == target.get('model_input_revision', target.get('input_revision', 1))
    assert run['pricing'] == {'state': 'not_configured', 'rate': None}
    if 'blocking_reason' in target:
        assert target['blocking_reason'] in run['blocking_reasons']
    report = RUNNER.call(client, 'get', prefix+'/reports/'+receipt['current_report_id'])
    assert report['branch'] == target['report_branch']
    assert report['status'] == target.get('report_status', 'draft') and not report['stale']
    if 'report_revision' in target: assert report['revision'] == target['report_revision']
    assert report['snapshot']['calculation']['calculation_id'] == run['id']
    assert report['snapshot']['calculation']['calculation_hash'] == run['business_hash']
    follow = RUNNER.call(client, 'get', prefix+'/followups')
    assert follow['ai']['state'] == expected['followups']['ai_state']
    assert follow['analysis_current'] and len(follow['active_cards']) <= 3
    assert all(c['kind'] == 'rule' for c in follow['active_cards'])
    # questionnaire independence: missing-item cards never surface in the rail
    assert all(c['rule'] != 'missing' for c in follow['active_cards'])
    state = RUNNER.call(client, 'get', prefix+'/diligence')
    onsite = next(f for f in state['facts'] if f['id'] == receipt['fact']['id'])
    assert onsite['record_confirmation'] == 'record_accurate' and onsite['verification'] == 'not_checked'
    # the seeded clarification fact (period-unknown card) may add one onsite row
    assert 1 <= len([f for f in state['facts'] if f['origin'] == 'onsite']) <= 2
    if scenario == 'missing':
        assert 'cashflow.annual_inflow' not in ws['draft']['inputs']['fields']
        assert not any(c['rule'] == 'missing' for c in follow['cards'])
        assert not any(c['field_id'] == 'cashflow.annual_inflow' for c in follow['cards'])
    if scenario == 'conflict':
        conflict = next(c for c in state['conflicts'] if c['field_id'] == 'household.member_count')
        assert conflict['kinds'] == ['value'] and conflict['unresolved']
        facts = [f for f in state['facts'] if f['field_id'] == 'household.member_count']
        assert {f['value'] for f in facts} == {'4', '5'}
        assert all(f['subject_id'] == 'C1-HOUSEHOLD' and f['unit'] == '人'
                   and f['period']['kind'] == 'as_of' and f['period']['start'] == f['period']['end'] == '2026-10-30'
                   and f['calculation_eligibility'] == 'blocked_conflict' for f in facts)
        card = next(c for c in follow['active_cards'] if c['field_id'] == 'household.member_count' and c['rule'] == 'conflict')
        assert card['kind'] == 'rule' and card['priority'] == 0
        assert {(f['id'], f['revision']) for f in facts}.issubset({(s['id'], s['revision']) for s in card['sources']})
        refs = [r for r in report['references'] if r['field_id'] == 'household.member_count']
        assert {r['value'] for r in refs} == {'4', '5'}
        assert {f['id'] for f in facts}.issubset({r['fact_id'] for r in refs})
    if scenario == 'ai_failure':
        assert follow['ai']['enabled'] and follow['ai']['revision'] == 1 and follow['ai']['cloud_fallback'] is False
        assert not any(c['kind'] == 'ai' and not c['stale'] for c in follow['cards'])
    if scenario == 'revision':
        assert receipt['after_input_save_before_run']['current_report_context_effective_limit'] is None
        assert len(receipt['reports']) == 3 and receipt['reports'][1]['kind'] == 'manual'
        assert ws['draft']['inputs']['fields']['crop.main.area']['value'] == '19'
        assert receipt['runs'][0]['business_hash'] != receipt['runs'][1]['business_hash']
        assert receipt['reports'][0]['content_hash'] != receipt['reports'][2]['content_hash']
    numeric_key = target.get('numeric_expected_key')
    if numeric_key:
        numbers = frozen['independent_numeric_expected'][numeric_key]
        for name in ('G10', 'G13', 'G16', 'D42', 'D44', 'D46', 'E23', 'E25', 'E28_interest', 'E28_flow', 'E28'):
            assert Decimal(run['routes'][name]) == Decimal(numbers[name])
        assert Decimal(run['base_score']) == Decimal(numbers['base_score'])
        assert Decimal(run['score_adjustments']) == Decimal(numbers['adjustments'])
        assert Decimal(run['aggregates']['asset_total']) == Decimal(numbers['asset_total'])
        assert Decimal(run['aggregates']['debt_total']) == Decimal(numbers['debt_total'])
        trace = {row['step']: row for row in run['trace']}
        assert Decimal(trace['median']['value']) == Decimal(numbers['median'])
        assert Decimal(trace['adjusted']['value']) == Decimal(numbers['after_area_adjustment'])
        assert Decimal(trace['requested_amount_cap']['value']) == Decimal(numbers['after_product_and_request_cap'])
    return {'state': 'frozen_contract_observed', 'scenario_id': scenario, 'contract_sha256': frozen_hash,
            'current_run_id': run['id'], 'current_report_id': report['id'],
            'followups_ai_state': follow['ai']['state'], 'input_revision': ws['draft']['revision']}


@pytest.fixture
def isolated_host(tmp_path, monkeypatch):
    # The main module's optional global app must also be isolated before import.
    monkeypatch.setenv('DUE_DATA_DIR', str(tmp_path/'import-only'))
    module = importlib.import_module('due_app.main')
    from due_app.followups import OllamaSuggestionAdapter
    events = {'hardware_instances': 0, 'real_model_attempts': 0, 'synthetic_timeout_calls': 0}
    data_root = tmp_path/'owned-synthetic-s7v2'

    class NoHardware:
        def __init__(self, *args, **kwargs):
            events['hardware_instances'] += 1
            raise AssertionError('S7 v2 synthetic API flow forbids capture and ASR')

    def forbidden_model(*args, **kwargs):
        events['real_model_attempts'] += 1
        raise AssertionError('S7 v2 synthetic API flow forbids real model calls')

    class SyntheticTimeout:
        def generate(self, source):
            events['synthetic_timeout_calls'] += 1
            raise TimeoutError('Deterministic synthetic adapter failure; no model was called')

    monkeypatch.setattr(OllamaSuggestionAdapter, 'generate', forbidden_model)
    monkeypatch.setattr(OllamaSuggestionAdapter, 'inventory', forbidden_model)

    def create():
        app = module.create_app(data_root=data_root, recording_supervisor_factory=NoHardware,
                                capture_lease_probe=lambda: None)
        assert app.state.settings.data_root == data_root.resolve()
        app.state.followups.adapter = SyntheticTimeout()
        return app
    return create, events, data_root


@pytest.mark.parametrize('scenario_id', RUNNER.SCENARIOS)
def test_five_current_model_workbench_paths_and_reopen(isolated_host, scenario_id):
    create, events, data_root = isolated_host
    label = os.environ.get('DUE_S7_V2_EVIDENCE_RUN', 'api-first')
    assert label.replace('-', '').isalnum()
    evidence_dir = EVIDENCE/label/scenario_id
    first = create()
    with TestClient(first, raise_server_exceptions=False) as client:
        initial_health = client.get('/api/due-diligence/health').json()
        receipt = RUNNER.execute_case(client, scenario_id, evidence_dir)
        contract_before = assert_frozen_contract(client, receipt)
        assert first.state.recordings.active() is None and first.state.recordings.asr_busy_count() == 0
    # A genuine application lifecycle close/reopen over the same owned data
    # root. This does not claim a new operating-system process or mic test.
    reopened = create()
    with TestClient(reopened, raise_server_exceptions=False) as client:
        next_health = client.get('/api/due-diligence/health').json()
        assert next_health['instance_id'] != initial_health['instance_id']
        proof = RUNNER.verify_reopen(client, receipt)
        contract_after = assert_frozen_contract(client, receipt)
        assert reopened.state.recordings.active() is None and reopened.state.recordings.asr_busy_count() == 0
    assert events['hardware_instances'] == events['real_model_attempts'] == 0
    assert events['synthetic_timeout_calls'] in (
        (1, 2) if scenario_id == 'ai_failure' else (0,))
    assert not list(data_root.rglob('*.wav'))
    payload = {'scenario_id': scenario_id, 'state': 'passed_technical_api_and_app_reopen',
               'checked_at': datetime.now(timezone.utc).isoformat(), 'receipt': receipt,
               'reopen': proof, 'events': events, 'initial_health': initial_health,
               'contract_before': contract_before, 'contract_after': contract_after,
               'reopened_health': next_health, 'data_root': str(data_root),
               'microphone_used': False, 'asr_used': False, 'real_model_used': False,
               'business_acceptance': 'not_performed', 'recording_and_S3_acceptance': 'still_deferred'}
    (evidence_dir/'app-reopen.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
