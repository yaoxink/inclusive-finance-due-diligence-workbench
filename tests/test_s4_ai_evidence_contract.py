"""Prompt evidence must satisfy the same source policy as returned AI citations."""
from test_s3_http_review import env, case, load, statement
from due_app.followups import FollowupService


def test_questionnaire_gaps_are_not_offered_as_ai_citation_evidence(env):
    app, client = env
    cid = case(client)
    load(client, cid)
    service = FollowupService(app.state.store, app.state.diligence)
    workspace = app.state.diligence.workspace(cid)
    source = service._ai_source(workspace)

    assert source['sources']
    assert all(s['kind'] != 'questionnaire_gap' for s in source['sources'])
    assert not any(s['id'].startswith('questionnaire:') for s in source['sources'])
    # Missing fields still have ordinary rule reminders; they were not deleted.
    assert any(c['field_id'] == 'operator.local_registration' for c in service._rules(workspace))


def test_real_statement_evidence_keeps_its_exact_identity_and_revision(env):
    app, client = env
    cid = case(client)
    load(client, cid)
    original = statement(client, cid, '合成口述：今年医保三个人，是否包括在校孩子尚待核对。')
    service = FollowupService(app.state.store, app.state.diligence)
    workspace = app.state.diligence.workspace(cid)
    source = service._ai_source(workspace)
    cited = [s for s in source['sources'] if s['id'] == original['id']]
    assert len(cited) == 1
    assert cited[0]['revision'] == original['revision']
    assert cited[0]['text_excerpt'] == original['text']
    assert not any(s['kind'] == 'questionnaire_gap' for s in source['sources'])


def test_gap_citations_still_fail_closed_at_validation(env):
    app, client = env
    cid = case(client)
    load(client, cid)
    service = FollowupService(app.state.store, app.state.diligence)
    workspace = app.state.diligence.workspace(cid)
    cards, rejected = service._validate_ai(workspace, {'suggestions': [{
        'field_id': 'operator.local_registration',
        'question': '请说明是否本地户籍？',
        'reason': '合成输出引用了问卷缺口，不是已记载的事实依据。',
        'source_ids': ['questionnaire:operator.local_registration'],
    }]})
    assert cards == []
    assert rejected == 1
