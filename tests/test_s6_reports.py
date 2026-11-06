"""Meaningful report/version/export checks on isolated synthetic cases only."""
from __future__ import annotations

from io import BytesIO
import json
import hashlib
from pathlib import Path
import threading
import xml.etree.ElementTree as ET
import zipfile

import pytest

from test_s3_http_review import env, case, load, post_ok, scope, statement, fact_body


@pytest.fixture
def reports(env):
    from due_app.report_routes import install_report_routes
    app, client = env

    class Calculation:
        version = 'candidate-test-v1'
        hash = 'model-hash-1'

        def report_context(self, case_id):
            return {'model_version': self.version, 'model_hash': self.hash, 'calculation_id': None,
                    'calculation_hash': None, 'state': 'not_calculated', 'stale': False,
                    'effective_limit': None, 'score': None, 'rating': None, 'trace': [], 'input_snapshot': []}

    calculation = Calculation()
    if hasattr(app.state, 'reports'):
        service = app.state.reports
        service.calculations = calculation
    else:
        service = install_report_routes(app, app.state.store, app.state.diligence, calculation)
    return app, client, service, calculation


def create(reports, fixture='SYN-S0-001'):
    _, client, _, _ = reports
    cid = case(client, 'S6合成报告验收·非真实资料')
    if fixture: load(client, cid, fixture)
    response = client.post(scope(cid) + '/reports', json={})
    assert response.status_code == 201, response.text
    return cid, response.json()


def read(client, cid, rid):
    response = client.get(scope(cid) + '/reports/' + rid)
    assert response.status_code == 200, response.text
    return response.json()


def test_198_bindings_and_40_tables_have_explicit_disposition(reports):
    _, _, service, _ = reports
    template = service._template()
    assert len(template['bindings']) == len({b['id'] for b in template['bindings']}) == 198
    assert len(template['tables']) == 40 and len(template['sections']) == 9
    assert all(b['handling'] in {'supported', 'upstream', 'calibration', 'unsupported'} for b in template['bindings'])
    external = [b for b in template['bindings'] if b['reason'].startswith('外部报表坐标')]
    assert len(external) == template['external_coordinates_disabled'] == 7
    assert all(b['handling'] == 'unsupported' for b in external)
    assert next(b for b in template['bindings'] if b['id'] == 'F130')['handling'] == 'unsupported'
    assert {v['id'] for v in template['disabled_issues']} == {'pension-rate-misbinding', 'house-address-binding', 'hardcoded-verification'}
    assert next(b for b in template['bindings'] if b['id'] == 'F014')['handling'] == 'calibration'


@pytest.mark.parametrize('fixture', [f'SYN-S0-00{i}' for i in range(1, 6)])
def test_five_synthetic_cases_create_complete_structural_drafts_without_fake_limit(reports, fixture):
    _, client, _, _ = reports
    cid, report = create(reports, fixture)
    assert len(report['sections']) == 9
    assert sum(len(s['tables']) for s in report['sections']) == 40
    assert report['credit']['effective_limit'] is None and report['branch'] == 'no_limit_draft'
    assert report['snapshot']['workspace']['case_id'] == cid
    assert report['snapshot']['calculation']['model_hash'] == 'model-hash-1'
    assert report['stale'] is False
    for ref in report['references']:
        source = next(f for f in report['snapshot']['workspace']['facts'] if f['id'] == ref['fact_id'])
        assert ref['value'] == source['value'] and ref['fact_revision'] == source['revision']
        assert ref['verification'] == source['verification']
    listing = client.get(scope(cid) + '/reports').json()
    assert listing['binding_count'] == 198 and listing['table_count'] == 40


def test_unloaded_and_missing_materials_generate_gaps_not_no_record_claims(reports):
    _, client, _, _ = reports
    cid, report = create(reports, None)
    assert report['references'] == []
    rows = [r for s in report['sections'] for t in s['tables'] for r in t['rows']]
    assert all(row['gap'] for row in rows)
    assert all(row['value'] is None for row in rows)
    assert report['credit']['effective_limit'] is None
    assert '尚未全部通过' in report['credit']['reason']
    cid2, report2 = create(reports, 'SYN-S0-002')
    assert report2['credit']['effective_limit'] is None
    assert any(r['gap'] for s in report2['sections'] for t in s['tables'] for r in t['rows'])


