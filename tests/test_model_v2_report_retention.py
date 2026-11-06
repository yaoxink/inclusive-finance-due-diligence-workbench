"""Cross-domain v2 boundaries on disposable synthetic workspaces."""
import json
import copy

import pytest

from test_s3_http_review import env, case, load, scope
from test_s6_reports import reports, create


def add_ledger_row(app, cid, table):
    with app.state.store.lock, app.state.store.connect() as connection:
        if table == 's5v2_inputs':
            connection.execute('''CREATE TABLE IF NOT EXISTS s5v2_inputs (
                case_id TEXT NOT NULL REFERENCES cases(id), revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(case_id, revision))''')
            connection.execute('INSERT INTO s5v2_inputs VALUES (?,?,?,?,?)',
                               (cid, 1, '2026-11-01T00:00:00+00:00', 'test-source-token', '{}'))
        else:
            connection.execute('''CREATE TABLE IF NOT EXISTS s5v2_runs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                created_at TEXT NOT NULL, input_revision INTEGER NOT NULL,
                business_hash TEXT NOT NULL, payload_json TEXT NOT NULL)''')
            connection.execute('INSERT INTO s5v2_runs VALUES (?,?,?,?,?,?)',
                               ('v2-synthetic-boundary-' + cid, cid,
                                '2026-11-01T00:00:00+00:00', 1, 'test-snapshot-hash', '{}'))


@pytest.mark.parametrize('table', ['s5v2_inputs', 's5v2_runs'])
def test_ledger_change_during_report_render_cannot_commit(reports, monkeypatch, table):
    app, client, service, _ = reports
    cid = case(client)
    load(client, cid)
    original = service._render_sections

    def render_then_change(context):
        rendered = original(context)
        add_ledger_row(app, cid, table)
        return rendered

    monkeypatch.setattr(service, '_render_sections', render_then_change)
    response = client.post(scope(cid) + '/reports', json={})
    assert response.status_code == 409, response.text
    with app.state.store.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM s6_reports WHERE case_id=?', (cid,)).fetchone()[0] == 0


def test_v2_rows_are_included_in_erasure_without_affecting_another_case(env):
    app, client = env
    cid = case(client, 'v2 disposable synthetic deletion')
    other = case(client, 'v2 retained synthetic case')
    for table in ['s5v2_inputs', 's5v2_runs']:
        add_ledger_row(app, cid, table)
        add_ledger_row(app, other, table)
    preview = client.get(scope(cid) + '/deletion-preview')
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value['counts']['s5v2_inputs'] == value['counts']['s5v2_runs'] == 1
    response = client.post(scope(cid) + '/delete', json={
        'expected_fingerprint': value['fingerprint'], 'confirm_title': 'v2 disposable synthetic deletion'})
    assert response.status_code == 200, response.text
    assert app.state.store.case(cid) is None
    with app.state.store.connect() as connection:
        for table in ['s5v2_inputs', 's5v2_runs']:
            assert connection.execute(f'SELECT COUNT(*) FROM {table} WHERE case_id=?', (cid,)).fetchone()[0] == 0
            assert connection.execute(f'SELECT COUNT(*) FROM {table} WHERE case_id=?', (other,)).fetchone()[0] == 1


def test_calculated_report_has_matching_declaration_and_export(reports, monkeypatch):
    _, client, _, calculation = reports
    monkeypatch.setattr(calculation, 'report_context', lambda cid: {
        'model_version': 'farmer-assessment-v2-synthetic-test', 'model_hash': 'test-model-v2',
        'calculation_id': 'test-calculation-v2', 'calculation_hash': 'test-calculation-hash',
        'state': 'valid', 'stale': False, 'effective_limit': '18600', 'score': '72', 'rating': 'AA',
        'trace': [], 'input_snapshot': [], 'subject_id': 'HOUSEHOLD-SYNTHETIC',
        'accounting_period': {'kind': 'year', 'label': '2025', 'start': '2025-01-01', 'end': '2025-12-31'}})
    cid, report = create(reports)
    assert report['branch'] == 'calculated_draft'
    declaration = next(s['text'] for s in report['sections'] if s['id'] == 'section-9')
    assert '辅助模型试算' in declaration and '当前无有效评分' not in declaration
    references = [r for r in report['references'] if r['origin'] == 'calculation']
    assert len(references) == 3
    assert all(r['subject_id'] == 'HOUSEHOLD-SYNTHETIC' and r['period']['end'] == '2025-12-31' for r in references)
    exported = client.get(scope(cid) + '/reports/' + report['id'] + '/export.docx')
    assert exported.status_code == 200
    from io import BytesIO
    from zipfile import ZipFile
    with ZipFile(BytesIO(exported.content)) as archive:
        xml = archive.read('word/document.xml').decode('utf-8')
    assert '18600' in xml and '辅助模型试算' in xml
    assert '当前无有效评分或授信额度' not in xml


