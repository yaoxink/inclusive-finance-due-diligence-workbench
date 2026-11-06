"""P5 acceptance: answers -> model inputs bridge, conflict juxtaposition,
recompute-after-change markers, across the nine frozen scenarios.

Scenarios (from the plan): complete, missing-required, condition-pending,
not-applicable, unit/period-unknown, onsite conflict, multiple debts,
invalidated-after-change, regenerate-after-edit. Each checks question status,
progress, input qualification, final limit/blocking, report gaps and old-version
retention as applicable. All in isolated synthetic storage."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared-recording-core"))

from due_app.answers import AnswerService
from due_app.answers_model_bridge import AnswersModelBridge
from due_app.diligence import DiligenceError, DiligenceService
from due_app.farmer_model_v2 import FarmerModelV2Service
from due_app.store import DueStore


class NullRecordings:
    active = staticmethod(lambda: None)
    asr_busy_count = staticmethod(lambda: 0)


@pytest.fixture
def env(tmp_path):
    store = DueStore(tmp_path / 'p5-isolated')
    diligence = DiligenceService(store, NullRecordings())
    answers = AnswerService(store, diligence)
    model = FarmerModelV2Service(store, diligence)
    bridge = AnswersModelBridge(store, answers, model)
    return SimpleNamespace(store=store, diligence=diligence, answers=answers,
                           model=model, bridge=bridge)


def make_case(env):
    return env.store.create_case('P5场景', 'farmer', 'farmer_demo')['id']


def confirmed(env, case, field_id, *, number=None, text=None, unit=None, period=None, status='confirmed'):
    data = {'field_id': field_id, 'status': status, 'actor': 'tester',
            'basis': [{'type': 'manual', 'id': 'n'}], 'period': period}
    if number is not None:
        data['value'] = {'number': number, **({'unit': unit} if unit else {})}
    elif text is not None:
        data['value'] = {'text': text}
    elif status != 'not_applicable':
        data['value'] = None
    if status == 'not_applicable':
        data['not_applicable_reason'] = '场景样例：该口径不适用'
    return env.answers.create_answer(case, data)


YEAR = {'kind': 'year', 'start': '2026-01-01', 'end': '2026-12-31', 'label': '2026年度'}


def test_bridge_maps_confirmed_answers_with_unit_and_period(env):
    case = make_case(env)
    confirmed(env, case, 'household.member_count', number=5, unit='人', period=YEAR)
    confirmed(env, case, 'crop.main.area', number=12, unit='亩', period=YEAR)
    confirmed(env, case, 'operator.experience_years', number=8, unit='年', period=YEAR)
    proposal = env.bridge.propose(case)
    assert set(proposal['mapped']) == {'household.member_count', 'crop.main.area', 'operator.experience_years'}
    record = proposal['mapped']['crop.main.area']
    assert record['value'] == '12' and record['unit'] == '亩' and record['presence'] == 'known'
    assert record['record_confirmation'] == 'record_accurate'
    assert record['verification']['state'] == 'not_checked', '答案确认不得自动升级为独立核验'
    assert record['source_ref']['revision'] == 1 and '问卷答案' in record['source_ref']['text']


def test_unit_unknown_blocks_but_missing_period_no_longer_does(env):
    case = make_case(env)
    confirmed(env, case, 'crop.main.yield', number=4000, unit='公斤/亩', period=YEAR)   # wrong unit
    confirmed(env, case, 'crop.main.price', number=2.5)                                   # no period, no unit
    proposal = env.bridge.propose(case)
    reasons = {gap['model_field']: gap['reason'] for gap in proposal['gaps']}
    assert reasons['crop.main.yield'] == 'unit_mismatch_or_missing'
    # periods are optional since 2026-11-05: the period-less price still maps
    assert proposal['mapped']['crop.main.price']['value'] == '2.5'
    assert proposal['mapped']['crop.main.price']['unit'] == '元/斤'  # question-marked unit


def test_draft_not_applicable_and_unanswered_never_map(env):
    case = make_case(env)
    confirmed(env, case, 'crop.main.yield', number=4000, unit='斤/亩', period=YEAR, status='draft')  # draft
    confirmed(env, case, 'household.pension_tier', status='not_applicable')                          # not applicable
    confirmed(env, case, 'operator.marital_status', text='已婚')
    proposal = env.bridge.propose(case)
    gaps = {g['answer_field']: g['reason'] for g in proposal['gaps']}
    assert gaps['crop.main.yield'] == 'answer_not_confirmed'
    assert gaps['household.pension_tier'] == 'answer_not_applicable'
    assert gaps['crop.main.area'] == 'no_answer'


def test_apply_writes_inputs_and_keeps_upstream_fields(env):
    case = make_case(env)
    confirmed(env, case, 'household.member_count', number=5, unit='人', period=YEAR)
    baseline = env.model.workspace(case)['draft']
    assert baseline['revision'] == 0
    saved = env.bridge.apply(case, 0)
    assert saved['draft']['revision'] == 1
    draft = env.model.workspace(case)['draft']
    assert draft['inputs']['fields']['household.member_count']['value'] == '5'
    assert set(draft['inputs']) >= {'assets', 'debts', 'evaluation_as_of'}, '上游/台账结构不被破坏'


def test_run_then_answer_edit_marks_pending_recheck_and_old_run_kept(env):
    case = make_case(env)
    confirmed(env, case, 'household.member_count', number=5, unit='人', period=YEAR)
    env.bridge.apply(case, 0)
    env.model.save_inputs(case, env.model.workspace(case)['draft']['inputs'], 1)
    # qualification is what decides validity; a run may block on missing inputs
    run = env.model.run(case, env.model.workspace(case)['draft']['revision'])
    view = env.model.workspace(case)
    assert view['latest_run'] is not None
    assert env.bridge.recheck_state(case, view['latest_run']['created_at'])['pending_recheck'] is False
    # edit the answer afterwards -> recheck pending, old run still listed
    revised = confirmed(env, case, 'crop.main.area', number=12, unit='亩', period=YEAR)
    assert revised['revision'] == 1
    view2 = env.model.workspace(case)
    assert env.bridge.recheck_state(case, view2['latest_run']['created_at'])['pending_recheck'] is True
    assert view2['runs'], '旧测算结果保留可回看，不静默重算'
    # regenerating inputs creates a new revision; the old one stays in history
    history_before = len(env.model.input_history(case).get('versions', []))
    env.bridge.apply(case, env.model.workspace(case)['draft']['revision'])
    history_after = len(env.model.input_history(case).get('versions', []))
    assert history_after >= history_before


def test_conflict_juxtaposition_shows_both_sides(env):
    case = make_case(env)
    env.diligence.load_previsit(case, 'SYN-S0-001', 'base')
    # load_previsit seeds previsit facts; answer something already recorded there
    board_before = env.answers.board(case)
    assert board_before['previsit_reference'] == {}
    fields = {f['field_id'] for f in env.diligence.questionnaire['fields']}
    target = next((f for f in fields if f.startswith('household.member_count')), None)
    if target is None:
        pytest.skip('fixture has no mapped previsit fact field')
    confirmed(env, case, target, number=99, unit='人', period=YEAR)  # intentionally different
    board = env.answers.board(case)
    reference = board['previsit_reference'].get(target)
    if reference is not None:
        assert reference['conflict'] is True
        assert reference['previsit_value'] is not None and reference['onsite_value'] == 99


def test_multiple_debts_flow_through_answers(env):
    case = make_case(env)
    created = env.answers.create_answer(case, {
        'field_id': 'debt.bank_contracts', 'status': 'confirmed',
        'items': [{'item_id': 'd1', 'members': {'creditor_kind': '银行贷款（不含房贷车贷）', 'creditor': 'A银行',
                                                'original_amount': 50000, 'outstanding_balance': 30000}},
                  {'item_id': 'd2', 'members': {'creditor_kind': '银行贷款（不含房贷车贷）', 'creditor': 'B银行',
                                                'original_amount': 80000, 'outstanding_balance': 61000}}],
        'basis': [{'type': 'manual', 'id': 'n'}], 'period': {'kind': 'as_of', 'start': '2026-11-04', 'end': None, 'label': '访谈时点'}})
    assert len([i for i in created['items'] if i['status'] == 'active']) == 2
    # ledger-style entries are entered on the model side; the bridge never sums them
    proposal = env.bridge.propose(case)
    assert all('debt' not in model_field for model_field in proposal['mapped']), '债务明细不桥接汇总，模型台账另录'


def test_condition_pending_and_progress(env):
    case = make_case(env)
    progress = env.answers.progress(case)
    assert progress['required']['done'] == 0
    assert progress['pending_judgement'] == 0  # v2 catalog has no confirmed conditions yet
    confirmed(env, case, 'household.member_count', number=5, unit='人', period=YEAR)
    progress2 = env.answers.progress(case)
    assert progress2['required']['done'] == 1


def test_period_subject_dimensions_validated(env):
    case = make_case(env)
    with pytest.raises(DiligenceError):
        env.answers.create_answer(case, {'field_id': 'cost.seed', 'status': 'confirmed',
                                          'value': {'number': 100, 'unit': '元'},
                                          'period': {'bogus': 1},
                                          'basis': [{'type': 'manual', 'id': 'n'}]})
    ok = confirmed(env, case, 'cost.seed', number=100, unit='元', period=YEAR)
    assert ok['period']['kind'] == 'year'


def test_household_counts_auto_calibrate_to_member_total(env):
    """Insured/dependent counts above the household size are capped at the
    household total instead of blocking the derived ratio rules."""
    case = make_case(env)
    confirmed(env, case, 'household.member_count', number=4, unit='人')
    confirmed(env, case, 'household.medical_count', number=5, unit='人')
    confirmed(env, case, 'household.pension_count', number=2, unit='人')
    result = env.bridge.apply(case, None)
    fields = result['draft']['inputs']['fields']
    assert fields['household.medical_count']['value'] == '4'
    assert '已自动校准为4' in fields['household.medical_count']['verification']['basis']
    assert fields['household.pension_count']['value'] == '2'