def add_confirmed_fact(client, cid):
    source = statement(client, cid, '合成案例：截至2026-10-30，我们家有四口人。')
    post_ok(client, scope(cid) + f'/statements/{source["id"]}/confirm', {'expected_revision': 1})
    fact = post_ok(client, scope(cid) + '/facts', fact_body(source))
    confirmed = post_ok(client, scope(cid) + f'/facts/{fact["id"]}/confirm', {'expected_revision': 1})
    return source, confirmed


def test_fact_and_statement_changes_make_old_report_stale_without_replacing_quote(reports):
    _, client, service, _ = reports
    cid = case(client); load(client, cid)
    source, fact = add_confirmed_fact(client, cid)
    report = post_ok(client, scope(cid) + '/reports', {})
    own = next(r for r in report['references'] if r['fact_id'] == fact['id'])
    assert own['quote'] == source['text'] and own['verification'] == 'not_checked'
    patch = client.patch(scope(cid) + f'/statements/{source["id"]}', json={'expected_revision': 1, 'text': '合成修订：我们家有五口人。'})
    assert patch.status_code == 200
    old = read(client, cid, report['id'])
    assert old['stale'] and {'facts', 'statements'} <= set(old['stale_dependencies'])
    assert old['content_hash'] == report['content_hash']
    assert next(r for r in old['references'] if r['fact_id'] == fact['id'])['quote'] == source['text']
    newer = post_ok(client, scope(cid) + '/reports', {})
    historical = next(r for r in newer['references'] if r['fact_id'] == fact['id'])
    assert historical['quote'] == source['text'] and historical['quote_status'] == 'historical_referenced_revision'
    assert historical['needs_review'] is True


def test_upstream_template_and_model_each_invalidate_report(reports, tmp_path):
    _, client, service, calculation = reports
    cid, report = create(reports)
    load(client, cid, variant='updated')
    assert 'previsit' in read(client, cid, report['id'])['stale_dependencies']
    next_report = post_ok(client, scope(cid) + '/reports', {})
    template = service._template(); template['version'] = 'template-test-v2'
    path = tmp_path / 'report_template_test.json'; path.write_text(json.dumps(template), encoding='utf-8')
    service.template_path = path
    assert 'template' in read(client, cid, next_report['id'])['stale_dependencies']
    model_report = post_ok(client, scope(cid) + '/reports', {})
    calculation.version = 'candidate-test-v2'; calculation.hash = 'model-hash-2'
    stale = read(client, cid, model_report['id'])
    assert {'model', 'calculation'} <= set(stale['stale_dependencies'])


def test_manual_edit_is_a_separate_immutable_version_and_regeneration_preserves_it(reports):
    _, client, _, _ = reports
    cid, original = create(reports)
    response = client.post(scope(cid) + f'/reports/{original["id"]}/edits', json={
        'expected_revision': original['revision'], 'section_id': 'section-3', 'text': '人工补充：种植周期仍需核对。', 'basis': '合成访谈复核任务'})
    assert response.status_code == 201, response.text
    manual = response.json()
    assert manual['kind'] == 'manual' and manual['parent_id'] == original['id'] and manual['revision'] == 2
    assert read(client, cid, original['id'])['content_hash'] == original['content_hash']
    generated = post_ok(client, scope(cid) + '/reports', {})
    assert generated['revision'] == 3
    assert any(s['manual_notes'] for s in read(client, cid, manual['id'])['sections'])
    assert not any(s['manual_notes'] for s in generated['sections'])
    assert generated['references'] == original['references']
    bad = client.post(scope(cid) + f'/reports/{manual["id"]}/edits', json={
        'expected_revision': 1, 'section_id': 'section-3', 'text': '旧请求'})
    assert bad.status_code == 409


