"""S4-A01's explicit-unknown gap: synthetic manual records, no capture/model."""
import pytest

from test_s3_http_review import env, case, load, scope, statement, fact_body, post_ok
from due_app.followups import FollowupService


class NeverModel:
    calls = 0

    def generate(self, source):
        self.calls += 1
        raise AssertionError('This deterministic rule check must not invoke a model.')


@pytest.mark.parametrize('presence,value,expected_missing', [
    ('unknown', None, True),
    ('missing', None, True),
    ('explicit_zero', '0', False),
])
def test_record_accuracy_cannot_turn_unknown_into_answered(env, presence, value, expected_missing):
    app, client = env
    cid = case(client, 'S4-A01 explicit completeness synthetic check')
    load(client, cid, 'SYN-S0-002')
    field_id = 'land.contracted_area'  # No upstream value for this field.
    source = statement(client, cid, '纯虚构：承包地面积' + (
        '明确为0亩。' if presence == 'explicit_zero' else '尚未明确，需后续核对。'))
    post_ok(client, scope(cid) + '/statements/' + source['id'] + '/confirm',
            {'expected_revision': source['revision']})
    fact = post_ok(client, scope(cid) + '/facts', fact_body(
        source, field_id=field_id, value_type='decimal', value=value,
        presence=presence, unit='亩', subject_id='C2-HOUSEHOLD'))
    post_ok(client, scope(cid) + '/facts/' + fact['id'] + '/confirm',
            {'expected_revision': fact['revision'], 'basis': '仅确认虚构记录完整性状态记载准确。'})

    adapter = NeverModel()
    service = FollowupService(app.state.store, app.state.diligence, adapter=adapter)
    result = service.analyze(cid)
    current = [card for card in result['cards']
               if card['field_id'] == field_id and not card['stale']]
    if expected_missing:
        assert len(current) == 1 and current[0]['rule'] == 'missing'
        assert current[0]['sources'][0]['kind'] == 'fact'
        assert current[0]['sources'][0]['id'] == fact['id']
        assert current[0]['sources'][0]['presence'] == presence
        assert current[0]['sources'][0]['value'] is None
    else:
        assert current == []
    saved = app.state.diligence.fact(cid, fact['id'])
    assert saved['record_confirmation'] == 'record_accurate'
    assert saved['presence'] == presence and saved['value'] == value
    assert saved['verification'] == 'not_checked'
    assert adapter.calls == 0 and result['ai']['state'] == 'disabled'
    assert app.state.recordings.active() is None
    assert app.state.recordings.asr_busy_count() == 0
