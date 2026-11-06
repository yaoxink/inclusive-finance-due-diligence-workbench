"""P4 acceptance: extraction contract, candidate validation, dedupe/frequency
limits, degradation and adoption into answers. Uses a fake adapter — no real
model calls here; the live endpoint smoke is recorded separately."""
from __future__ import annotations

from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared-recording-core"))

from due_app.answers import AnswerService
from due_app.diligence import DiligenceError, DiligenceService
from due_app.extraction import AnswerExtractionService
from due_app.store import DueStore


class NullRecordings:
    active = staticmethod(lambda: None)
    asr_busy_count = staticmethod(lambda: 0)


SEGMENTS = [
    {'recording_id': 'rec_1', 'interview_id': 'iv_1', 'segment_id': 'seg_1', 'segment_revision': 2,
     'text': '我们家一共五口人。', 'start_seconds': 0, 'end_seconds': 6, 'source_run_id': None},
    {'recording_id': 'rec_1', 'interview_id': 'iv_1', 'segment_id': 'seg_2', 'segment_revision': 1,
     'text': '今年种了十二亩柑橘，没有欠银行的钱。', 'start_seconds': 7.2, 'end_seconds': 20, 'source_run_id': None},
]
CATALOG = {'version': 'test-v1', 'fields': [
    {'field_id': 'household.member_count', 'label': '家庭总人数', 'question_text': '家里几口人',
     'value_type': 'integer', 'handling': 'interview', 'default_unit': '人', 'required': True},
    {'field_id': 'crop.main.area', 'label': '主营种植面积', 'question_text': '主营种了多少亩',
     'value_type': 'decimal', 'handling': 'interview', 'default_unit': '亩', 'required': True},
    {'field_id': 'debt.flag', 'label': '是否有银行贷款', 'question_text': '有没有欠银行的钱',
     'value_type': 'boolean', 'handling': 'interview', 'required': True},
]}


class FakeAdapter:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def extract(self, catalog_rows, segment_rows):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = DueStore(tmp_path / 'p4-isolated')
    # freeze the catalog by injecting DiligenceService's active questionnaire
    monkeypatch.setattr(DiligenceService, '__init__', _patched_init(CATALOG), raising=True)
    diligence = DiligenceService(store, NullRecordings())
    answers = AnswerService(store, diligence)
    extraction = AnswerExtractionService(store, diligence, adapter=FakeAdapter())
    return SimpleNamespace(store=store, diligence=diligence, answers=answers, extraction=extraction)


def _patched_init(catalog):
    original = DiligenceService.__init__

    def init(self, store, recordings, *, catalog_filename=None):
        original(self, store, recordings)
        self.questionnaire = catalog
        self.fields = {f['field_id']: f for f in catalog['fields']}

    return init


def make_case(env):
    return env.store.create_case('P4样例', 'farmer', 'farmer_demo')['id']


def with_segments(env, case_id, segments):
    env.extraction._stable_rows = lambda connection, cid: segments if cid == case_id else []
    return env.extraction