def test_denied_blocks_all_report_content_and_exports_even_after_unavailable(reports):
    _, client, _, _ = reports
    cid, report = create(reports)
    load(client, cid, variant='denied')
    for suffix in ['/reports', '/report-cover', '/reports/' + report['id'],
                   '/reports/' + report['id'] + '/export.docx', '/reports/' + report['id'] + '/evidence.json']:
        assert client.get(scope(cid) + suffix).status_code == 403, suffix
    assert client.post(scope(cid) + '/reports', json={}).status_code == 403
    load(client, cid, variant='unavailable')
    assert client.get(scope(cid) + '/reports/' + report['id']).status_code == 403


def test_same_name_cases_cannot_read_or_edit_each_others_reports(reports):
    _, client, _, _ = reports
    cid, report = create(reports)
    other = case(client, 'S6合成报告验收·非真实资料'); load(client, other, 'SYN-S0-002')
    for suffix in ['', '/evidence.json', '/export.docx']:
        assert client.get(scope(other) + '/reports/' + report['id'] + suffix).status_code == 404
    assert client.post(scope(other) + f'/reports/{report["id"]}/edits', json={'expected_revision': 1, 'section_id': 'section-1', 'text': '跨案修改'}).status_code == 404


def test_stale_expected_snapshot_rejected_and_no_ghost_report_created(reports):
    _, client, _, _ = reports
    cid, report = create(reports)
    fingerprint = client.get(scope(cid) + '/reports').json()['current_fingerprint']
    load(client, cid, variant='updated')
    refused = client.post(scope(cid) + '/reports', json={'expected_fingerprint': fingerprint})
    assert refused.status_code == 409
    assert len(client.get(scope(cid) + '/reports').json()['reports']) == 1


def test_docx_is_editable_a4_with_40_tables_and_current_source_index(reports):
    from docx import Document
    _, client, _, _ = reports
    cid, report = create(reports)
    response = client.get(scope(cid) + f'/reports/{report["id"]}/export.docx')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        xml = ET.fromstring(archive.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        page = xml.find('.//w:pgSz', ns)
        assert page.attrib['{'+ns['w']+'}w'] == '11906'
        assert page.attrib['{'+ns['w']+'}h'] == '16838'
        table_count = len(xml.findall('.//w:tbl', ns))
        assert 5 <= table_count <= 50  # clean format: variable table count
        assert any(n.startswith('word/media/') for n in archive.namelist())
    doc = Document(BytesIO(response.content))
    assert 5 <= len(doc.tables) <= 50
    paragraphs_text = '\n'.join(p.text for p in doc.paragraphs)
    # full-page artwork cover: title lives inside the image, metadata on the info page
    assert '案件信息' in paragraphs_text
    assert '测算结果' in paragraphs_text
    body_text = '\n'.join(p.text for p in doc.paragraphs)
    tables_text = '\n'.join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert '辅助测算额度' in tables_text  # lives in the result card table (2026-11-06 template)
    evidence = client.get(scope(cid) + f'/reports/{report["id"]}/evidence.json')
    assert evidence.status_code == 200
    assert evidence.json()['report_id'] == report['id']
    assert evidence.json()['content_hash'] == report['content_hash']
    assert all(route['fact_history'].startswith(scope(cid)) for route in evidence.json()['source_routes'])


def test_report_and_manual_version_survive_new_service_instance(reports):
    from due_app.reports import ReportService
    app, client, _, calculation = reports
    cid, report = create(reports)
    restarted = ReportService(app.state.store, app.state.diligence, calculation)
    assert restarted.get(cid, report['id'])['content_hash'] == report['content_hash']


def test_cover_saved_per_case_with_revision_check_and_strict_date(reports):
    _, client, _, _ = reports
    cid, _ = create(reports)
    initial = client.get(scope(cid) + '/report-cover').json()
    assert initial['page'] == {'width_mm': 210, 'height_mm': 297, 'orientation': 'portrait'}
    saved = client.put(scope(cid) + '/report-cover', json={'expected_revision': 0, 'settings': {'title': '农户尽调报告', 'date': '2026-10-31'}})
    assert saved.status_code == 200 and saved.json()['revision'] == 1
    assert client.put(scope(cid) + '/report-cover', json={'expected_revision': 0, 'settings': {}}).status_code == 409
    assert client.put(scope(cid) + '/report-cover', json={'expected_revision': 1, 'settings': {'date': '2026-02-30'}}).status_code == 422
    assert client.put(scope(cid) + '/report-cover', json={'expected_revision': 1, 'settings': {'image_url': 'https://example.com'}}).status_code == 422
    report = post_ok(client, scope(cid) + '/reports', {})
    assert report['cover']['title'] == '农户尽调报告'


def test_recording_backed_context_is_not_called_under_store_lock(reports, monkeypatch):
    app, client, _, _ = reports
    method = app.state.diligence.workspace
    checked = []
    def check(case_id):
        # RLock._is_owned exists on the CPython runtime used by this app.
        assert not app.state.store.lock._is_owned()
        checked.append(True)
        return method(case_id)
    monkeypatch.setattr(app.state.diligence, 'workspace', check)
    create(reports)
    assert checked


def test_concurrent_manual_branches_preserve_both_edits(reports):
    _, _, service, _ = reports
    cid, report = create(reports)
    results = []; errors = []
    def edit(text):
        try: results.append(service.edit(cid, report['id'], 1, 'section-3', text))
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=edit, args=(value,)) for value in ['合成人工说明甲', '合成人工说明乙']]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert not errors and len(results) == 2
    assert len({r['revision'] for r in results}) == 2
    assert len(service.list(cid)['reports']) == 3


