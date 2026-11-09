"""P2 acceptance: customer info, answers with multi-entry details, optimistic
concurrency, case scoping and progress calculation on fixed samples.

Isolated synthetic storage only; no devices, models or the live database."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared-recording-core"))

from due_app.answers import AnswerService, calculate_progress
from due_app.diligence import DiligenceError, DiligenceService
from due_app.store import DueStore


class NullRecordings:
    active = staticmethod(lambda: None)
    asr_busy_count = staticmethod(lambda: 0)


@pytest.fixture
def env(tmp_path):
    store = DueStore(tmp_path / 'p2-isolated')
    diligence = DiligenceService(store, NullRecordings())
    return SimpleNamespace(store=store, diligence=diligence,
                           answers=AnswerService(store, diligence))


def make_case(env, title='P2样例'):
    return env.store.create_case(title, 'farmer', 'farmer_demo')['id']


def raises(env, code, fn, *args, **kwargs):
    with pytest.raises(DiligenceError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, f'expect {code}, got {exc.value.code}: {exc.value.message}'
    return exc.value


# ---------- customer info ----------

def test_customer_info_create_revise_conflict_and_history(env):
    case = make_case(env)
    first = env.answers.update_customer_info(case, {
        'fields': {'customer_name': '张三', 'id_type': '居民身份证', 'id_number': 'TEST-ID-001',
                   'phone': 'TEST-PHONE-A'},
        'expected_revision': 0, 'actor': 'manager_a'})
    assert first['revision'] == 1 and first['fields']['customer_name'] == '张三'
    # stale revision must not overwrite
    raises(env, 'revision_conflict', env.answers.update_customer_info, case, {
        'fields': {'customer_name': '李四', 'id_type': '居民身份证', 'id_number': 'TEST-ID-001'},
        'expected_revision': 0, 'actor': 'manager_b'})
    second = env.answers.update_customer_info(case, {
        'fields': {'customer_name': '张三丰', 'id_type': '居民身份证', 'id_number': 'TEST-ID-001',
                   'phone': 'TEST-PHONE-B'},
        'expected_revision': 1, 'actor': 'manager_b'})
    assert second['revision'] == 2 and second['changed_keys'] == ['customer_name', 'phone']
    view = env.answers.customer_info(case)
    assert view['revision'] == 2 and len(view['history']) == 2
    assert view['customer_info']['fields']['customer_name'] == '张三丰'
    # required fields cannot be dropped silently
    raises(env, 'missing_customer_field', env.answers.update_customer_info, case, {
        'fields': {'customer_name': '', 'id_type': '居民身份证', 'id_number': 'TEST-ID-001'},
        'expected_revision': 2})
    # unknown field rejected
    raises(env, 'invalid_customer_fields', env.answers.update_customer_info, case, {
        'fields': {'customer_name': '王五', 'id_type': '居民身份证', 'id_number': 'TEST-ID-001', 'secret': 'x'},
        'expected_revision': 2})
    # cross-case read rejected
    raises(env, 'case_not_found', env.answers.customer_info, 'case_does_not_exist')


# ---------- answers and multi-entry details ----------

def test_two_debts_independent_revise_revoke_reopen(env):
    case = make_case(env)
    created = env.answers.create_answer(case, {
        'field_id': 'debt.bank_contracts', 'status': 'confirmed',
        'items': [
            {'item_id': 'loan-1', 'members': {'creditor_kind': '银行贷款（不含房贷车贷）', 'creditor': 'A银行',
                                              'original_amount': 50000, 'outstanding_balance': 30000}},
            {'item_id': 'loan-2', 'members': {'creditor_kind': '银行贷款（不含房贷车贷）', 'creditor': 'B银行',
                                              'original_amount': 80000, 'outstanding_balance': 80000}}],
        'basis': [{'type': 'manual', 'id': 'manager_note', 'note': '客户口述两笔'}], 'actor': 'manager_a'})
    assert created['revision'] == 1 and len(created['items']) == 2
    # revise only the second loan's balance via full-answer revision (optimistic)
    items = [dict(i) for i in created['items']]
    items[1]['members']['outstanding_balance'] = 61000
    revised = env.answers.revise_answer(case, created['answer_id'], {
        'expected_revision': 1, 'items': items, 'status': 'confirmed', 'actor': 'manager_a'})
    assert revised['revision'] == 2
    # concurrent stale write must fail
    raises(env, 'revision_conflict', env.answers.revise_answer, case, created['answer_id'], {
        'expected_revision': 1, 'items': items, 'status': 'confirmed', 'actor': 'manager_b'})
    # revoke one entry with a reason, independently
    revoked = env.answers.revoke_item(case, created['answer_id'], 'loan-2', {
        'expected_revision': 2, 'reason': '客户结清并提供回单线索，待核验', 'actor': 'manager_b'})
    assert revoked['revision'] == 3
    statuses = {i['item_id']: i['status'] for i in revoked['items']}
    assert statuses == {'loan-1': 'active', 'loan-2': 'revoked'}
    # revoke needs a reason
    raises(env, 'missing_reason', env.answers.revoke_item, case, created['answer_id'], 'loan-1',
           {'expected_revision': 3})
    # only revoked entries can reopen
    raises(env, 'item_not_revoked', env.answers.reopen_item, case, created['answer_id'], 'loan-1',
           {'expected_revision': 3})
    reopened = env.answers.reopen_item(case, created['answer_id'], 'loan-2', {
        'expected_revision': 3, 'reason': '回单核验不通过，恢复该笔', 'actor': 'manager_b'})
    assert all(i['status'] == 'active' for i in reopened['items'])
    history = env.answers.answer_history(case, created['answer_id'])
    assert [r['revision'] for r in history['revisions']] == [1, 2, 3, 4]
    assert all(r['actor'] and r['updated_at'] for r in history['revisions'])
    # duplicate answer for same field is refused
    raises(env, 'answer_exists', env.answers.create_answer, case, {
        'field_id': 'debt.bank_contracts', 'status': 'draft', 'value': {'text': 'x'}})


def test_not_applicable_needs_reason_and_no_value(env):
    case = make_case(env)
    raises(env, 'missing_not_applicable_reason', env.answers.create_answer, case, {
        'field_id': 'household.member_count', 'status': 'not_applicable'})
    raises(env, 'not_applicable_with_value', env.answers.create_answer, case, {
        'field_id': 'household.member_count', 'status': 'not_applicable',
        'not_applicable_reason': '不适用样例', 'value': {'number': 3}})
    marked = env.answers.create_answer(case, {
        'field_id': 'household.member_count', 'status': 'not_applicable',
        'not_applicable_reason': '样例：该农户为单人户口径待定，先标记不适用',
        'basis': [{'type': 'manual', 'id': 'note-1'}]})
    assert marked['status'] == 'not_applicable' and marked['value'] is None
    # a blank draft is not zero and not confirmed
    draft = env.answers.create_answer(case, {'field_id': 'cost.seed', 'status': 'draft',
                                             'value': {'text': '约三千元（草稿待核）'}})
    assert draft['status'] == 'draft'
    # unknown fields and cross-case answers are rejected
    raises(env, 'unknown_field', env.answers.create_answer, case, {
        'field_id': 'no.such_field', 'status': 'draft'})
    other = make_case(env, '另一案')
    raises(env, 'answer_not_found', env.answers.answer, other, draft['answer_id'])
    raises(env, 'answer_not_found', env.answers.answer_history, other, draft['answer_id'])


def test_items_structural_validation(env):
    case = make_case(env)
    raises(env, 'not_a_list_field', env.answers.create_answer, case, {
        'field_id': 'household.member_count', 'status': 'confirmed',
        'items': [{'item_id': 'x', 'members': {}}], 'basis': [{'type': 'manual', 'id': 'n'}]})
    raises(env, 'invalid_item_keys', env.answers.create_answer, case, {
        'field_id': 'land.transfers', 'status': 'confirmed',
        'items': [{'item_id': 'p-1', 'members': {'area': 3.2, 'surprise': 1}}],
        'basis': [{'type': 'manual', 'id': 'n'}]})
    ok = env.answers.create_answer(case, {
        'field_id': 'land.transfers', 'status': 'confirmed',
        'items': [{'item_id': 'p-1', 'members': {'tenure_type': '流转', 'location': '四川蒲江', 'area': 3.2, 'annual_fee': 1800}}],
        'basis': [{'type': 'manual', 'id': 'n', 'note': '合同旁听'}]})
    assert ok['items'][0]['members']['area'] == 3.2
    # scalar value and items cannot mix (use a different list field)
    raises(env, 'value_and_items', env.answers.create_answer, case, {
        'field_id': 'insurance.policies', 'status': 'confirmed',
        'value': {'number': 3}, 'items': [{'item_id': 'ins-1', 'members': {'insurer': '中国平安财产保险'}}],
        'basis': [{'type': 'manual', 'id': 'n'}]})


# ---------- progress: fixed samples ----------

def sample_catalog():
    def field(fid, klass, *, conditions=None):
        return {'field_id': fid, 'required_class': klass, 'conditions': conditions}
    return {'version': 'sample-v1', 'fields': [
        field('r1', 'required_candidate'),
        field('r2', 'required_candidate'),
        field('c1', 'conditionally_required_candidate',
              conditions={'state': 'unconfirmed'}),
        field('c2', 'conditionally_required_candidate',
              conditions={'state': 'confirmed',
                          'confirmed_expression': {'field_id': 'flag', 'operator': 'equals', 'value': True}}),
        field('c3', 'conditionally_required_candidate',
              conditions={'state': 'confirmed',
                          'confirmed_expression': {'field_id': 'flag', 'operator': 'equals', 'value': False}}),
        field('c4', 'conditionally_required_candidate',
              conditions={'state': 'confirmed',
                          'confirmed_expression': {'field_id': 'flag', 'operator': 'equals', 'value': True}}),
        field('flag', 'required_candidate'),
        field('o1', 'optional_candidate'),
        field('o2', 'optional_candidate'),
        field('p1', 'pending_confirmation'),
    ]}


def answer(field_id, status, value=None, **extra):
    return {'field_id': field_id, 'status': status, 'value': value, 'items': None, **extra}


def test_progress_fixed_samples():
    catalog = sample_catalog()
    # empty case: zero completions, conditions pending, nothing fabricated
    empty = calculate_progress(catalog, [])
    assert empty['required'] == {'done': 0, 'applicable': 3}   # r1 r2 flag
    assert empty['optional'] == {'done': 0, 'total': 2}
    assert empty['pending_judgement'] == 5                     # c1 c2 c3 c4 unjudged + p1 whole group
    assert empty['not_applicable'] == 0
    assert set(empty['pending_fields']) == {'c1', 'c2', 'c3', 'c4', 'p1'}
    # r1 confirmed, r2 not applicable (with reason carried on answer), flag=true confirmed
    partial = calculate_progress(catalog, [
        answer('r1', 'confirmed', {'number': 4}),
        answer('r2', 'not_applicable', not_applicable_reason='样例不适用'),
        answer('flag', 'confirmed', {'boolean': True}),
    ])
    # flag=true -> c2 c4 applicable (denominator grows), c3 condition-inapplicable (out), c1 still pending
    assert partial['required'] == {'done': 2, 'applicable': 4}  # r1 + flag confirmed; r2 NA left the denominator
    assert partial['not_applicable'] == 1
    assert partial['condition_inapplicable'] == 1
    assert set(partial['pending_fields']) == {'c1', 'p1'}
    # flag=false -> c3 applicable only; complete the rest
    full = calculate_progress(catalog, [
        answer('r1', 'confirmed', {'number': 4}),
        answer('r2', 'not_applicable', not_applicable_reason='x'),
        answer('flag', 'confirmed', {'boolean': False}),
        answer('c3', 'confirmed', {'text': '条件必问已答'}),
        answer('o1', 'confirmed', {'text': '选答'}),
    ])
    assert full['required'] == {'done': 3, 'applicable': 3}    # r1 flag c3 confirmed; r2 NA left the denominator
    assert full['optional'] == {'done': 1, 'total': 2}
    assert full['not_applicable'] == 1
    assert full['not_applicable_fields'] == ['r2']
    # unknown trigger value keeps the conditional pending (never auto-drop)
    unknown = calculate_progress(catalog, [answer('flag', 'draft', {'boolean': True})])
    assert set(unknown['pending_fields']) == {'c1', 'c2', 'c3', 'c4', 'p1'}
    assert unknown['required']['done'] == 0


def test_progress_via_service_with_v2_catalog(env):
    case = make_case(env)
    progress = env.answers.progress(case)
    catalog = env.diligence.questionnaire
    required_count = sum(1 for f in catalog['fields'] if f.get('required'))
    assert progress['required']['applicable'] == required_count
    assert progress['required']['done'] == 0
    assert progress['catalog_version'] == catalog['version']
    listed = env.answers.answers(case)
    assert listed['answers'] == [] and listed['progress']['required']['done'] == 0


def test_v2_cases_untouched_and_readable(env):
    """New tables must not disturb the legacy s3 chain; a bound legacy case still reads."""
    case = make_case(env, '旧链路并存')
    env.diligence.load_previsit(case, 'SYN-S0-001', 'base')
    context = env.diligence.previsit_history(case)
    assert context, 'legacy previsit history must stay readable'
    # questionnaire snapshot frozen for the case and answers validate against it
    assert env.answers.progress(case)['catalog_version']  # snapshot or active, either readable