def raises(code, fn, *args, **kwargs):
    with pytest.raises(DiligenceError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


def good_reply():
    return {'candidates': [
        {'field_id': 'household.member_count', 'value_kind': 'number', 'value': 5, 'unit': '人',
         'period': None, 'subject': '家庭', 'quote': '我们家一共五口人', 'recording_id': 'rec_1',
         'segment_id': 'seg_1', 'segment_revision': 2, 'reason': '原话直接说明人数'},
        {'field_id': 'crop.main.area', 'value_kind': 'number', 'value': 12, 'unit': '亩',
         'period': None, 'subject': None, 'quote': '种了十二亩柑橘', 'recording_id': 'rec_1',
         'segment_id': 'seg_2', 'segment_revision': 1, 'reason': '原话说明面积'},
        {'field_id': 'debt.flag', 'value_kind': 'boolean', 'value': False, 'unit': None,
         'period': None, 'subject': None, 'quote': '没有欠银行的钱', 'recording_id': 'rec_1',
         'segment_id': 'seg_2', 'segment_revision': 1, 'reason': '否定表述'}]}


def test_valid_candidates_stored_with_verbatim_evidence(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(reply=good_reply())
    result = env.extraction.run(case)
    assert result['status'] == 'done' and result['candidates_accepted'] == 3 and result['candidates_rejected'] == 0
    stored = env.extraction.candidates(case)
    assert len(stored['candidates']) == 3
    member = next(c for c in stored['candidates'] if c['field_id'] == 'household.member_count')
    assert member['quote'] in SEGMENTS[0]['text'] and member['value'] == 5


def test_forgeries_and_stale_versions_rejected(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    reply = good_reply()['candidates']
    reply.append({'field_id': 'crop.main.area', 'value_kind': 'number', 'value': 99, 'quote': '种了九十九亩',
                  'recording_id': 'rec_1', 'segment_id': 'seg_2', 'segment_revision': 1,
                  'reason': '伪造引用'})  # quote not verbatim
    reply.append({'field_id': 'household.member_count', 'value_kind': 'number', 'value': 4,
                  'quote': '我们家一共五口人', 'recording_id': 'rec_1', 'segment_id': 'seg_1',
                  'segment_revision': 1, 'reason': '过期版本'})  # stale revision
    reply.append({'field_id': 'no.such.field', 'value_kind': 'number', 'value': 1,
                  'quote': '我们家一共五口人', 'recording_id': 'rec_1', 'segment_id': 'seg_1',
                  'segment_revision': 2, 'reason': '目录外题目'})
    reply.append({'field_id': 'household.member_count', 'value_kind': 'number', 'value': 6,
                  'quote': '我们家一共五口人', 'recording_id': 'rec_other', 'segment_id': 'seg_1',
                  'segment_revision': 2, 'reason': '跨案来源'})
    env.extraction.adapter = FakeAdapter(reply={'candidates': reply})
    result = env.extraction.run(case)
    assert result['candidates_accepted'] == 3 and result['candidates_rejected'] == 4
    reasons = {r['reason'] for r in result['rejected']}
    assert {'quote_not_verbatim_in_segment', 'segment_revision_stale', 'unknown_field',
            'segment_not_in_this_case_input'} <= reasons


def test_duplicate_run_deduplicated_and_garbled_rejected(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    adapter = FakeAdapter(reply=good_reply())
    env.extraction.adapter = adapter
    first = env.extraction.run(case)
    second = env.extraction.run(case)
    assert second['deduplicated'] is True and second['deduplicated_from_run'] == first['run_id']
    assert adapter.calls == 1, 'same fingerprint must not re-call the model within cooldown'
    garbled = FakeAdapter(reply={'candidates': [{'field_id': 'crop.main.area', 'value_kind': 'text',
                                                 'value': '十二亩', 'quote': '种了十二亩柑橘',
                                                 'recording_id': 'rec_1', 'segment_id': 'seg_2',
                                                 'segment_revision': 1, 'reason': 'kind与值不符'}],
                                 'noise': True})
    env.extraction.adapter = garbled
    third = env.extraction.run(case, force=True)
    assert third['candidates_accepted'] == 0 and third['candidates_rejected'] == 1


def test_model_failure_degrades_visibly_and_recording_unaffected(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(error=TimeoutError('api timeout'))
    raises('extraction_model_unavailable', env.extraction.run, case)
    runs = env.extraction.runs(case)
    assert runs['runs'][0]['summary']['status'] == 'failed'
    # manual answering still works untouched
    answer = env.answers.create_answer(case, {'field_id': 'household.member_count', 'status': 'confirmed',
                                              'value': {'number': 4}, 'basis': [{'type': 'manual', 'id': 'n'}]})
    assert answer['revision'] == 1


def test_adopt_candidate_creates_answer_with_segment_basis(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(reply=good_reply())
    env.extraction.run(case)
    candidates = env.extraction.candidates(case)['candidates']
    target = next(c for c in candidates if c['field_id'] == 'debt.flag')
    adopted = env.answers.adopt_candidate(case, {'candidate_id': target['candidate_id'],
                                                 'status': 'confirmed', 'actor': 'manager_a',
                                                 'note': '口述无欠款，待流水核验'})
    assert adopted['status'] == 'confirmed' and adopted['origin'] == 'llm_candidate_adopted'
    assert adopted['value'] == {'boolean': False}
    assert adopted['basis'][0]['type'] == 'segment' and '没有欠银行的钱' in adopted['basis'][0]['note']
    progress = env.answers.progress(case)
    assert progress['required']['done'] == 1
    # adopting twice for the same field is refused
    raises('answer_exists', env.answers.adopt_candidate, case,
           {'candidate_id': target['candidate_id'], 'status': 'draft'})
    # cross-case candidate is rejected
    other = make_case(env)
    raises('candidate_not_found', env.answers.adopt_candidate, other,
           {'candidate_id': target['candidate_id'], 'status': 'draft'})


def test_input_window_guard(env):
    case = make_case(env)
    big = [dict(segment, text='很长的话' * 50) for segment in SEGMENTS for _ in range(60)]
    env.extraction = with_segments(env, case, big)
    raises('extraction_window_too_large', env.extraction.run, case)


def agent_reply_with_suggestions():
    return {**good_reply(), 'suggestions': [
        {'field_id': 'crop.main.area', 'question': '种的十二亩是自家承包地还是流转来的？',
         'reason': '原话提到种了十二亩但未说明土地来源', 'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'field_id': 'no.such.field', 'question': '问题', 'reason': '字段不在目录',
         'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'field_id': 'debt.flag', 'question': '能保证贷款必批吗？', 'reason': '违规措辞',
         'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'field_id': 'household.member_count', 'question': '五口人里包含外出务工的吗？',
         'reason': '引用不存在的语句', 'recording_id': 'rec_1', 'segment_id': 'seg_404'},
        {'field_id': 'household.member_count', 'question': '外出务工成员有收入吗？',
         'reason': '人数已提到但收入归属未说明', 'recording_id': 'rec_1', 'segment_id': 'seg_1'},
        {'field_id': 'crop.main.area', 'question': '亩产大概多少斤？',
         'reason': '面积有但产量未提', 'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'field_id': 'crop.main.area', 'question': '柑橘销往哪里？',
         'reason': '第4条有效建议，应被上限拦截', 'recording_id': 'rec_1', 'segment_id': 'seg_2'}]}


def test_agent_suggestions_validated_stored_and_hidden_after_adopt(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(reply=agent_reply_with_suggestions())
    result = env.extraction.run(case)
    assert [s['field_id'] for s in result['suggestions']] ==         ['crop.main.area', 'household.member_count', 'crop.main.area']
    rejected = {item['reason'] for item in result['suggestions_rejected']}
    assert {'unknown_field', 'forbidden_suggestion_wording',
            'segment_not_in_this_case_input', 'suggestion_cap_exceeded'} <= rejected
    view = env.extraction.candidates(case)
    assert len(view['suggestions']) == 3
    suggestion = view['suggestions'][0]
    assert suggestion['suggestion_id'].startswith('sug_') and len(suggestion['suggestion_id']) == 24
    # adopted -> accepted ai card -> answerable question on the board
    adopted = env.extraction.adopt_suggestion(case, suggestion['suggestion_id'])
    assert adopted['adopted'] is True and not adopted['already_adopted']
    board = env.answers.board(case)
    followup = [q for q in board['questions'] if q['field_id'].startswith('followup.')]
    assert len(followup) == 1 and followup[0]['source'] == 'ai'
    assert followup[0]['question_text'] == suggestion['question']
    # adopting twice is idempotent; the pending view hides adopted suggestions
    again = env.extraction.adopt_suggestion(case, suggestion['suggestion_id'])
    assert again['already_adopted'] is True
    assert len(env.extraction.candidates(case)['suggestions']) == 2
    # unknown and malformed ids are refused
    raises('suggestion_not_found', env.extraction.adopt_suggestion, case, 'sug_' + '0' * 20)
    raises('invalid_suggestion_id', env.extraction.adopt_suggestion, case, 'nope')


def test_cloud_reply_without_suggestions_is_not_an_error(env):
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(reply=good_reply())
    result = env.extraction.run(case)
    assert result['suggestions'] == [] and result['suggestions_rejected'] == []


def test_hermes_adapter_policy_and_wire_format(monkeypatch):
    import due_app.extraction as extraction

    def config(**overrides):
        values = {'base_url': '', 'model': '', 'api_key': '',
                  'agent_base_url': 'http://127.0.0.1:8082', 'agent_model': 'due-answer-matcher'}
        values.update(overrides)
        return values

    monkeypatch.setattr(extraction, '_config', lambda: config())
    adapter = extraction.HermesExtractionAdapter()
    assert adapter.model == 'due-answer-matcher'

    captured = {}

    class FakeResponse:
        @staticmethod
        def read(limit):
            return extraction.encoded({'choices': [{'message': {'content':
                '```json\n{"candidates":[],"suggestions":[]}\n```'}}]}).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeOpener:
        @staticmethod
        def open(request, timeout=None):
            captured['url'] = request.full_url
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return FakeResponse()

    adapter.opener = FakeOpener()
    catalog_rows = [{'field_id': 'a', 'question_text': 'q', 'unit': None, 'value_type': 'text'}]
    segment_rows = [{'recording_id': 'r', 'segment_id': 's', 'revision': 1, 'text': 't'}]
    reply = adapter.extract(catalog_rows, segment_rows)
    assert reply == {'candidates': [], 'suggestions': []}
    assert captured['url'] == 'http://127.0.0.1:8082/v1/chat/completions'
    assert captured['body']['model'] == 'due-answer-matcher' and captured['body']['stream'] is False
    assert captured['body']['messages'][0]['content'] == extraction.encoded(
        {'catalog': catalog_rows, 'segments': segment_rows})

    # non-loopback bases are refused; a misconfigured agent falls back to cloud
    monkeypatch.setattr(extraction, '_config', lambda: config(agent_base_url='http://192.168.1.5:8082'))
    with pytest.raises(ValueError):
        extraction.HermesExtractionAdapter()
    monkeypatch.setattr(extraction, '_config', lambda: config(agent_base_url='http://evil.example'))
    monkeypatch.setattr(extraction.ApiExtractionAdapter, '__init__', lambda self, timeout=60: None)
    assert isinstance(extraction._default_adapter(), extraction.ApiExtractionAdapter)
    monkeypatch.setattr(extraction, '_config', lambda: config())
    assert isinstance(extraction._default_adapter(), extraction.HermesExtractionAdapter)


def test_candidates_view_shows_latest_run_only(env):
    """Older runs' candidates (evidence possibly superseded by re-transcription)
    must not sit beside the latest run's values on the board."""
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    env.extraction.adapter = FakeAdapter(reply=good_reply())
    env.extraction.run(case)
    assert len(env.extraction.candidates(case)['candidates']) == 3
    changed = [dict(segment, text=segment['text'] + '呀') for segment in SEGMENTS]  # re-transcribed text
    env.extraction = with_segments(env, case, changed)
    env.extraction.adapter = FakeAdapter(reply={'candidates': [
        {'field_id': 'household.member_count', 'value_kind': 'number', 'value': 4, 'unit': '人',
         'period': None, 'subject': None, 'quote': '我们家一共五口人。呀', 'recording_id': 'rec_1',
         'segment_id': 'seg_1', 'segment_revision': 2, 'reason': '重新识别后的原话'}]})
    result = env.extraction.run(case)
    assert result['candidates_accepted'] == 1
    view = env.extraction.candidates(case)
    assert [c['value'] for c in view['candidates']] == [4]
    assert view['latest_run']['run_id'] == result['run_id']


def test_suggestions_require_evidence_and_one_question_of_a_kind(env):
    """A suggestion may only clarify collected evidence: fields without a
    candidate keep their unanswered marker on the board (no duplicate
    question), and a field owning an adopted followup is closed for more."""
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    only_member = [good_reply()['candidates'][0]]  # household.member_count only
    env.extraction.adapter = FakeAdapter(reply={'candidates': only_member, 'suggestions': [
        {'field_id': 'crop.main.area', 'question': '主营种了多少亩？', 'reason': '原话没提',
         'recording_id': 'rec_1', 'segment_id': 'seg_2'},  # no candidate -> duplicates the board question
        {'field_id': 'household.member_count', 'question': '五口人里有外出务工的吗？',
         'reason': '人数构成未说明', 'recording_id': 'rec_1', 'segment_id': 'seg_1'}]})
    result = env.extraction.run(case)
    assert [s['field_id'] for s in result['suggestions']] == ['household.member_count']
    assert 'field_without_evidence_question_already_on_board' in {r['reason'] for r in result['suggestions_rejected']}
    # adopt it, then a new run on a changed transcript may not re-suggest the same field
    env.extraction.adopt_suggestion(case, result['suggestions'][0]['suggestion_id'])
    changed = [dict(segment, text=segment['text'] + '哈') for segment in SEGMENTS]
    env.extraction = with_segments(env, case, changed)
    env.extraction.adapter = FakeAdapter(reply={'candidates': only_member, 'suggestions': [
        {'field_id': 'household.member_count', 'question': '五口人的构成再说明一下？', 'reason': '补充口径',
         'recording_id': 'rec_1', 'segment_id': 'seg_1'}]})
    result2 = env.extraction.run(case)
    assert result2['suggestions'] == []
    assert 'field_already_has_adopted_followup' in {r['reason'] for r in result2['suggestions_rejected']}


def test_risk_signals_mined_off_questionnaire(env):
    """Off-questionnaire risk leads: verbatim-evidence bound, clarifying-only
    wording, capped; adopting one lands it on the board like any AI followup."""
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    reply = good_reply()
    reply['risk_signals'] = [
        {'topic': '坏引用', 'question': '引用不存在的语句？', 'reason': 'bad segment',
         'quote': 'x', 'recording_id': 'rec_1', 'segment_id': 'seg_404'},
        {'topic': '违规', 'question': '把资料上传给我看看？', 'reason': 'forbidden wording',
         'quote': '没有欠银行的钱', 'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'topic': '卖车', 'question': '去年把车卖了是因为什么？',
         'reason': '变卖资产可能与短期资金安排有关，了解原因有助于判断资金状况',
         'quote': '没有欠银行的钱', 'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'topic': '担保', 'question': '帮邻居担保的那笔现在情况如何？',
         'reason': '对外担保可能构成或有负债，需要澄清金额与状态',
         'quote': '没有欠银行的钱', 'recording_id': 'rec_1', 'segment_id': 'seg_2'},
        {'topic': '赌博暗示', 'question': '平时买的彩票多吗？',
         'reason': '了解日常开销构成', 'quote': '我们家一共五口人',
         'recording_id': 'rec_1', 'segment_id': 'seg_1'},
        {'topic': '超上限', 'question': '第四条有效线索应该被上限拦截？',
         'reason': 'cap check', 'quote': '种了十二亩柑橘', 'recording_id': 'rec_1', 'segment_id': 'seg_2'}]
    env.extraction.adapter = FakeAdapter(reply=reply)
    result = env.extraction.run(case)
    assert len(result['risk_signals']) == 3
    assert result['risk_signals'][0]['suggestion_id'].startswith('risk_')
    rejected = {item['reason'] for item in result['risk_signals_rejected']}
    assert {'risk_signal_cap_exceeded', 'segment_not_in_this_case_input',
            'forbidden_risk_wording'} <= rejected
    # risk leads are NOT gated by catalog evidence — the quote is the evidence
    view = env.extraction.candidates(case)
    assert len(view['risk_signals']) == 3 and len(view['suggestions']) == 0
    # adopt one: it becomes an answerable AI question in the 补问 section
    adopted = env.extraction.adopt_suggestion(case, result['risk_signals'][0]['suggestion_id'])
    assert adopted['adopted'] is True and not adopted['already_adopted']
    board = env.answers.board(case)
    followup = [q for q in board['questions'] if q['field_id'].startswith('followup.risk.')]
    assert len(followup) == 1 and followup[0]['source'] == 'ai' and followup[0]['section'] == '补问'
    assert followup[0]['question_text'] == '去年把车卖了是因为什么？'
    # adopted risk lead leaves the pending view; malformed ids are refused
    assert len(env.extraction.candidates(case)['risk_signals']) == 2
    raises('invalid_suggestion_id', env.extraction.adopt_suggestion, case, 'risk_short')
    raises('suggestion_not_found', env.extraction.adopt_suggestion, case, 'risk_' + '0' * 20)


def test_bulk_adopt_takes_singles_skips_answered_and_conflicts(env):
    """One-click adoption only takes single-candidate unanswered fields; it
    never picks a value among conflicting candidates."""
    case = make_case(env)
    env.extraction = with_segments(env, case, SEGMENTS)
    reply = good_reply()  # member_count / crop.main.area / debt.flag, one each
    reply['candidates'].append({  # a second, conflicting reading on debt.flag
        'field_id': 'debt.flag', 'value_kind': 'boolean', 'value': True, 'unit': None,
        'period': None, 'subject': None, 'quote': '今年种了十二亩柑橘', 'recording_id': 'rec_1',
        'segment_id': 'seg_2', 'segment_revision': 1, 'reason': '冲突读数'})
    env.extraction.adapter = FakeAdapter(reply=reply)
    env.extraction.run(case)
    # pre-answer one field so bulk must skip it
    env.answers.create_answer(case, {'field_id': 'household.member_count', 'status': 'confirmed',
                                     'actor': 'tester', 'value': {'number': 5, 'unit': '人'},
                                     'basis': [{'type': 'manual', 'id': 'n'}]})
    result = env.answers.adopt_candidates_bulk(case, env.extraction.candidates(case)['candidates'])
    assert result['adopted_count'] == 1 and result['adopted_fields'] == ['crop.main.area']
    assert result['skipped_answered'] == ['household.member_count']
    assert result['conflict_fields'] == ['debt.flag']
    # the adopted answer carries the segment basis; conflict stays unadopted
    progress = env.answers.progress(case)
    assert progress['required']['done'] >= 2
    # a second bulk pass finds nothing new to adopt
    again = env.answers.adopt_candidates_bulk(case, env.extraction.candidates(case)['candidates'])
    assert again['adopted_count'] == 0