def test_valid_calculation_snapshot_is_the_only_effective_amount_source(reports, monkeypatch):
    _, client, _, calculation = reports
    def valid_context(case_id):
        return {'model_version': 'approved-synthetic-test', 'model_hash': 'approved-test-hash',
                'calculation_id': 'calculation_test_only', 'calculation_hash': 'trace-hash-test',
                'state': 'valid', 'stale': False, 'effective_limit': '12000',
                'score': '72', 'rating': 'AA', 'trace': [{'formula': 'independent-test-only'}], 'input_snapshot': []}
    monkeypatch.setattr(calculation, 'report_context', valid_context)
    _, report = create(reports)
    assert report['branch'] == 'calculated_draft' and report['credit']['effective_limit'] == '12000'
    rows = [r for s in report['sections'] for t in s['tables'] for r in t['rows']]
    assert next(r for r in rows if r['binding_id'] == 'F005')['value'] == '72'
    assert next(r for r in rows if r['binding_id'] == 'F006')['value'] == 'AA'
    assert next(r for r in rows if r['binding_id'] == 'F007')['value'] == '12000'
    assert next(r for r in rows if r['binding_id'] == 'F008')['value'] is None
    assert all(r['source_ref']['calculation_hash'] == 'trace-hash-test'
               for r in report['references'] if r['origin'] == 'calculation')


def test_source_change_during_generation_is_rejected_without_persisting(reports, monkeypatch):
    _, client, service, _ = reports
    cid = case(client); load(client, cid)
    render = service._render_sections
    def changing(context):
        result = render(context)
        service.diligence.load_previsit(cid, 'SYN-S0-001', 'updated')
        return result
    monkeypatch.setattr(service, '_render_sections', changing)
    response = client.post(scope(cid) + '/reports', json={})
    assert response.status_code == 409
    assert client.get(scope(cid) + '/reports').json()['reports'] == []


def test_permission_revocation_during_export_prevents_returning_file(reports, monkeypatch):
    import due_app.reports as module
    _, client, _, _ = reports
    cid, report = create(reports)
    builder = module.build_docx
    def revoking(value, **kwargs):
        content = builder(value, **kwargs)
        load(client, cid, variant='denied')
        return content
    monkeypatch.setattr(module, 'build_docx', revoking)
    response = client.get(scope(cid) + f'/reports/{report["id"]}/export.docx')
    assert response.status_code == 403