def test_actual_v2_report_projects_inputs_with_their_own_period_and_source(env):
    app, client = env
    cid = case(client, 'v2 report input projection synthetic')
    load(client, cid)
    base = scope(cid) + '/calculation/v2'
    inputs = client.get(base + '/examples').json()['examples'][0]['inputs']
    response = client.put(base + '/inputs', json={'expected_revision': 0, 'inputs': inputs})
    assert response.status_code == 200, response.text
    run = client.post(base + '/run', json={'expected_revision': 1})
    assert run.status_code == 201 and run.json()['state'] == 'valid', run.text
    response = client.post(scope(cid) + '/reports', json={})
    assert response.status_code == 201, response.text
    report = response.json()
    appendix = report['calculation_appendix']
    assert len(appendix['fields']) == len(inputs['fields'])
    assert appendix['scope']['accounting_period'] == {'start': '2025-01-01', 'end': '2025-12-31'}
    assert len(appendix['indicators']) == 36
    assert all('会计年度' in step['label'] for step in appendix['indicators'] if step['step'] in {'SCORE-01', 'SCORE-06'})
    rows = [row for section in report['sections'] for table in section['tables'] for row in table['rows']]
    def row(binding):
        return next(item for item in rows if item['binding_id'] == binding)
    assert row('F008')['value'] == '未配置' and row('F008')['gap'] is False
    assert row('F127')['value'] == '否' and row('F127')['gap'] is False
    assert row('F004')['gap'] and row('F038')['gap']  # No unsafe semantic substitution.
    assert '会计年度' in row('F018')['label'] and row('F018')['period']['end'] == '2025-12-31'
    assert '个人' in row('F034')['label'] and '个人' in row('F040')['label']
    assert row('F009')['subject_id'] == inputs['fields']['operator.age']['subject_id']
    assert row('F009')['period'] == inputs['fields']['operator.age']['period']
    assert row('F020')['value'] == '本次未选为主营规模评分项' and not row('F020')['gap']
    assert row('F019')['unit'] == '亩'
    assert row('F019')['period'] == inputs['fields']['crop.main.area']['period']
    assert row('F019')['subject_id'] == inputs['fields']['crop.main.area']['subject_id']
    assert row('F032')['period'] == inputs['fields']['insurance.policy_covered_area']['period']
    assert row('F033')['period'] == inputs['fields']['insurance.policy_continuous_years']['period']
    t2 = next(table for section in report['sections'] for table in section['tables'] if table['id'] == 'T2')
    assert len(t2['rows']) == 9 and all(not item['gap'] for item in t2['rows'])
    original = next(f for f in report['snapshot']['workspace']['facts'] if f['field_id'] == 'crop.main.yield')
    model = next(f for f in appendix['fields'] if f['field_id'] == 'crop.main.yield')
    assert original['value'] == '4500' and model['value'] == '2000'
    assert original['period'] != model['period']
    from due_app.reports import content_digest
    changed = copy.deepcopy(report)
    changed['calculation_appendix']['fields'][0]['value'] = 'changed'
    assert content_digest(changed) != report['content_hash']
    exported = client.get(scope(cid) + '/reports/' + report['id'] + '/export.docx')
    assert exported.status_code == 200
    from io import BytesIO
    from zipfile import ZipFile
    with ZipFile(BytesIO(exported.content)) as archive:
        xml = archive.read('word/document.xml').decode('utf-8')
    assert '本次测算输入快照' not in xml and '报告分栏明细' not in xml  # appendices removed (2026-11-06 template)
    assert '首版未配置风险定价' in xml
    assert '事实 None' not in xml
    assert '评级阈值及端点口径待校准' not in xml
    assert '首版未配置风险定价' in xml
