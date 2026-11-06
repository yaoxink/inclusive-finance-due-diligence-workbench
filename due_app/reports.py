"""Versioned, source-bound due-diligence drafts. No model calls or credit decisions."""
from __future__ import annotations

import copy
from datetime import date
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import struct
import uuid

from .config import APP_ID, APP_ROOT
from .diligence import DiligenceError
from .store import now
from .report_model_v2 import build_appendix, binding_value, qualified_v2, source_text

DATA = Path(__file__).resolve().parent / 'data'
DEFAULT_TEMPLATE = DATA / 'report_template_v2.json'
COVER_IMAGE = APP_ROOT / 'due_app' / 'data' / 'cover-agricultural-v2.png'
COVER_VARIANT = 'agricultural-cover-v2'
PRESENCE = {'known': '已说明', 'explicit_zero': '明确为零', 'unknown': '未知', 'missing': '未提供',
            'not_applicable': '明确不适用', 'withheld': '明确不愿提供'}
VERIFICATION = {'not_checked': '尚未核验', 'checked_consistent': '合成示例核验一致',
                'checked_inconsistent': '核验不一致', 'pending': '核验待确认'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def content_digest(payload):
    content = {'cover': payload['cover'], 'cover_asset': payload['snapshot'].get('cover_asset'),
               'sections': payload['sections'], 'references': payload['references']}
    if 'calculation_appendix' in payload:
        content['calculation_appendix'] = payload['calculation_appendix']
    return digest(content)


def fail(status, code, message):
    raise DiligenceError(status, code, message)


def clean_text(value, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\r\n\t' for c in value):
        fail(422, 'invalid_report_text', '报告文字格式不符合要求。')
    if required and not value.strip():
        fail(422, 'invalid_report_text', '报告标题或人工说明不能为空。')
    return value


def cover_settings(value=None):
    defaults = {'title': '农户经营尽职调查报告', 'subtitle': '经营情况与访谈记录',
                'organization': '', 'author': '', 'date': '', 'imageMode': 'scene',
                'focusX': 50, 'focusY': 50, 'fit': 'cover'}
    if value is None:
        return defaults
    if not isinstance(value, dict) or set(value) - set(defaults):
        fail(422, 'invalid_cover', '仅接受本案封面文字与默认场景图设置。')
    result = {**defaults, **value}
    for key, maximum in [('title', 48), ('subtitle', 72), ('organization', 60), ('author', 40)]:
        clean_text(result[key], maximum, required=key == 'title')
    if not isinstance(result['date'], str):
        fail(422, 'invalid_cover', '封面日期格式无效。')
    if result['date']:
        try:
            date.fromisoformat(result['date'])
        except ValueError:
            fail(422, 'invalid_cover', '封面日期必须为有效日期。')
    if result['imageMode'] not in {'scene', 'none'} or result['fit'] not in {'cover', 'contain'}:
        fail(422, 'invalid_cover', '封面图片选项无效。')
    if any(type(result[key]) not in {int, float} or not 0 <= result[key] <= 100 for key in ['focusX', 'focusY']):
        fail(422, 'invalid_cover', '封面裁切位置须为0至100。')
    return result


class ReportService:
    def __init__(self, store, diligence, calculations=None, template_path=None):
        self.store, self.diligence, self.calculations = store, diligence, calculations
        self.template_path = Path(template_path or DEFAULT_TEMPLATE)
        self.cover_image_path = COVER_IMAGE
        with store.lock, store.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS s6_reports (
                id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),revision INTEGER NOT NULL,
                parent_id TEXT,kind TEXT NOT NULL,created_at TEXT NOT NULL,payload_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS s6_report_case ON s6_reports(case_id,revision);
              CREATE TABLE IF NOT EXISTS s6_cover_settings (
                case_id TEXT PRIMARY KEY REFERENCES cases(id),revision INTEGER NOT NULL,payload_json TEXT NOT NULL);
            ''')

    @staticmethod
    def _case(connection, case_id):
        row = connection.execute('SELECT * FROM cases WHERE id=? AND app_id=?', (case_id, APP_ID)).fetchone()
        if row is None:
            fail(404, 'case_not_found', '本尽调应用中不存在该案件。')
        if row['data_class'] != 'synthetic' or row['subject_type'] != 'farmer' or row['product_id'] != 'farmer_demo':
            fail(409, 'synthetic_only', '当前报告仅用于合成案件。')
        return dict(row)

    def _access(self, connection, case_id):
        case = self._case(connection, case_id)
        row = connection.execute('SELECT access_state FROM s3_bindings WHERE case_id=?', (case_id,)).fetchone()
        if row and row[0] == 'denied':
            fail(403, 'previsit_access_denied', '当前资料权限被拒绝，报告正文及导出不可旁路读取。')
        return case

    def _sql_token(self, connection, case_id):
        case = self._access(connection, case_id)
        pieces = {'case': {k: case[k] for k in ['id', 'title', 'subject_type', 'product_id']}}
        for table in ['s3_bindings', 's3_statements', 's3_facts', 's3_questionnaire_snapshots']:
            rows = connection.execute(f'SELECT * FROM {table} WHERE case_id=? ORDER BY rowid', (case_id,)).fetchall()
            pieces[table] = [dict(row) for row in rows]
        # The v2 ledger and its snapshots can change independently of S3 facts.
        # Include them in the final transaction token so late calculations cannot
        # be attached to a report generated from a different ledger revision.
        for table in ['s5v2_inputs', 's5v2_runs']:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                rows = connection.execute(f'SELECT * FROM {table} WHERE case_id=? ORDER BY rowid', (case_id,)).fetchall()
                pieces[table] = [dict(row) for row in rows]
        return digest(pieces)

    def _template(self):
        try:
            value = json.loads(self.template_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            fail(503, 'template_unavailable', '报告模板暂不可用，未生成替代正文。')
        if len(value.get('bindings', [])) != 198 or len(value.get('tables', [])) != 40:
            fail(503, 'invalid_template', '报告模板的绑定或表格范围不完整。')
        return value

    def _calculation(self, case_id):
        if self.calculations is not None:
            return copy.deepcopy(self.calculations.report_context(case_id))
        return {'model_version': None, 'model_hash': None, 'calculation_id': None,
                'calculation_hash': None, 'state': 'not_calculated', 'effective_limit': None,
                'score': None, 'rating': None, 'trace': [], 'input_snapshot': [], 'stale': False}

    def _asset(self):
        try:
            content = self.cover_image_path.read_bytes()
            if content[:8] != b'\x89PNG\r\n\x1a\n' or len(content) < 24:
                raise ValueError('invalid image')
            width, height = struct.unpack('>II', content[16:24])
            if not width or not height: raise ValueError('invalid dimensions')
            return {'state': 'available', 'variant': COVER_VARIANT,
                    'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                    'width_px': width, 'height_px': height}
        except (OSError, ValueError, struct.error):
            return {'state': 'unavailable', 'variant': COVER_VARIANT, 'sha256': None,
                    'bytes': None, 'width_px': None, 'height_px': None}

    def _asset_path(self, sha256):
        if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
            fail(409, 'invalid_report_asset', '报告配图标识无效，未读取替代文件。')
        data_root = self.store.root.resolve()
        directory = (data_root / 'exports' / 'report-assets').resolve()
        if data_root not in directory.parents:
            fail(409, 'invalid_report_asset', '报告配图目录超出本应用数据范围。')
        path = (directory / (sha256 + '.png')).resolve()
        if directory not in path.parents:
            fail(409, 'invalid_report_asset', '报告配图文件路径超出本应用数据范围。')
        return path

    def _freeze_asset(self, metadata):
        if metadata['state'] != 'available':
            fail(503, 'report_asset_unavailable', '默认农田配图暂不可用，请修复或选择无图封面。')
        path = self._asset_path(metadata['sha256'])
        try:
            content = self.cover_image_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != metadata['sha256']:
                fail(409, 'source_changed', '生成期间配图已变化，请重试。')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
                    fail(409, 'report_asset_corrupt', '既有冻结配图被修改，请保留文件并检查，不覆盖历史文件。')
            else:
                temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(content)
                    temporary.replace(path)
                finally:
                    if temporary.exists(): temporary.unlink()
        except OSError:
            fail(503, 'report_asset_storage_failed', '报告配图未能可靠保存，未生成初稿。')
        return {**metadata, 'relative_path': path.relative_to(self.store.root.resolve()).as_posix()}

    def context(self, case_id):
        # Never call a recording-backed workspace or calculation service under store.lock.
        for _ in range(3):
            with self.store.lock, self.store.connect() as connection:
                before = self._sql_token(connection, case_id)
            workspace = self.diligence.workspace(case_id)
            if workspace['previsit']['state'] == 'denied':
                fail(403, 'previsit_access_denied', '当前资料权限拒绝，无法读取报告来源。')
            template = self._template()
            calculation = self._calculation(case_id)
            asset = self._asset()
            with self.store.lock, self.store.connect() as connection:
                case = self._access(connection, case_id)
                after = self._sql_token(connection, case_id)
            if before != after:
                continue
            components = {
                'facts': digest(sorted(workspace['facts'], key=lambda f: f['id'])),
                'statements': digest(sorted(workspace['statements'], key=lambda s: s['id'])),
                'previsit': digest({k: workspace['previsit'].get(k) for k in
                                  ['state', 'snapshot_id', 'context_version', 'data_as_of', 'cached', 'materials']}),
                'questionnaire': digest({k: workspace['questionnaire'].get(k) for k in
                                       ['version', 'content_sha256', 'source_sha256']}),
                'template': digest(template),
                'model': digest({k: calculation.get(k) for k in ['model_version', 'model_hash']}),
                'calculation': digest(calculation),
                'case': digest({k: case[k] for k in ['id', 'title', 'subject_type', 'product_id']}),
                'cover_asset': digest(asset),
            }
            return {'case': case, 'workspace': workspace, 'template': template, 'calculation': calculation,
                    'cover_asset': asset, 'components': components, 'fingerprint': digest(components), 'sql_token': after}
        fail(409, 'source_changed', '来源正在变化，请待当前修订保存后重试。')

    def get_cover(self, case_id):
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            row = connection.execute('SELECT * FROM s6_cover_settings WHERE case_id=?', (case_id,)).fetchone()
            return {'case_id': case_id, 'revision': row['revision'] if row else 0,
                    'settings': json.loads(row['payload_json']) if row else cover_settings(),
                    'page': {'width_mm': 210, 'height_mm': 297, 'orientation': 'portrait'},
                    'image_note': '正式导出使用默认农田场景图；浏览器临时自选图片不上传。'}

    def save_cover(self, case_id, settings, expected_revision):
        settings = cover_settings(settings)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            row = connection.execute('SELECT revision FROM s6_cover_settings WHERE case_id=?', (case_id,)).fetchone()
            revision = row[0] if row else 0
            if type(expected_revision) is not int or expected_revision != revision:
                fail(409, 'revision_conflict', '封面设置已变化，请刷新后再保存。')
            connection.execute('INSERT OR REPLACE INTO s6_cover_settings VALUES (?,?,?)',
                               (case_id, revision + 1, encoded(settings)))
        return self.get_cover(case_id)

    def _render_sections(self, context):
        workspace, template = context['workspace'], context['template']
        bindings = {f['id']: f for f in template['bindings']}
        tables = {t['id']: t for t in template['tables']}
        fields = {f['field_id']: f['label'] for f in workspace['questionnaire']['fields']}
        references, sections = [], []
        for section in template['sections']:
            output = {'id': section['id'], 'title': section['title'], 'tables': [], 'manual_notes': []}
            for table_id in section['tables']:
                table = tables[table_id]
                rows = []
                for binding_id in table['binding_ids']:
                    binding = bindings[binding_id]
                    calculation = context['calculation']
                    calculation_key = {'F005': 'score', 'F006': 'rating', 'F007': 'effective_limit'}.get(binding_id)
                    if (calculation_key and calculation.get('state') == 'valid' and not calculation.get('stale')
                            and calculation.get('effective_limit') is not None
                            and calculation.get('calculation_id') and calculation.get('calculation_hash')
                            and calculation.get(calculation_key) is not None):
                        reference = self._calculation_reference(calculation, calculation_key)
                        references.append(reference)
                        rows.append({'binding_id': binding_id, 'field_id': calculation_key,
                                     'label': binding['label'], 'value': str(calculation[calculation_key]),
                                     'unit': '元' if calculation_key == 'effective_limit' else None,
                                     'period': calculation.get('accounting_period'), 'subject_id': calculation.get('subject_id'),
                                     'status': '辅助模型试算快照；仍需人员复核，未作审批决定',
                                     'origin': '确定性计算', 'reference_ids': [reference['id']], 'gap': False})
                        continue
                    projection = binding_value(binding_id, calculation) if binding['handling'] == 'calibration' else None
                    if projection is not None:
                        reference = self._model_reference(calculation, projection)
                        references.append(reference)
                        rows.append({'binding_id': binding_id, 'field_id': projection['field_id'],
                                     'label': projection.get('label') or binding['label'],
                                     'value': projection['value'], 'unit': projection.get('unit'),
                                     'period': projection.get('period'), 'subject_id': projection.get('subject_id'),
                                     'status': projection['status'], 'origin': '本次辅助测算快照',
                                     'reference_ids': [reference['id']], 'gap': False})
                        continue
                    if binding_id == 'F008' and qualified_v2(calculation):
                        rows.append({'binding_id': binding_id, 'field_id': 'pricing', 'label': binding['label'],
                                     'value': '未配置', 'unit': None, 'period': None, 'subject_id': None,
                                     'status': '首版未配置风险定价，不将缺省值写成0利率', 'origin': '模型配置',
                                     'reference_ids': [], 'gap': False})
                        continue
                    if binding['handling'] == 'supported':
                        # A revoked value remains in the frozen source snapshot for
                        # history, but is no longer a current report assertion or
                        # evidence reference. It must not be relabeled as pending.
                        facts = [f for f in workspace['facts'] if f['field_id'] == binding['canonical_field']
                                 and f.get('record_confirmation') != 'revoked']
                        for fact in facts:
                            reference = self._reference(fact, workspace)
                            references.append(reference)
                            flags = [PRESENCE.get(fact['presence'], '状态未知'),
                                     '记录准确已确认' if fact['record_confirmation'] == 'record_accurate' else '记录待确认',
                                     VERIFICATION.get(fact['verification'], '核验状态待确认')]
                            if fact.get('needs_review'): flags.append('来源变化待复核')
                            if fact.get('conflict'): flags.append('存在冲突')
                            value = fact['value'] if fact['presence'] in {'known', 'explicit_zero'} else PRESENCE.get(fact['presence'], '未知')
                            rows.append({'binding_id': binding_id, 'field_id': fact['field_id'],
                                         'label': fields.get(fact['field_id'], binding['label']), 'value': value,
                                         'unit': fact.get('unit'), 'period': fact.get('period'),
                                         'subject_id': fact.get('subject_id'), 'status': '；'.join(flags),
                                         'origin': '现场陈述' if fact['origin'] == 'onsite' else '事前材料记载',
                                         'reference_ids': [reference['id']], 'gap': False})
                        if facts:
                            continue
                    reason = binding['reason']
                    if binding['handling'] == 'supported': reason = '本次快照未提供该字段，保持待补充'
                    if binding['handling'] == 'calibration' and qualified_v2(calculation):
                        reason = '该原业务来源尚未绑定；本次模型输入和评分明细另见测算附录，不替换不同口径'
                    rows.append({'binding_id': binding_id, 'field_id': binding.get('canonical_field'),
                                 'label': fields.get(binding.get('canonical_field'), binding['label']),
                                 'value': None, 'unit': None, 'period': None, 'subject_id': None,
                                 'status': reason, 'origin': '待补充', 'reference_ids': [], 'gap': True})
                for slot in table['unbound_slots']:
                    rows.append({'binding_id': slot['id'], 'field_id': None, 'label': slot['label'],
                                 'value': None, 'unit': None, 'period': None, 'subject_id': None,
                                 'status': slot['reason'], 'origin': '待补充', 'reference_ids': [], 'gap': True})
                if not rows:
                    if table_id == 'T2':
                        calculation = context['calculation']
                        if qualified_v2(calculation):
                            for rating in calculation.get('ratings', []):
                                rows.append({'binding_id': None, 'field_id': None,
                                    'label': rating['rating'], 'value': rating['condition'], 'unit': None,
                                    'period': None, 'subject_id': None,
                                    'status': '本稿辅助模型评分区间；评级系数 ' + rating['coefficient'],
                                    'origin': '本稿冻结模型规则', 'reference_ids': [], 'gap': False})
                            detail = '本案辅助评级以冻结计算快照为准，不作银行审批评级。'
                        else:
                            detail = '评级阈值及端点口径待校准；未生效前不展示申请人评级。'
                    elif table_id == 'T40':
                        detail = '术语仅作栏目说明；农业职业经理人及龙头企业资格应以相应制度和证明为准，不从普通访谈推定。'
                    else:
                        detail = '本次未提供可用记录，不能据此认定无相关信息。'
                    if not rows:
                        rows.append({'binding_id': None, 'field_id': None, 'label': table['title'], 'value': None,
                                 'unit': None, 'period': None, 'subject_id': None, 'status': detail,
                                 'origin': '栏目说明', 'reference_ids': [], 'gap': True})
                output['tables'].append({'id': table_id, 'title': table['title'], 'rows': rows})
            if section['id'] == 'section-9':
                output['text'] = ('本报告为合成案件尽调初稿，记载来源于本次冻结的事前资料和现场记录。'
                                  '记录准确确认不等于事实核验；未获取资料不表示无记录。'
                                  '当前无有效评分或授信额度，不构成审批意见。实际业务用途及声明文本仍需业务人员复核。')
                calculation = context['calculation']
                if (calculation.get('state') == 'valid' and not calculation.get('stale')
                        and calculation.get('effective_limit') is not None
                        and calculation.get('calculation_id') and calculation.get('calculation_hash')):
                    output['text'] = ('本报告为合成案件尽调初稿，评分、评级及额度来自本稿冻结的辅助模型试算。'
                                      '计算依据、模型版本及输入核验记录随快照保留；记录准确确认不等于事实核验。'
                                      '模型试算不构成银行审批、放款或利率承诺，实际业务用途及报告仍需业务人员复核。')
            sections.append(output)
        unique = {r['id']: r for r in references}
        return sections, list(unique.values())

    @staticmethod
    def _model_reference(calculation, projection):
        field = projection['field_id']
        reference = ReportService._calculation_reference(calculation, 'effective_limit')
        reference.update(id='MODEL-' + digest({'id': calculation['calculation_id'],
            'hash': calculation['calculation_hash'], 'field': field})[:16], field_id=field,
            value=str(projection['value']), unit=projection.get('unit'),
            period=copy.deepcopy(projection.get('period')), subject_id=projection.get('subject_id'))
        reference['source_ref'].update(source_field=projection.get('source_field'),
                                       input_source=copy.deepcopy(projection.get('source_ref')))
        if not projection.get('derived'):
            verification = projection.get('verification') or {}
            reference.update(origin='model_input', quote_status='model_input_snapshot',
                presence=projection.get('presence', 'known'),
                record_confirmation=projection.get('record_confirmation', 'record_accurate'),
                verification=verification.get('state', 'checked_consistent'),
                verification_meta=copy.deepcopy(verification))
        return reference

    @staticmethod
    def _calculation_reference(calculation, field):
        return {'id': 'CALC-' + digest({'id': calculation['calculation_id'], 'hash': calculation['calculation_hash'], 'field': field})[:16],
                'fact_id': None, 'fact_revision': None, 'field_id': field,
                'origin': 'calculation', 'value': str(calculation[field]), 'presence': 'known',
                'unit': '元' if field == 'effective_limit' else None,
                'period': copy.deepcopy(calculation.get('accounting_period')), 'subject_id': calculation.get('subject_id'),
                'record_confirmation': 'not_applicable', 'verification': 'deterministic_calculation',
                'needs_review': False, 'conflict': False, 'statement_id': None, 'statement_revision': None,
                'quote': None, 'quote_status': 'deterministic_calculation',
                'source_ref': {'kind': 'calculation_snapshot', 'calculation_id': calculation['calculation_id'],
                               'calculation_hash': calculation['calculation_hash'],
                               'model_version': calculation.get('model_version'), 'model_hash': calculation.get('model_hash')}}

    @staticmethod
    def _reference(fact, workspace):
        statement = next((s for s in workspace['statements'] if s['id'] == fact.get('statement_id')
                          and s['revision'] == fact.get('statement_revision')), None)
        # The report snapshots always retain the fact's original source ref. A changed
        # current statement is never substituted for the referenced historical quote.
        return {'id': 'REF-' + digest({'fact': fact['id'], 'revision': fact['revision'],
                                     'snapshot': fact.get('source_ref')})[:16],
                'fact_id': fact['id'], 'fact_revision': fact['revision'], 'field_id': fact['field_id'],
                'origin': fact['origin'], 'value': fact['value'], 'presence': fact['presence'],
                'unit': fact.get('unit'), 'period': fact.get('period'), 'subject_id': fact.get('subject_id'),
                'record_confirmation': fact['record_confirmation'], 'verification': fact['verification'],
                'needs_review': fact.get('needs_review', False), 'conflict': fact.get('conflict', False),
                'statement_id': fact.get('statement_id'), 'statement_revision': fact.get('statement_revision'),
                'quote': statement['text'] if statement else None,
                'quote_status': 'current_referenced_revision' if statement else
                                'historical_source_requires_lookup' if fact.get('statement_id') else 'material_reference',
                'source_ref': copy.deepcopy(fact.get('source_ref')),
                'record_confirmation_meta': copy.deepcopy(fact.get('record_confirmation_meta')),
                'verification_meta': copy.deepcopy(fact.get('verification_meta'))}

    def _answer_values(self, case_id):
        """Latest onsite answers as display strings for the report body."""
        with self.store.connect() as connection:
            rows = connection.execute(
                'SELECT a.id, a.field_id, a.revision, a.payload_json FROM s2_answers a '
                'JOIN (SELECT id, MAX(revision) r FROM s2_answers WHERE case_id=? GROUP BY id) t '
                'ON a.id=t.id AND a.revision=t.r', (case_id,)).fetchall()
        values = {}
        for row in rows:
            payload = json.loads(row[3])
            if payload.get('status') == 'not_applicable':
                values[row[1]] = '不适用'
                continue
            value = payload.get('value') or {}
            unit = f" {value['unit']}" if value.get('unit') else ''
            if value.get('number') is not None:
                values[row[1]] = f"{value['number']:g}{unit}"
            elif value.get('boolean') is not None:
                values[row[1]] = '是' if value['boolean'] else '否'
            else:
                text = value.get('text') or value.get('choice') or value.get('date')
                values[row[1]] = f"{text}{unit}" if text else ''
        return values

    def _customer_info(self, case_id):
        with self.store.connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM s2_customer_info WHERE case_id=? '
                'ORDER BY revision DESC LIMIT 1', (case_id,)).fetchone()
        if row is None:
            return {}
        payload = json.loads(row[0])
        fields = payload.get('fields') or {}
        return {key: (value or '') for key, value in fields.items() if value}

    def generate(self, case_id, cover=None, expected_fingerprint=None):
        context = self.context(case_id)
        if expected_fingerprint is not None and context['fingerprint'] != expected_fingerprint:
            fail(409, 'source_changed', '案件来源已变化，请刷新后再生成。')
        settings = cover_settings(cover if cover is not None else self.get_cover(case_id)['settings'])
        sections, references = self._render_sections(context)
        # Resolve old quoted statements explicitly via their case-owned history.
        for reference in references:
            if reference['quote_status'] == 'historical_source_requires_lookup':
                history = self.diligence.statement_history(case_id, reference['statement_id'])
                original = next((s for s in history['versions'] if s['revision'] == reference['statement_revision']), None)
                if original:
                    reference['quote'], reference['quote_status'] = original['text'], 'historical_referenced_revision'
        after = self.context(case_id)
        if after['fingerprint'] != context['fingerprint']:
            fail(409, 'source_changed', '报告生成期间来源已变化，未保存过期初稿。')
        # File creation is deferred until the final case/version transaction.
        # Otherwise a deleted case's late generation can recreate an orphan
        # image after retention has already checked and removed its exports.
        frozen_asset = None
        payload = {'id': 'report_' + uuid.uuid4().hex, 'case_id': case_id,
                   'kind': 'generated', 'parent_id': None, 'created_at': now(),
                   'cover': settings, 'case_title': context['case']['title'],
                   'template_version': context['template']['version'],
                   'dependency_fingerprint': context['fingerprint'], 'dependency_components': context['components'],
                   'snapshot': {'workspace': context['workspace'], 'calculation': context['calculation'],
                                'answers': self._answer_values(case_id),
                                'customer_info': self._customer_info(case_id),
                                'template': context['template'], 'cover_asset': frozen_asset},
                   'sections': sections, 'references': references,
                   'credit': {'state': 'blocked', 'effective_limit': None, 'score': None, 'rating': None,
                              'reason': '当前有效规则及本案输入资格尚未全部通过，报告不输出有效额度。'},
                   'branch': 'no_limit_draft', 'content_hash': digest({'cover': settings, 'cover_asset': frozen_asset, 'sections': sections, 'references': references}),
                   'image_note': '配图为农田场景示意；DOCX使用默认图片，不导出浏览器临时自选图片。'}
        calculation = context['calculation']
        appendix = build_appendix(calculation)
        if appendix is not None:
            payload['calculation_appendix'] = appendix
        # Only a current, explicitly qualified snapshot may enter this branch.
        # A blocked or modified v2 ledger never falls back to an older result.
        if (calculation.get('state') == 'valid' and not calculation.get('stale')
                and calculation.get('effective_limit') is not None
                and calculation.get('calculation_hash') and calculation.get('calculation_id')):
            payload['credit'] = {k: calculation.get(k) for k in ['state', 'effective_limit', 'score', 'rating', 'calculation_id', 'calculation_hash']}
            payload['credit']['reason'] = '本结果为合成案件的辅助模型试算，仍需人员复核，不构成银行审批或放款承诺。'
            payload['branch'] = 'calculated_draft'
        with self.store.lock, self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if self._sql_token(connection, case_id) != context['sql_token']:
                fail(409, 'source_changed', '来源修订尚未稳定，请刷新后生成。')
            frozen_asset = self._freeze_asset(context['cover_asset']) if settings['imageMode'] == 'scene' else None
            payload['snapshot']['cover_asset'] = frozen_asset
            payload['content_hash'] = content_digest(payload)
            payload['revision'] = 1 + connection.execute('SELECT COALESCE(MAX(revision),0) FROM s6_reports WHERE case_id=?', (case_id,)).fetchone()[0]
            connection.execute('INSERT INTO s6_reports VALUES (?,?,?,?,?,?,?)',
                               (payload['id'], case_id, payload['revision'], None, 'generated', payload['created_at'], encoded(payload)))
        return self.get(case_id, payload['id'])

    @staticmethod
    def _view(payload, current):
        result = copy.deepcopy(payload)
        changes = [key for key, value in payload['dependency_components'].items() if current['components'].get(key) != value]
        if payload['cover']['imageMode'] == 'scene' and not payload['snapshot'].get('cover_asset'):
            changes.append('cover_asset')
        result.update(stale=bool(changes), stale_dependencies=changes,
                      status='needs_review' if changes else 'draft', current_fingerprint=current['fingerprint'])
        return result

    def list(self, case_id):
        current = self.context(case_id)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            rows = connection.execute('SELECT payload_json FROM s6_reports WHERE case_id=? ORDER BY revision DESC', (case_id,))
            items = []
            for row in rows:
                value = self._view(json.loads(row[0]), current)
                items.append({key: value[key] for key in ['id', 'case_id', 'revision', 'kind', 'parent_id', 'created_at',
                                                          'template_version', 'branch', 'stale', 'stale_dependencies', 'status', 'content_hash']})
        counts = {}
        for binding in current['template']['bindings']:
            counts[binding['handling']] = counts.get(binding['handling'], 0) + 1
        return {'case_id': case_id, 'reports': items, 'current_fingerprint': current['fingerprint'],
                'template_version': current['template']['version'], 'binding_coverage': counts,
                'binding_count': 198, 'table_count': 40, 'formal_business_acceptance': 'pending',
                'calculation': current['calculation'], 'page': {'width_mm': 210, 'height_mm': 297}}

    def get(self, case_id, report_id):
        current = self.context(case_id)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            row = connection.execute('SELECT payload_json FROM s6_reports WHERE id=? AND case_id=?', (report_id, case_id)).fetchone()
            if row is None:
                fail(404, 'report_not_found', '本案不存在该报告版本。')
            return self._view(json.loads(row[0]), current)

    def edit(self, case_id, report_id, expected_revision, section_id, text, basis=None):
        clean_text(text, 12000, required=True)
        if basis is not None: clean_text(basis, 2000)
        original = self.get(case_id, report_id)
        if type(expected_revision) is not int or expected_revision != original['revision']:
            fail(409, 'revision_conflict', '报告版本已变化，旧请求不会覆盖当前内容。')
        section = next((s for s in original['sections'] if s['id'] == section_id), None)
        if section is None:
            fail(422, 'invalid_report_section', '只能在现有报告章节添加人工说明。')
        payload = {k: copy.deepcopy(v) for k, v in original.items()
                   if k not in {'stale', 'stale_dependencies', 'status', 'current_fingerprint'}}
        payload.update(id='report_' + uuid.uuid4().hex, kind='manual', parent_id=report_id, created_at=now())
        section = next(s for s in payload['sections'] if s['id'] == section_id)
        section['manual_notes'].append({'text': text, 'basis': basis, 'origin': 'manual',
                                        'verification': 'not_checked', 'edited_at': payload['created_at']})
        payload['content_hash'] = content_digest(payload)
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
            # Immutable parent plus a fresh case-wide version; concurrent manual
            # edits preserve both branches rather than rewriting either parent.
            payload['revision'] = 1 + connection.execute('SELECT COALESCE(MAX(revision),0) FROM s6_reports WHERE case_id=?', (case_id,)).fetchone()[0]
            connection.execute('INSERT INTO s6_reports VALUES (?,?,?,?,?,?,?)',
                               (payload['id'], case_id, payload['revision'], report_id, 'manual', payload['created_at'], encoded(payload)))
        return self.get(case_id, payload['id'])

    def evidence(self, case_id, report_id):
        report = self.get(case_id, report_id)
        return {'schema': 'due-diligence-report-evidence/v1', 'case_id': case_id, 'report_id': report_id,
                'revision': report['revision'], 'content_hash': report['content_hash'],
                'dependency_fingerprint': report['dependency_fingerprint'],
                'dependency_components': report['dependency_components'], 'stale': report['stale'],
                'stale_dependencies': report['stale_dependencies'], 'references': report['references'],
                'calculation_snapshot': report['snapshot']['calculation'],
                'calculation_appendix': report.get('calculation_appendix'),
                'rendered_binding_dispositions': [{'binding_id': row['binding_id'], 'gap': row['gap'],
                    'origin': row['origin'], 'reference_ids': row['reference_ids']}
                    for section in report['sections'] for table in section['tables'] for row in table['rows']
                    if row.get('binding_id')],
                'cover_asset': report['snapshot'].get('cover_asset'),
                'binding_dispositions': report['snapshot']['template']['bindings'],
                'table_dispositions': report['snapshot']['template']['tables'],
                'disabled_binding_issues': report['snapshot']['template']['disabled_issues'],
                'manual_notes': [{'section_id': s['id'], **note} for s in report['sections'] for note in s['manual_notes']],
                'source_routes': [{'id': r['id'], 'fact_history': f'/api/due-diligence/cases/{case_id}/facts/{r["fact_id"]}/history' if r.get('fact_id') else None,
                                   'statement_history': f'/api/due-diligence/cases/{case_id}/statements/{r["statement_id"]}/history' if r.get('statement_id') else None}
                                  for r in report['references']]}

    def docx(self, case_id, report_id):
        report = self.get(case_id, report_id)
        asset_path = None
        if report['cover']['imageMode'] == 'scene':
            asset = report['snapshot'].get('cover_asset')
            if not asset:
                fail(409, 'report_asset_not_frozen', '旧版本尚未冻结配图，请生成新版本；未使用当前配图替换旧稿。')
            asset_path = self._asset_path(asset.get('sha256'))
            try:
                asset_bytes = asset_path.read_bytes()
                if hashlib.sha256(asset_bytes).hexdigest() != asset['sha256']:
                    fail(409, 'report_asset_corrupt', '历史报告配图校验失败，未返回被修改的封面。')
            except OSError:
                fail(409, 'report_asset_missing', '历史报告冻结配图缺失，请从本应用备份恢复。')
            # Use the verified bytes, not a second unverified path read by Word's
            # package builder after the hash check.
            asset_path = BytesIO(asset_bytes)
        result = build_docx(report, asset_path=asset_path)
        current = self.context(case_id)
        if current['fingerprint'] != report['current_fingerprint']:
            fail(409, 'source_changed', '导出期间来源已变化，请刷新版本状态后重试。')
        # Recheck access immediately before returning the export. Generation never
        # writes a public static file that could bypass later case permissions.
        with self.store.lock, self.store.connect() as connection:
            self._access(connection, case_id)
        return result


def render_cover_with_meta(asset_path, cover, case_title=''):
    """Draw investigator/date onto the artwork's blank band (below the printed
    title) and return a temporary PNG path. The frozen asset itself is never
    modified; this only affects the exported copy."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.open(asset_path).convert('RGB')
        width, height = image.size
        font_path = 'C:/Windows/Fonts/msyh.ttc'
        font = ImageFont.truetype(font_path, max(22, width // 34))
        investigator = (cover.get('author') or '').strip() or '待填写'
        date = (cover.get('date') or '').strip() or '待填写'
        draw = ImageDraw.Draw(image)
        center = width // 2
        # single line in the blank band (below the printed English subtitle)
        farmer = (case_title or '').split('-')[0].strip() or '待填写'
        text = f'农户名称：{farmer}    尽调人员：{investigator}    报告日期：{date}'
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((center - (box[2] - box[0]) // 2, int(height * 0.945) - (box[3] - box[1]) // 2),
                  text, fill=(74, 74, 74), font=font)
        import tempfile
        handle, path = tempfile.mkstemp(suffix='.png')
        import os
        os.close(handle)
        image.save(path, format='PNG')
        return path
    except Exception:
        return str(asset_path)


def full_page_geometry(asset, width_mm=210, height_mm=297):
    """Center-crop the cover artwork to the drawn box (no distortion)."""
    ratio = asset['width_px'] / asset['height_px']
    target = width_mm / height_mm
    if ratio > target:  # artwork wider -> crop left/right
        visible = target / ratio
        side = round((1 - visible) / 2 * 100000)
        return {'crop': {'l': side, 'r': side, 't': 0, 'b': 0}}
    visible = ratio / target  # artwork taller -> crop top/bottom
    side = round((1 - visible) / 2 * 100000)
    return {'crop': {'l': 0, 'r': 0, 't': side, 'b': side}}


def picture_geometry(asset, cover, width_mm=170, height_mm=63):
    """Native OOXML crop percentages match CSS object-fit/object-position."""
    ratio = asset['width_px'] / asset['height_px']
    target = width_mm / height_mm
    if cover['fit'] == 'contain':
        width, height = (width_mm, width_mm / ratio) if ratio >= target else (height_mm * ratio, height_mm)
        return {'width_mm': width, 'height_mm': height,
                'offset_x_mm': (width_mm - width) * cover['focusX'] / 100,
                'offset_y_mm': (height_mm - height) * cover['focusY'] / 100,
                'crop': {'l': 0, 'r': 0, 't': 0, 'b': 0}}
    visible_width, visible_height = (target / ratio, 1) if ratio > target else (1, ratio / target)
    left, top = (1 - visible_width) * cover['focusX'] / 100, (1 - visible_height) * cover['focusY'] / 100
    return {'width_mm': width_mm, 'height_mm': height_mm, 'offset_x_mm': 0, 'offset_y_mm': 0,
            'crop': {'l': round(left * 100000), 'r': round((1 - visible_width - left) * 100000),
                     't': round(top * 100000), 'b': round((1 - visible_height - top) * 100000)}}


"""New build_docx: clean report per approved template (农业尽调报告-排版美化版).
Structure: cover -> 案件信息 -> 测算结果 -> 一~五章 clean tables.
All tables are 2-col (项目|内容) except score summary (3-col) and indicators (3-col).
No source references, no period/subject lines, no verification status.
"""
from io import BytesIO
from pathlib import Path



"""New build_docx: clean report per approved template (农业尽调报告-排版美化版).
Structure: cover -> 案件信息 -> 测算结果 -> 一~五章 clean tables.
All tables are 2-col (项目|内容) except score summary (3-col) and indicators (3-col).
No source references, no period/subject lines, no verification status.
"""
from io import BytesIO
from pathlib import Path



def build_docx(report, asset_path=None):
    """Clean A4 report matching the approved template."""
    try:
        from docx import Document
        from docx.shared import Mm, Pt, RGBColor
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.section import WD_SECTION_START
    except ImportError:
        raise

    document = Document()

    # ---- helpers ----
    def set_font(run, size=None, bold=None):
        run.font.name = 'Microsoft YaHei'
        run.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        if size: run.font.size = Pt(size)
        if bold is not None: run.bold = bold

    def add_table(doc, headers, rows, widths=None, style='Table Grid', header_bold=True):
        cols = len(headers)
        t = doc.add_table(rows=1, cols=cols)
        t.style = style
        if widths:
            t.autofit = False
            for col, w in zip(t.columns, widths): col.width = Mm(w)
        for i, h in enumerate(headers):
            t.rows[0].cells[i].text = h
            if header_bold:
                for r in t.rows[0].cells[i].paragraphs[0].runs: set_font(r, bold=True)
        repeat = OxmlElement('w:tblHeader')
        t.rows[0]._tr.get_or_add_trPr().append(repeat)
        for row_data in rows:
            row = t.add_row()
            nosplit = OxmlElement('w:cantSplit')
            row._tr.get_or_add_trPr().append(nosplit)
            for i, val in enumerate(row_data):
                row.cells[i].text = str(val) if val is not None else ''
                for p in row.cells[i].paragraphs:
                    p.paragraph_format.space_after = Pt(2)
                    for r in p.runs: set_font(r, size=9)
        return t

    def kv_table(doc, pairs, width_l=60, width_r=110):
        return add_table(doc, ['项目', '内容'], pairs, widths=[width_l, width_r])

    def fmt(val, unit=''):
        if val is None: return ''
        s = str(val)
        if unit: s += ' ' + unit
        return s

    # ---- section 1: full-bleed cover ----
    cover = report['cover']
    asset = report['snapshot'].get('cover_asset') or {}
    has_cover_art = (cover.get('imageMode') == 'scene' and asset_path
                     and asset.get('variant') == 'agricultural-cover-v2')

    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    if has_cover_art:
        section.top_margin = section.bottom_margin = Mm(0)
        section.left_margin = section.right_margin = Mm(0)
        section.header_distance = section.footer_distance = Mm(0)
        rendered = _render_cover(asset_path, cover, report.get('case_title') or '')
        image = document.add_picture(rendered, width=Mm(210), height=Mm(297))
        picture = document.paragraphs[-1]
        picture.alignment = WD_ALIGN_PARAGRAPH.CENTER
        picture.paragraph_format.space_before = Pt(0)
        picture.paragraph_format.space_after = Pt(0)
        picture.paragraph_format.line_spacing = 1.0
        crop_xml = OxmlElement('a:srcRect')
        for key, value in _cover_crop(asset).items():
            crop_xml.set(key, str(value))
        image._inline.graphic.graphicData.pic.blipFill.insert(1, crop_xml)
        image._inline.graphic.graphicData.pic.nvPicPr.cNvPr.set('name', 'cover.png')
    else:
        section.top_margin = section.bottom_margin = Mm(18)
        section.left_margin = section.right_margin = Mm(20)
        doc_title = document.add_paragraph(cover.get('title') or '农业尽调报告', 'Title')
        for r in doc_title.runs: set_font(r, size=26, bold=True)

    # ---- section 2: body (aligned to the reviewed 农贷通-style template) ----
    body = document.add_section(WD_SECTION_START.NEW_PAGE)
    body.page_width, body.page_height = Mm(210), Mm(297)
    body.top_margin, body.bottom_margin = Mm(18), Mm(18)
    body.left_margin, body.right_margin = Mm(20), Mm(20)
    body.header_distance, body.footer_distance = Mm(8), Mm(8)
    if has_cover_art:
        body.header.is_linked_to_previous = False
        body.footer.is_linked_to_previous = False
        body.header.paragraphs[0].text = '农业尽调报告'
        body.header.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        footer = body.footer.paragraphs[0]
        footer.text = '第 '
        fld = OxmlElement('w:fldSimple'); fld.set(qn('w:instr'), 'PAGE')
        footer._p.append(fld)
        footer.add_run(' 页')
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER

    calculation = report['snapshot'].get('calculation') or {}
    inputs = calculation.get('input_snapshot') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    answers = report['snapshot'].get('answers') or {}
    customer = report['snapshot'].get('customer_info') or {}
    credit = report.get('credit') or {}
    is_valid = report.get('branch') == 'calculated_draft'
    appendix = report.get('calculation_appendix')

    def field_val(fid, default='—'):
        """Answer first, model input second — values only, no meta text."""
        display = answers.get(fid)
        if display:
            return display
        record = (inputs.get('fields') or {}).get(fid) or {}
        value = record.get('value')
        if value is None:
            return default
        value = {'fruit': '水果', 'grain': '主粮', 'herb': '中草药'}.get(value, value)
        unit = record.get('unit') or ''
        if unit in ('类别', '分类', '状态', '类型'):
            unit = ''
        return f'{value} {unit}'.strip() or default

    # ===== 授信结果横幅（封面之下、正文最前） =====
    if is_valid and credit.get('effective_limit'):
        try:
            score_display = f"{float(credit.get('score')):g}"
        except (TypeError, ValueError):
            score_display = str(credit.get('score') or '—')
        banner = _render_credit_banner(score_display, f"{int(credit['effective_limit']):,}", credit.get('rating'))
        if banner:
            picture = document.add_picture(banner, width=Mm(170))
            # a deterministic docPr name keeps document.xml stable across rebuilds
            picture._inline.graphic.graphicData.pic.nvPicPr.cNvPr.set('name', 'credit-banner.png')
            picture._inline.graphic.graphicData.pic.nvPicPr.cNvPr.set('descr', '授信测算结果横幅')
            document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ===== 案件信息 =====
    document.add_heading('案件信息', 1)
    kv_table(document, [
        ('客户姓名', customer.get('customer_name') or (report.get('case_title') or '').split('-')[0]),
        ('证件号码', customer.get('id_number') or ''),
        ('贷款用途', '农业种植'),
        ('申请金额', field_val('loan.requested_amount') or '待确认'),
        ('尽调人员', cover.get('author') or '待填写'),
        ('报告日期', cover.get('date') or '待填写'),
    ])

    # ===== 测算结果 =====
    document.add_heading('测算结果', 1)
    add_table(document,
              ['模型评分', '信用等级', '辅助测算额度'],
              [[str(credit.get('score') or '—') if is_valid else '—',
                str(credit.get('rating') or '—') if is_valid else '—',
                (str(credit.get('effective_limit') or '—') + ' 元') if is_valid else '—']],
              widths=[55, 55, 60], style='Light Shading Accent 1')
    document.add_paragraph('本结果为辅助模型试算，仍需人员复核；首版未配置风险定价。')

    # ===== 一、农户信用评级 =====
    document.add_heading('一、农户信用评级', 1)
    document.add_heading('1.1 评分评级', 2)
    kv_table(document, [
        ('模型评分', str(credit.get('score') or '—') if is_valid else '—'),
        ('信用等级', str(credit.get('rating') or '—') if is_valid else '—'),
        ('辅助测算额度', (str(credit.get('effective_limit') or '—') + ' 元') if is_valid else '—'),
    ])
    score_items = []
    if appendix:
        for row in (appendix.get('indicators') or []):
            value = row.get('value')
            if value in (None, ''):
                continue  # rows without data stay out of the report
            score_items.append((str(row.get('label') or ''), f"{value}{row.get('unit') or ''}", ''))
    if score_items:  # no usable indicators -> the whole 1.2 block (title included) is hidden
        document.add_heading('1.2 评分评级详细指标', 2)
        add_table(document, ['指标名称', '实际值', '预警'], score_items,
                  widths=[65, 70, 35])  # same grid style as the other chapters

    # ===== 二、农户家庭信息 =====
    document.add_heading('二、农户家庭信息', 1)
    document.add_heading('2.1 家庭信息', 2)
    kv_table(document, [
        ('家庭总人数', field_val('household.member_count', '待确认')),
        ('需照料人数', field_val('household.dependent_count')),
        ('医保参保人数', field_val('household.medical_count')),
        ('养老参保人数', field_val('household.pension_count')),
        ('养老缴费档位', field_val('household.pension_tier')),
        ('婚姻状况', field_val('operator.marital_status')),
        ('户籍情况', field_val('operator.local_registration')),
    ])

    # ===== 三、农户经营信息 =====
    document.add_heading('三、农户经营信息', 1)
    document.add_heading('3.1 土地信息', 2)
    kv_table(document, [
        ('承包地面积', field_val('land.contracted_area', '待确认')),
        ('主营种植面积', field_val('crop.main.area')),
        ('次营种植面积', field_val('crop.secondary.area')),
    ])
    document.add_heading('3.2 经营收入', 2)
    kv_table(document, [
        ('主营品类', field_val('crop.main.scale_category')),
        ('种植年限', field_val('crop.main.experience_years')),
        ('亩产量', field_val('crop.main.yield')),
        ('市场单价', field_val('crop.main.price')),
        ('年种植收入', field_val('income.planting.annual_receipts')),
        ('其他年收入', field_val('business.other.annual_income')),
        ('销售渠道', field_val('business.sales_stability')),
    ])
    document.add_heading('3.3 经营成本', 2)
    kv_table(document, [
        ('总成本', field_val('cost.total')),
        ('种子费用', field_val('cost.seed')),
        ('农药费用', field_val('cost.pesticide')),
        ('肥料费用', field_val('cost.fertilizer')),
        ('水电灌溉', field_val('cost.water_power')),
        ('人工费用', field_val('cost.labor')),
        ('社会化服务费', field_val('cost.service')),
    ])

    # ===== 四、资产与负债信息 =====
    document.add_heading('四、资产与负债信息', 1)
    document.add_heading('4.1 房产信息', 2)
    kv_table(document, [
        ('房产类型', '宅基地自建房' if not field_val('assets.house_declared_value') else '商品房'),
        ('商品房申报价值', field_val('assets.house_declared_value')),
    ])
    document.add_heading('4.2 车辆信息', 2)
    assets_list = inputs.get('assets') or []
    vehicle = next((a for a in assets_list if isinstance(a, dict) and a.get('type') == 'vehicle'), None)
    kv_table(document, [
        ('车辆类型', '有' if vehicle else (field_val('assets.vehicles') or '无')),
    ])
    document.add_heading('4.3 银行贷款', 2)
    debts_list = inputs.get('debts') or []
    bank_debt = next((d for d in debts_list if isinstance(d, dict) and d.get('type') == 'bank'), None)
    kv_table(document, [
        ('银行贷款余额', fmt((bank_debt or {}).get('amount', {}).get('value'), '元') if bank_debt else (field_val('debt.flag') or '无')),
    ])

    # ===== 五、农业保险 =====
    document.add_heading('五、农业保险', 1)
    kv_table(document, [
        ('是否连续购买农业保险', '是' if field_val('insurance.continuous_years') else '待确认'),
        ('连续购买年数', field_val('insurance.continuous_years')),
    ])

    output = BytesIO(); document.save(output)
    return output.getvalue()


def _render_credit_banner(score, limit_text, rating):
    """Credit-result banner for the page right under the cover: gradient base,
    score gauge, big limit, rating pill (layout accepted by visual review)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import tempfile, os
        W, H = 1750, 420
        img = Image.new('RGB', (W, H))
        draw = ImageDraw.Draw(img, 'RGBA')
        c1, c2, c3 = (14, 58, 74), (17, 106, 94), (61, 47, 122)
        for x in range(W):
            t_ratio = x / W * 2
            a, b, k = (c1, c2, t_ratio) if t_ratio < 1 else (c2, c3, t_ratio - 1)
            draw.line([(x, 0), (x, H)], fill=tuple(round(a[i] + (b[i] - a[i]) * k) for i in range(3)))
        for y in range(150):
            draw.line([(0, y), (W, y)], fill=(255, 255, 255, int(36 * (1 - y / 150))))
        f_big = ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc', 58)
        f_mid = ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc', 116)
        f_sml = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 34)
        f_tny = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 28)
        try:
            ratio = max(0.0, min(1.0, float(score) / 100))
        except (TypeError, ValueError):
            ratio = 0.0
        cx, cy, r = 225, 210, 150
        draw.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=(255, 255, 255, 60), width=22)
        draw.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * ratio, fill=(159, 232, 255), width=22)
        draw.text((cx, cy - 26), str(score), font=f_big, fill=(255, 255, 255), anchor='mm')
        draw.text((cx, cy + 34), '模型评分', font=f_tny, fill=(207, 232, 226), anchor='mm')
        tx = 480
        draw.text((tx, 118), '辅助测算授信额度', font=f_sml, fill=(168, 216, 205))
        draw.text((tx, 236), f'¥ {limit_text}', font=f_mid, fill=(234, 255, 247), anchor='lm')
        draw.text((tx, 340), '授信参考 · 以行内审批为准', font=f_tny, fill=(207, 232, 226))
        if rating:
            badge = f'信用等级 {rating}'
            tw = draw.textlength(badge, font=f_sml)
            bw, bh = tw + 76, 66
            bx, by = W - 100 - int(bw), 236 - bh // 2
            draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2,
                                   fill=(255, 233, 168, 36), outline=(255, 233, 168, 120), width=2)
            draw.text((bx + bw / 2, by + bh / 2 + 2), badge, font=f_sml, fill=(255, 233, 168), anchor='mm')
        fd, path = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        img.save(path, format='PNG')
        return path
    except Exception:
        return None


def _render_cover(asset_path, cover, case_title=''):
    """Pre-render cover artwork with meta text drawn into the blank band."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import tempfile, os
        image = Image.open(asset_path).convert('RGB')
        width, height = image.size
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', max(22, width // 36))
        farmer = case_title.split('-')[0].strip() or '待填写'
        investigator = (cover.get('author') or '').strip() or '待填写'
        date = (cover.get('date') or '').strip() or '待填写'
        text = f'农户名称：{farmer}    尽调人员：{investigator}    报告日期：{date}'
        draw = ImageDraw.Draw(image)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((width // 2 - (box[2] - box[0]) // 2, int(height * 0.945) - (box[3] - box[1]) // 2),
                  text, fill=(74, 74, 74), font=font)
        fd, path = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        image.save(path, format='PNG')
        return path
    except Exception:
        return str(asset_path)


def _cover_crop(asset):
    ratio = asset['width_px'] / asset['height_px']
    target = 210 / 297
    if ratio > target:
        side = round((1 - target / ratio) / 2 * 100000)
        return {'l': side, 'r': side, 't': 0, 'b': 0}
    side = round((1 - ratio / target) / 2 * 100000)
    return {'l': 0, 'r': 0, 't': side, 'b': side}

def _render_cover(asset_path, cover, case_title=''):
    """Pre-render cover artwork with meta text drawn into the blank band."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import tempfile, os
        image = Image.open(asset_path).convert('RGB')
        width, height = image.size
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', max(22, width // 36))
        farmer = case_title.split('-')[0].strip() or '待填写'
        investigator = (cover.get('author') or '').strip() or '待填写'
        date = (cover.get('date') or '').strip() or '待填写'
        text = f'农户名称：{farmer}    尽调人员：{investigator}    报告日期：{date}'
        draw = ImageDraw.Draw(image)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((width // 2 - (box[2] - box[0]) // 2, int(height * 0.945) - (box[3] - box[1]) // 2),
                  text, fill=(74, 74, 74), font=font)
        fd, path = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        image.save(path, format='PNG')
        return path
    except Exception:
        return str(asset_path)


def _cover_crop(asset):
    ratio = asset['width_px'] / asset['height_px']
    target = 210 / 297
    if ratio > target:
        side = round((1 - target / ratio) / 2 * 100000)
        return {'l': side, 'r': side, 't': 0, 'b': 0}
    side = round((1 - ratio / target) / 2 * 100000)
    return {'l': 0, 'r': 0, 't': side, 'b': side}