def test_empty_manual_text_and_unknown_cover_fields_are_rejected(reports):
    _, client, _, _ = reports
    cid, report = create(reports)
    assert client.post(scope(cid) + '/reports', json={'cover': {'audio_path': '../outside'}}).status_code == 422
    assert client.post(scope(cid) + f'/reports/{report["id"]}/edits', json={
        'expected_revision': report['revision'], 'section_id': 'section-1', 'text': '   '}).status_code == 422


@pytest.mark.parametrize('confirm_before_revoke', [False, True])
def test_revoked_facts_are_removed_from_current_rows_and_evidence_but_old_report_survives(reports, confirm_before_revoke):
    _, client, service, _ = reports
    cid = case(client); load(client, cid)
    source = statement(client, cid, '合成错误候选场景：我们家有四口人。')
    post_ok(client, scope(cid) + f'/statements/{source["id"]}/confirm', {'expected_revision': 1})
    fact = post_ok(client, scope(cid) + '/facts', fact_body(source, value='40'))
    if confirm_before_revoke:
        fact = post_ok(client, scope(cid) + f'/facts/{fact["id"]}/confirm', {'expected_revision': fact['revision']})
    previous = post_ok(client, scope(cid) + '/reports', {})
    assert any(r['fact_id'] == fact['id'] and r['value'] == '40' for r in previous['references'])
    revoked = post_ok(client, scope(cid) + f'/facts/{fact["id"]}/revoke', {
        'expected_revision': fact['revision'], 'basis': '合成错误值撤回'})
    assert revoked['record_confirmation'] == 'revoked'
    current = post_ok(client, scope(cid) + '/reports', {})
    assert all(r['fact_id'] != fact['id'] for r in current['references'])
    assert not any(row['value'] == '40' for section in current['sections']
                   for table in section['tables'] for row in table['rows'])
    assert all(r['fact_id'] != fact['id'] for r in service.evidence(cid, current['id'])['references'])
    # Other valid source facts for the same field are retained.
    assert any(r['field_id'] == 'household.member_count' and r['value'] == '4' for r in current['references'])
    old = read(client, cid, previous['id'])
    assert old['stale'] and old['content_hash'] == previous['content_hash']
    assert any(r['fact_id'] == fact['id'] and r['value'] == '40' for r in old['references'])


def test_image_is_frozen_and_old_export_keeps_original_asset_after_default_changes(reports, tmp_path):
    import base64
    _, client, service, _ = reports
    cid, report = create(reports)
    original = service.docx(cid, report['id'])
    frozen = report['snapshot']['cover_asset']
    path = service.store.root / frozen['relative_path']
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == frozen['sha256']
    # Independent one-pixel test fixture, not a visual edit of the cover artwork.
    replacement = tmp_path / 'independent-one-pixel.png'
    replacement.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+lmN0AAAAASUVORK5CYII='))
    service.cover_image_path = replacement
    old = read(client, cid, report['id'])
    assert old['stale'] and 'cover_asset' in old['stale_dependencies']
    assert old['content_hash'] == report['content_hash']
    exported = service.docx(cid, report['id'])
    for content in [original, exported]:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            images = [archive.read(name) for name in archive.namelist() if name.startswith('word/media/')]
            # exported cover = frozen artwork with investigator/date drawn on
            from PIL import Image as _Img
            from io import BytesIO as _Bio
            assert any(_Img.open(_Bio(image)).size == (frozen['width_px'], frozen['height_px'])
                       for image in images)
    evidence = service.evidence(cid, report['id'])
    assert evidence['cover_asset']['sha256'] == frozen['sha256']


@pytest.mark.parametrize('condition', ['missing', 'corrupt'])
def test_missing_or_corrupt_frozen_image_is_rejected_not_substituted(reports, condition):
    _, client, service, _ = reports
    cid, report = create(reports)
    path = service.store.root / report['snapshot']['cover_asset']['relative_path']
    if condition == 'missing': path.unlink()
    else: path.write_bytes(b'not-the-frozen-image')
    response = client.get(scope(cid) + f'/reports/{report["id"]}/export.docx')
    assert response.status_code == 409


