"""Questionnaire admin: required/disabled toggles, _rN version bump, and the
explicit push of the edited master into existing case snapshots."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'shared-recording-core'))

from due_app import diligence as diligence_module
from due_app.answers import AnswerService
from due_app.diligence import DiligenceError, DiligenceService
from due_app.extraction import AnswerExtractionService
from due_app.store import DueStore

CATALOG = {'version': 'q-test-v1', 'fields': [
    {'field_id': 'f.req', 'label': '必问一', 'question_text': '第一题', 'value_type': 'integer',
     'handling': 'interview', 'required': True, 'default_unit': '人'},
    {'field_id': 'f.opt', 'label': '选问一', 'question_text': '第二题', 'value_type': 'text',
     'handling': 'interview', 'required': False},
    {'field_id': 'f.off', 'label': '将被停用', 'question_text': '第三题', 'value_type': 'decimal',
     'handling': 'interview', 'required': True, 'default_unit': '亩'},
]}


class NullRecordings:
    active = staticmethod(lambda: None)
    asr_busy_count = staticmethod(lambda: 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    real = diligence_module.DATA
    for name in ('previsit_schema_s0.json', 'synthetic_cases_s0.json'):
        shutil.copy(real / name, tmp_path / name)
    (tmp_path / 'q.json').write_text(json.dumps(CATALOG, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(diligence_module, 'DATA', tmp_path)
    store = DueStore(tmp_path / 'store')
    diligence = DiligenceService(store, NullRecordings(), catalog_filename='q.json')
    answers = AnswerService(store, diligence)
    extraction = AnswerExtractionService(store, diligence, adapter=SimpleNamespace())
    return SimpleNamespace(store=store, diligence=diligence, answers=answers, extraction=extraction)


def raises(code, fn, *args):
    with pytest.raises(DiligenceError) as exc:
        fn(*args)
    assert exc.value.code == code


def test_update_toggles_flags_bumps_version_and_reloads(env):
    result = env.diligence.update_questionnaire_fields(
        {'f.opt': {'required': True}, 'f.off': {'disabled': True}})
    assert result['version'] == 'q-test-v1_r1'
    assert set(result['updated_fields']) == {'f.opt', 'f.off'}
    # the service reloaded the rewritten file
    assert env.diligence.fields['f.opt']['required'] is True
    assert env.diligence.fields['f.off'].get('disabled') is True
    # a second edit increments the suffix instead of resetting it
    again = env.diligence.update_questionnaire_fields({'f.req': {'required': False}})
    assert again['version'] == 'q-test-v1_r2'
    # unknown fields and no-op updates are refused
    raises('unknown_field', env.diligence.update_questionnaire_fields, {'f.no': {'required': True}})
    raises('no_valid_updates', env.diligence.update_questionnaire_fields, {'f.req': {'required': False}})


def test_disabled_and_required_flow_through_board_progress_extraction(env):
    case = env.store.create_case('题库管理样例', 'farmer', 'farmer_demo')['id']
    board = env.answers.board(case)
    assert len(board['questions']) == 3
    assert board['progress']['required'] == {'done': 0, 'applicable': 2}
    assert board['progress']['optional'] == {'done': 0, 'total': 1}
    # disable one question, promote another to required
    env.diligence.update_questionnaire_fields(
        {'f.off': {'disabled': True}, 'f.opt': {'required': True}})
    board = env.answers.board(case)
    fields = [q['field_id'] for q in board['questions']]
    assert fields == ['f.req', 'f.opt'] and 'f.off' not in fields
    assert board['progress']['required'] == {'done': 0, 'applicable': 2}
    assert board['progress']['optional'] == {'done': 0, 'total': 0}
    # the matcher never receives disabled fields either
    with env.store.connect() as connection:
        _, rows = env.extraction._catalog_rows(connection, case)
    assert {row['field_id'] for row in rows} == {'f.req', 'f.opt'}


def test_apply_refreezes_existing_case_snapshots(env):
    case = env.store.create_case('快照应用样例', 'farmer', 'farmer_demo')['id']
    env.diligence.load_previsit(case, 'SYN-S0-001', 'base')
    env.diligence.update_questionnaire_fields({'f.off': {'disabled': True}})
    result = env.diligence.apply_questionnaire_to_cases()
    assert result['cases_updated'] == 1 and result['version'] == 'q-test-v1_r1'
    # the frozen snapshot now matches the master (board reads it without error)
    board = env.answers.board(case)
    assert board['catalog_version'] == 'q-test-v1_r1'
    assert 'f.off' not in [q['field_id'] for q in board['questions']]