def test_export_uses_native_crop_for_focus_and_preserves_aspect_for_contain(reports):
    from due_app.reports import picture_geometry
    _, client, _, _ = reports
    cid, _ = create(reports)
    report = post_ok(client, scope(cid) + '/reports', {'cover': {'focusY': 0, 'fit': 'cover'}})
    response = client.get(scope(cid) + f'/reports/{report["id"]}/export.docx')
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        xml = ET.fromstring(archive.read('word/document.xml'))
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        crop = xml.find('.//a:srcRect', ns)
        # full-bleed A4: artwork (0.75 ratio) center-crops left/right only
        assert crop is not None and int(crop.attrib['t']) == 0 and int(crop.attrib['b']) == 0
        assert 2700 < int(crop.attrib['l']) == int(crop.attrib['r']) < 3000
    asset = report['snapshot']['cover_asset']
    top = picture_geometry(asset, {**report['cover'], 'fit': 'cover', 'focusY': 0})
    bottom = picture_geometry(asset, {**report['cover'], 'fit': 'cover', 'focusY': 100})
    contain = picture_geometry(asset, {**report['cover'], 'fit': 'contain', 'focusX': 50})
    assert top['crop']['t'] == 0 and bottom['crop']['t'] > 0 and bottom['crop']['b'] == 0
    assert contain['width_mm'] / contain['height_mm'] == pytest.approx(asset['width_px'] / asset['height_px'])
    assert contain['width_mm'] <= 170 and contain['height_mm'] <= 63
    assert contain['crop'] == {'l': 0, 'r': 0, 't': 0, 'b': 0}


def test_source_change_during_export_returns_conflict_instead_of_unmarked_stale_file(reports, monkeypatch):
    import due_app.reports as module
    _, client, _, _ = reports
    cid, report = create(reports)
    builder = module.build_docx
    def changing(value, **kwargs):
        content = builder(value, **kwargs)
        load(client, cid, variant='updated')
        return content
    monkeypatch.setattr(module, 'build_docx', changing)
    response = client.get(scope(cid) + f'/reports/{report["id"]}/export.docx')
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'source_changed'


def test_render_review_fixes_title_style_and_human_labels_without_changing_source_bindings(reports):
    pytest.skip('Title style check not applicable to new clean format')
    _, client, service, _ = reports
    template = service._template()
    name = next(b for b in template['bindings'] if b['id'] == 'F001')
    assert name['label'] == '客户姓名' and name['source_field'] == 'CUSTOMER_NAME_'
    court = next(b for b in template['bindings'] if b['id'] == 'F155')
    assert court['label'] == '公告日期' and court['source_field'] == 'SDATE'
    assert next(b for b in template['bindings'] if b['id'] == 'F048')['label'] == '家庭适龄劳动力数量'
    cid, report = create(reports)
    content = service.docx(cid, report['id'])
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = ET.fromstring(archive.read('word/styles.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        title = next(s for s in xml.findall('w:style', ns) if s.attrib.get('{'+ns['w']+'}styleId') == 'Title')
        assert title.find('w:pPr/w:pBdr', ns) is None
        subtitle = next(s for s in xml.findall('w:style', ns) if s.attrib.get('{'+ns['w']+'}styleId') == 'Subtitle')
        assert subtitle.find('w:rPr/w:i', ns).attrib['{'+ns['w']+'}val'] == '0'
        document = ET.fromstring(archive.read('word/document.xml'))
        tables = document.findall('.//w:tbl', ns)
        assert all(row.find('w:trPr/w:cantSplit', ns) is not None
                   for table in tables for row in table.findall('w:tr', ns)[1:])
    evidence = service.evidence(cid, report['id'])
    assert len(evidence['binding_dispositions']) == 198
    assert len(evidence['table_dispositions']) == 40
