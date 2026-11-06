"""P2 answers domain: versioned customer info, per-question answers with
multi-entry details, and interview progress.

Layering: statements keep verbatim evidence and facts keep verified inputs for
the models. An *answer* binds one catalog field of one case to a human-entered
or confirmed value (scalar or list of detail items) with its basis. It never
mutates statements or facts and never feeds model calculations by itself.

Every write creates a new revision of the whole payload (event-sourced, like
s3 statements/facts). Optimistic concurrency: writers must pass the revision
they read; a mismatch is a 409 and nothing is overwritten. All reads are
case-scoped; an answer id from another case is rejected, never leaked.
"""
import hashlib
import json
import pathlib
import re
import uuid

from .diligence import DiligenceError, encoded, error, new_id, now

MAX_TEXT = 2000
ACTOR_PATTERN = re.compile(r'^[\w\u4e00-\u9fff.@-]{1,64}$')
CUSTOMER_FIELDS = {
    'customer_name': {'kind': 'text', 'label': '客户姓名', 'required': True},
    'id_type': {'kind': 'enum', 'label': '证件类型', 'options': ['居民身份证'], 'required': True},
    'id_number': {'kind': 'text', 'label': '证件号码', 'required': True},
    'phone': {'kind': 'text', 'label': '联系电话', 'required': False},
    'home_address': {'kind': 'text', 'label': '常住地址', 'required': False},
    'contact_address': {'kind': 'text', 'label': '通讯地址', 'required': False},
    'note': {'kind': 'text', 'label': '备注', 'required': False},
}
ANSWER_STATUSES = ('draft', 'confirmed', 'not_applicable')
BASIS_TYPES = ('statement', 'fact', 'manual', 'segment')
ITEM_STATUSES = ('active', 'revoked')
# frozen v2 snapshots name detail fields without embedding a structure id;
# v3 catalogs carry list_structure directly on the field.
V2_LIST_STRUCTURE_BY_FIELD = {
    'household.member_list': 'household.members', 'land.transfers': 'land.plots',
    'assets.vehicles': 'asset.vehicles', 'assets.machinery': 'asset.machinery',
    'assets.financial': 'asset.financial_items', 'debt.bank_contracts': 'debt.contracts',
    'debt.other_contracts': 'debt.contracts', 'insurance.policies': 'insurance.policies'}


class AnswerService:
    def __init__(self, store, diligence):
        self.store, self.diligence = store, diligence
        with store.lock, store.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS s2_customer_info (
                case_id TEXT NOT NULL REFERENCES cases(id),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(case_id, revision));
              CREATE TABLE IF NOT EXISTS s2_answers (
                id TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES cases(id),
                field_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                payload_json TEXT NOT NULL,PRIMARY KEY(id, revision));
              CREATE INDEX IF NOT EXISTS s2_answer_case ON s2_answers(case_id, id, revision);
              CREATE INDEX IF NOT EXISTS s2_answer_field ON s2_answers(case_id, field_id, revision);
            ''')

    # ---------- shared helpers ----------

    def _case(self, connection, case_id):
        row = connection.execute("SELECT * FROM cases WHERE id=? AND app_id='due-diligence'", (case_id,)).fetchone()
        if row is None:
            error(404, 'case_not_found', '本尽调应用中不存在该案件。')
        return row

    def _catalog(self, connection, case_id):
        """The case-frozen questionnaire snapshot, falling back to the active catalog."""
        row = connection.execute('SELECT catalog_json FROM s3_questionnaire_snapshots WHERE case_id=?',
                                 (case_id,)).fetchone()
        catalog = json.loads(row[0]) if row else self.diligence.questionnaire
        fields = {f['field_id']: f for f in catalog['fields']}
        return catalog, fields

    def _followup_questions(self, connection, case_id, catalog_fields):
        """Accepted followup cards become answerable onsite questions.

        Each accepted card gets its own stable answer field id so it can be
        answered and tracked without colliding with the catalog field it
        refers to; the card kind (ai/rule) travels along for the UI badge."""
        has_cards = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s4_cards'").fetchone()
        if not has_cards:
            return {}
        rows = connection.execute('SELECT id, payload_json FROM s4_cards WHERE case_id=? ORDER BY id',
                                  (case_id,)).fetchall()
        sections = {field_id: field.get('section') for field_id, field in catalog_fields.items()}
        extra = {}
        for index, (card_id, payload) in enumerate(rows):
            card = json.loads(payload)
            if card.get('status') != 'accepted':
                continue
            base = card.get('field_id')
            field_id = f'followup.{base}.{index}'
            extra[field_id] = {'field_id': field_id, 'label': card.get('label') or base,
                               'question_text': card.get('question'),
                               'section': sections.get(base) or '补问',
                               'value_type': 'text', 'unit': None,
                               'required_class': 'optional_candidate', 'list_structure': None,
                               'handling': 'interview',
                               'source': card.get('kind') or 'rule', 'source_card_id': card_id}
        return extra

    @staticmethod
    def _latest(connection, table, where, params):
        row = connection.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY revision DESC LIMIT 1',
                                 params).fetchone()
        return json.loads(row['payload_json']) if row else None

    @staticmethod
    def _check_revision(current, expected):
        if current is None:
            if expected not in (None, 0):
                error(409, 'revision_conflict', '记录已被他人修改或不存在，请刷新后重试。')
        elif expected is None or current['revision'] != expected:
            error(409, 'revision_conflict', '记录已被他人修改，请刷新后重试；不会覆盖他人改动。')

    @staticmethod
    def _actor(data):
        actor = str(data.get('actor') or 'local_user')
        if not ACTOR_PATTERN.fullmatch(actor):
            error(422, 'invalid_actor', '修改人标识不合规。')
        return actor

    @staticmethod
    def _clean_text(value, label, max_length=MAX_TEXT):
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if len(value) > max_length or any(ord(ch) < 32 for ch in value):
            error(422, 'invalid_text', f'{label}超长或含控制字符。')
        return value

    # ---------- customer info ----------

    def customer_info(self, case_id):
        with self.store.connect() as connection:
            self._case(connection, case_id)
            payload = self._latest(connection, 's2_customer_info', 'case_id=?', (case_id,))
            revisions = connection.execute('SELECT revision, actor, updated_at FROM s2_customer_info '
                                           'WHERE case_id=? ORDER BY revision', (case_id,)).fetchall()
            return {'case_id': case_id, 'customer_info': payload,
                    'revision': payload['revision'] if payload else 0,
                    'history': [dict(row) for row in revisions]}

    def update_customer_info(self, case_id, data):
        expected = data.get('expected_revision', 0)
        fields = data.get('fields')
        if not isinstance(fields, dict) or set(fields) - set(CUSTOMER_FIELDS):
            error(422, 'invalid_customer_fields', '客户信息字段不在此契约内。')
        clean = {}
        for key, spec in CUSTOMER_FIELDS.items():
            value = self._clean_text(fields.get(key), spec['label'])
            if spec['required'] and not value:
                error(422, 'missing_customer_field', f"缺少必填客户信息：{spec['label']}。")
            if spec.get('options') and value not in spec['options']:
                error(422, 'invalid_customer_field', f"{spec['label']}不在允许范围。")
            clean[key] = value
        actor = self._actor(data)
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            current = self._latest(connection, 's2_customer_info', 'case_id=?', (case_id,))
            self._check_revision(current, expected)
            revision = (current['revision'] if current else 0) + 1
            payload = {'case_id': case_id, 'revision': revision, 'fields': clean,
                       'actor': actor, 'updated_at': now(),
                       'supersedes_revision': current['revision'] if current else None,
                       'changed_keys': sorted(k for k in clean if not current or clean[k] != current['fields'].get(k))}
            connection.execute('INSERT INTO s2_customer_info VALUES (?,?,?,?,?)',
                               (case_id, revision, encoded(payload), actor, payload['updated_at']))
            return payload

    # ---------- answers ----------

    def _field(self, connection, case_id, field_id):
        catalog, fields = self._catalog(connection, case_id)
        field = fields.get(field_id)
        if field is None:
            followups = self._followup_questions(connection, case_id, fields)
            field = followups.get(field_id)
        if field is None:
            error(422, 'unknown_field', '该问题不在本案问卷或已采纳补问范围内。')
        return catalog, field

    def _validate_value(self, field, value):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {'text', 'number', 'boolean', 'date', 'choice', 'unit'}:
            error(422, 'invalid_value_shape', '答案值必须是受限结构（text/number/boolean/date/choice/unit）。')
        clean = {k: v for k, v in value.items() if v is not None}
        if 'text' in clean:
            clean['text'] = self._clean_text(clean['text'], '答案文本')
        if not clean:
            error(422, 'empty_value', '答案值不能为空；空白不自动视为零或不适用。')
        return clean or None

    def _validate_items(self, field, items, *, partial=False):
        structure_id = field.get('list_structure') or V2_LIST_STRUCTURE_BY_FIELD.get(field['field_id'])
        if items is None:
            return None
        if not isinstance(items, list):
            error(422, 'invalid_items', '多笔明细必须是列表。')
        if structure_id is None:
            error(422, 'not_a_list_field', '该问题没有定义多笔明细结构。')
        structure = self.diligence.questionnaire.get('list_structures', {}).get(structure_id)
        if structure is None:
            # frozen v2 snapshots do not carry list structures; fall back to the active catalog's definition
            structure = LIST_STRUCTURE_FALLBACK.get(structure_id)
        if structure is None:
            error(422, 'unknown_list_structure', f'明细结构 {structure_id} 未定义。')
        member_keys = {m['key'] for m in structure['members']}
        clean = []
        for item in items:
            if not isinstance(item, dict):
                error(422, 'invalid_item', '明细条目必须是对象。')
            if not partial and not item.get('item_id'):
                error(422, 'missing_item_id', '明细条目缺少 item_id。')
            extra = set(item) - {'item_id', 'status', 'note', 'members'}
            if extra:
                error(422, 'invalid_item_keys', f'明细条目含未定义成员：{sorted(extra)}。')
            members = item.get('members')
            if not isinstance(members, dict):
                error(422, 'invalid_item_members', '明细条目 members 必须是对象。')
            unknown = set(members) - member_keys
            if unknown:
                error(422, 'invalid_item_keys', f'明细条目含未定义成员：{sorted(unknown)}。')
            for key, value in members.items():
                if isinstance(value, str):
                    self._clean_text(value, key)
            status = item.get('status', 'active')
            if status not in ITEM_STATUSES:
                error(422, 'invalid_item_status', '明细条目状态不合法。')
            clean.append({'item_id': item.get('item_id'), 'status': status,
                          'note': self._clean_text(item.get('note'), '明细备注', 500),
                          'members': members})
        return clean

    def _validate_basis(self, basis, *, require_basis=False):
        if basis is None:
            basis = []
        if not isinstance(basis, list):
            error(422, 'invalid_basis', '依据必须是列表。')
        clean = []
        for entry in basis:
            if not isinstance(entry, dict) or entry.get('type') not in BASIS_TYPES or not entry.get('id'):
                error(422, 'invalid_basis', '依据条目必须指明类型（statement/fact/manual）与ID。')
            clean.append({'type': entry['type'], 'id': str(entry['id']),
                          'note': self._clean_text(entry.get('note'), '依据说明', 500)})
        if require_basis and not clean:
            error(422, 'missing_basis', '确认与不适用操作必须携带依据。')
        return clean

    def _validate_answer_payload(self, connection, case_id, field_id, data, *, confirming=False):
        catalog, field = self._field(connection, case_id, field_id)
        status = data.get('status', 'draft')
        if status not in ANSWER_STATUSES:
            error(422, 'invalid_status', '答案状态不合法。')
        reason = None
        if status == 'not_applicable':
            reason = self._clean_text(data.get('not_applicable_reason'), '不适用原因')
            if not reason:
                error(422, 'missing_not_applicable_reason', '标记不适用必须给出原因。')
        value = self._validate_value(field, data.get('value'))
        items = self._validate_items(field, data.get('items'))
        if status == 'confirmed' and value is None and not items:
            error(422, 'confirmed_empty', '确认的答案必须包含值或多笔明细；空白不自动视为零或不适用。')
        if value is not None and items:
            error(422, 'value_and_items', '标量值与多笔明细不能同时填写。')
        if status == 'not_applicable' and (value is not None or items):
            error(422, 'not_applicable_with_value', '不适用不能同时携带答案值。')
        basis = self._validate_basis(data.get('basis'), require_basis=(status in ('confirmed', 'not_applicable')))
        period = self._validate_period(data.get('period'))
        subject = self._clean_text(data.get('subject'), '主体', 120)
        return {'field_id': field_id, 'catalog_version': catalog['version'], 'status': status,
                'value': value, 'items': items, 'basis': basis,
                'not_applicable_reason': reason, 'period': period, 'subject': subject,
                'origin': str(data.get('origin') or 'manual_input')}

    def _validate_period(self, period):
        if period is None:
            return None
        if not isinstance(period, dict) or set(period) - {'kind', 'start', 'end', 'label'}:
            error(422, 'invalid_period', '期间必须是 {kind,start,end,label} 结构或空。')
        return {key: (self._clean_text(value, '期间', 64) if value is not None else None)
                for key, value in period.items()} or None

    def answers(self, case_id):
        with self.store.connect() as connection:
            self._case(connection, case_id)
            rows = connection.execute('SELECT id, field_id, MAX(revision) FROM s2_answers WHERE case_id=? '
                                      'GROUP BY id, field_id ORDER BY field_id', (case_id,)).fetchall()
            latest = []
            for answer_id, field_id, revision in rows:
                payload = json.loads(connection.execute(
                    'SELECT payload_json FROM s2_answers WHERE id=? AND revision=?',
                    (answer_id, revision)).fetchone()[0])
                latest.append(payload)
            return {'case_id': case_id, 'answers': latest,
                    'progress': self._progress_payload(connection, case_id, latest)}

    def create_answer(self, case_id, data):
        field_id = data.get('field_id')
        if not field_id or not isinstance(field_id, str):
            error(422, 'missing_field_id', '必须指明问题 field_id。')
        actor = self._actor(data)
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            _, fields = self._catalog(connection, case_id)
            if field_id not in fields and field_id not in self._followup_questions(connection, case_id, fields):
                error(422, 'unknown_field', '该问题不在本案问卷或已采纳补问范围内。')
            current = self._latest(connection, 's2_answers', 'case_id=? AND field_id=?', (case_id, field_id))
            if current is not None:
                error(409, 'answer_exists', '该问题已有答案记录；请修订或重开现有答案，不新建重复答案。')
            clean = self._validate_answer_payload(connection, case_id, field_id, data)
            answer_id = new_id('answer')
            revision = 1
            payload = {'answer_id': answer_id, 'case_id': case_id, **clean,
                       'revision': revision, 'actor': actor, 'updated_at': now(),
                       'supersedes_revision': None, 'change': 'create'}
            connection.execute('INSERT INTO s2_answers VALUES (?,?,?,?,?)',
                               (answer_id, case_id, field_id, revision, encoded(payload)))
            return payload

    def _answer(self, connection, case_id, answer_id):
        payload = self._latest(connection, 's2_answers', 'case_id=? AND id=?', (case_id, answer_id))
        if payload is None:
            error(404, 'answer_not_found', '本案不存在该答案。')
        return payload

    def answer(self, case_id, answer_id):
        with self.store.connect() as connection:
            self._case(connection, case_id)
            return self._answer(connection, case_id, answer_id)

    def answer_history(self, case_id, answer_id):
        with self.store.connect() as connection:
            self._case(connection, case_id)
            self._answer(connection, case_id, answer_id)
            rows = connection.execute('SELECT revision, payload_json FROM s2_answers WHERE case_id=? AND id=? '
                                      'ORDER BY revision', (case_id, answer_id)).fetchall()
            return {'answer_id': answer_id, 'revisions': [json.loads(r[1]) for r in rows]}

    def revise_answer(self, case_id, answer_id, data):
        expected = data.get('expected_revision')
        actor = self._actor(data)
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            current = self._answer(connection, case_id, answer_id)
            self._check_revision(current, expected)
            # explicit override semantics: a key present in the request (even null)
            # replaces the previous value; absent keys keep the previous value.
            merged = {'field_id': current['field_id']}
            for key in ('status', 'value', 'items', 'basis', 'not_applicable_reason', 'origin',
                        'period', 'subject'):
                merged[key] = data[key] if key in data else current.get(key)
            clean = self._validate_answer_payload(connection, case_id, current['field_id'], merged)
            revision = current['revision'] + 1
            payload = {'answer_id': answer_id, 'case_id': case_id, **clean,
                       'revision': revision, 'actor': actor, 'updated_at': now(),
                       'supersedes_revision': current['revision'], 'change': 'revise'}
            connection.execute('INSERT INTO s2_answers VALUES (?,?,?,?,?)',
                               (answer_id, case_id, current['field_id'], revision, encoded(payload)))
            return payload

    def _revise_item_status(self, case_id, answer_id, item_id, data, change):
        expected, actor = data.get('expected_revision'), self._actor(data)
        reason = self._clean_text(data.get('reason'), '原因')
        if change == 'revoke_item' and not reason:
            error(422, 'missing_reason', '撤销明细必须给出原因。')
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            current = self._answer(connection, case_id, answer_id)
            self._check_revision(current, expected)
            items = current.get('items') or []
            target = next((i for i in items if i['item_id'] == item_id), None)
            if target is None:
                error(404, 'item_not_found', '该答案下不存在此明细条目。')
            if change == 'revoke_item':
                target['status'], target['note'] = 'revoked', reason
            else:
                if target['status'] != 'revoked':
                    error(409, 'item_not_revoked', '只有已撤销的明细可以重开。')
                target['status'] = 'active'
                target['note'] = reason or target.get('note')
            revision = current['revision'] + 1
            payload = {**current, 'revision': revision, 'actor': actor, 'updated_at': now(),
                       'supersedes_revision': current['revision'], 'change': change, 'items': items}
            connection.execute('INSERT INTO s2_answers VALUES (?,?,?,?,?)',
                               (answer_id, case_id, current['field_id'], revision, encoded(payload)))
            return payload

    def revoke_item(self, case_id, answer_id, item_id, data):
        return self._revise_item_status(case_id, answer_id, item_id, data, 'revoke_item')

    def reopen_item(self, case_id, answer_id, item_id, data):
        return self._revise_item_status(case_id, answer_id, item_id, data, 'reopen_item')

    def adopt_candidates_bulk(self, case_id, candidates, status='confirmed'):
        """One-click adoption of the latest run's candidates. Only fields with
        exactly one pending candidate and no existing answer are adopted;
        answered fields are skipped and multi-candidate fields (conflicting
        readings) are left for a human choice — the system never picks a
        value for the person."""
        with self.store.connect() as connection:
            self._case(connection, case_id)
            answered = {row[0] for row in connection.execute(
                'SELECT DISTINCT field_id FROM s2_answers WHERE case_id=?', (case_id,))}
        by_field = {}
        for candidate in candidates:
            by_field.setdefault(candidate['field_id'], []).append(candidate)
        adopted, skipped_answered, conflict_fields = [], [], []
        for field_id, items in by_field.items():
            if field_id in answered:
                skipped_answered.append(field_id)
                continue
            if len(items) > 1:
                conflict_fields.append(field_id)
                continue
            try:
                self.adopt_candidate(case_id, {'candidate_id': items[0]['candidate_id'],
                                               'status': status, 'actor': 'workbench_user'})
                adopted.append(field_id)
            except DiligenceError:
                skipped_answered.append(field_id)  # answered concurrently
        return {'adopted_count': len(adopted), 'adopted_fields': adopted,
                'skipped_answered': skipped_answered, 'conflict_fields': conflict_fields}

    def adopt_candidate(self, case_id, data):
        """Turn a stored LLM candidate into a human-submitted answer. The person
        adopting chooses draft or confirmed; the verbatim quote and segment are
        carried as basis automatically. The candidate itself never mutates."""
        candidate_id = data.get('candidate_id')
        status = data.get('status', 'draft')
        if status not in ('draft', 'confirmed'):
            error(422, 'invalid_status', '采纳状态只能是草稿或确认。')
        if not candidate_id or not isinstance(candidate_id, str):
            error(422, 'missing_candidate_id', '必须指明候选 candidate_id。')
        actor = self._actor(data)
        with self.store.lock, self.store.connect() as connection:
            self._case(connection, case_id)
            row = connection.execute('SELECT payload_json FROM s4x_candidates WHERE case_id=? AND id=? '
                                     'ORDER BY revision DESC LIMIT 1', (case_id, candidate_id)).fetchone()
            if row is None:
                error(404, 'candidate_not_found', '本案不存在该候选。')
            candidate = json.loads(row[0])
            field_id = candidate['field_id']
            catalog, fields = self._catalog(connection, case_id)
            if field_id not in fields:
                error(422, 'unknown_field', '候选指向的题目不在本案冻结目录内。')
            current = self._latest(connection, 's2_answers', 'case_id=? AND field_id=?', (case_id, field_id))
            if current is not None:
                error(409, 'answer_exists', '该问题已有答案记录；请修订现有答案，不重复采纳。')
            value = {'unit': candidate.get('unit')} if candidate.get('unit') else {}
            if candidate['value_kind'] == 'text':
                value['text'] = candidate['value']
            elif candidate['value_kind'] == 'number':
                value['number'] = candidate['value']
            elif candidate['value_kind'] == 'boolean':
                value['boolean'] = candidate['value']
            elif candidate['value_kind'] == 'date':
                value['date'] = candidate['value']
            basis = [{'type': 'segment', 'id': candidate['segment_id'],
                      'note': f"原话：{candidate['quote']}（录音 {candidate['recording_id']} r{candidate['segment_revision']}）"}]
            note = str(data.get('note') or '').strip()
            if note:
                basis.append({'type': 'manual', 'id': 'adopt_note', 'note': note[:300]})
            answer_id = new_id('answer')
            payload = {'answer_id': answer_id, 'case_id': case_id, 'field_id': field_id,
                       'catalog_version': catalog['version'], 'status': status,
                       'value': value or None, 'items': None, 'basis': basis,
                       'not_applicable_reason': None, 'origin': 'llm_candidate_adopted',
                       'revision': 1, 'actor': actor, 'updated_at': now(),
                       'supersedes_revision': None, 'change': 'adopt_candidate',
                       'adopted_candidate_id': candidate_id}
            connection.execute('INSERT INTO s2_answers VALUES (?,?,?,?,?)',
                               (answer_id, case_id, field_id, 1, encoded(payload)))
            return payload

    # ---------- progress ----------

    def _progress_payload(self, connection, case_id, answers, catalog=None):
        catalog = catalog or self._catalog(connection, case_id)[0]
        return calculate_progress(catalog, answers, list_structures=self._list_structures())

    def _list_structures(self):
        return self.diligence.questionnaire.get('list_structures') or {}

    def progress(self, case_id):
        with self.store.connect() as connection:
            self._case(connection, case_id)
            rows = connection.execute('SELECT id, MAX(revision) FROM s2_answers WHERE case_id=? '
                                      'GROUP BY id', (case_id,)).fetchall()
            latest = [json.loads(connection.execute('SELECT payload_json FROM s2_answers WHERE id=? AND revision=?',
                                                    (r[0], r[1])).fetchone()[0]) for r in rows]
            payload = self._progress_payload(connection, case_id, latest)
            return {'case_id': case_id, **payload}

    # ---------- aggregated board for the onsite page ----------

    def _structure_view(self, field):
        structure_id = field.get('list_structure') or V2_LIST_STRUCTURE_BY_FIELD.get(field['field_id'])
        if not structure_id:
            return None
        structure = self.diligence.questionnaire.get('list_structures', {}).get(structure_id) \
            or LIST_STRUCTURE_FALLBACK.get(structure_id)
        if structure is None:
            return None
        return {'id': structure_id, 'label': structure['label'],
                'cardinality': structure.get('cardinality'),
                'members': [{'key': m['key'], 'label': m['label'], 'value_type': m['value_type'],
                             'unit': m.get('unit')} for m in structure['members']]}

    def board(self, case_id):
        """Everything the onsite answer board needs in one read: the case-frozen
        interview questions with list structures, latest answers, progress and
        customer info. Read-only."""
        with self.store.connect() as connection:
            self._case(connection, case_id)
            catalog, catalog_fields = self._catalog(connection, case_id)
            # derived.* metrics and output.* model results are computed, never
            # asked onsite — they stay out of the board and the progress count
            catalog = {**catalog, 'fields': [f for f in catalog['fields']
                                            if not f['field_id'].startswith(('derived.', 'output.'))
                                            and not f.get('disabled')]}
            questions = []
            for field in catalog['fields']:
                if field.get('handling') not in ('interview', 'extension_required'):
                    continue
                questions.append({
                    'field_id': field['field_id'], 'label': field.get('label') or field.get('question_text'),
                    'question_text': field.get('question_text'), 'section': field.get('section'),
                    'value_type': field.get('value_type'), 'unit': field.get('default_unit'),
                    'required_class': field.get('required_class')
                        or ('required_candidate' if field.get('required') else 'optional_candidate'),
                    'list_structure': self._structure_view(field),
                    'required_source_markers': field.get('required_source_markers'),
                    'conditions': {'state': (field.get('conditions') or {}).get('state')} if 'required_class' in field else None})
            followup_questions = self._followup_questions(connection, case_id, catalog_fields)
            for question in followup_questions.values():
                questions.append({**question, 'list_structure': None,
                                  'conditions': None})
            # independence rule: fields already answered in the presale packet
            # (pre-visit facts) leave the onsite questionnaire — they are shown
            # on the prepare tab instead, so each question lives in one place.
            covered = self._presale_covered_fields(connection, case_id)
            if covered:
                questions = [question for question in questions
                             if question['field_id'] not in covered]
                progress_fields = [field for field in catalog['fields']
                                   if field['field_id'] not in covered]
                progress_catalog = {**catalog, 'fields': progress_fields}
            else:
                progress_catalog = catalog
            if followup_questions:
                progress_catalog = {**progress_catalog,
                                    'fields': [*progress_catalog.get('fields', []),
                                               *followup_questions.values()]}
            rows = connection.execute('SELECT id, field_id, MAX(revision) FROM s2_answers WHERE case_id=? '
                                      'GROUP BY id, field_id', (case_id,)).fetchall()
            latest = [json.loads(connection.execute('SELECT payload_json FROM s2_answers WHERE id=? AND revision=?',
                                                    (r[0], r[2])).fetchone()[0]) for r in rows]
            info = self._latest(connection, 's2_customer_info', 'case_id=?', (case_id,))
            return {'case_id': case_id, 'catalog_version': catalog['version'],
                    'questions': questions, 'answers': latest,
                    'progress': self._progress_payload(connection, case_id, latest,
                                                      catalog=progress_catalog),
                    'customer_info': info,
                    'previsit_reference': self._previsit_reference(connection, case_id, latest)}

    def _presale_covered_fields(self, connection, case_id):
        """Field ids that already carry an answer in the pre-visit packet."""
        row = connection.execute('SELECT context_json FROM s3_previsit_versions WHERE case_id=? '
                                 'ORDER BY loaded_at DESC LIMIT 1', (case_id,)).fetchone()
        if not row:
            return set()
        return {fact.get('field_id') for fact in json.loads(row[0]).get('facts', [])
                if fact.get('field_id')}

    def _previsit_reference(self, connection, case_id, answers):
        """Latest previsit-recorded facts per answered field, so the onsite page
        can show both sides of any disagreement without auto-overriding."""
        wanted = {answer['field_id'] for answer in answers}
        if not wanted:
            return {}
        rows = connection.execute('''SELECT f.payload_json FROM s3_facts f
            JOIN (SELECT id, MAX(revision) r FROM s3_facts WHERE case_id=? GROUP BY id) t
            ON f.id=t.id AND f.revision=t.r''', (case_id,)).fetchall()
        reference = {}
        for row in rows:
            fact = json.loads(row[0])
            field_id = fact.get('field_id')
            if field_id in wanted and fact.get('origin') == 'previsit':
                value = fact.get('value')
                presence = fact.get('presence')
                reference.setdefault(field_id, {
                    'fact_id': fact['id'], 'fact_revision': fact['revision'],
                    'previsit_value': value if value is not None else presence,
                    'presence': presence})
        for answer in answers:
            entry = reference.get(answer['field_id'])
            if entry is not None:
                answer_value = (answer.get('value') or {})
                onsite = answer_value.get('number', answer_value.get('text') or answer_value.get('choice'))
                entry['onsite_value'] = onsite
                entry['conflict'] = (answer.get('status') == 'confirmed'
                                     and str(onsite) != str(entry['previsit_value']))
        return reference


# ---------- progress as a pure function (fixed samples test it directly) ----------

def _normalized_required_class(field):
    """v3 catalogs carry required_class; frozen v2 snapshots are mapped without
    inventing conditions: source-required markers stay required-candidates,
    everything else optional-candidate. Candidate classes never become approved
    by this mapping."""
    if 'required_class' in field:
        return field['required_class']
    return 'required_candidate' if field.get('required') else 'optional_candidate'


def _condition_state(field):
    conditions = field.get('conditions')
    if not isinstance(conditions, dict):
        return 'none'
    if conditions.get('state') == 'confirmed':
        return 'confirmed'
    if conditions.get('state') == 'corrupted':
        return 'corrupted'
    return 'unconfirmed'


def calculate_progress(catalog, answers, *, list_structures=None):
    """Required/optional completion plus pending-judgement counts.

    - only confirmed answers count as done; a confirmed not-applicable answer
      leaves the required/optional denominators (the question no longer needs
      asking) and is reported on its own 不适用 line (never as a value);
    - conditionally required fields join the required denominator only when
      their condition is confirmed AND evaluates applicable; unconfirmed or
      corrupted conditions stay in pending_judgement and never silently drop
      a question;
    - pending_confirmation fields (whole group undetermined) also stay pending.
    """
    by_field = {}
    for answer in answers:
        field_id = answer.get('field_id')
        if field_id not in by_field:
            by_field[field_id] = answer  # answers list is already latest-per-id
    required_done = optional_done = processed_not_applicable = 0
    required_applicable = optional_total = pending_judgement = condition_inapplicable = 0
    pending_fields, not_applicable_fields = [], []
    for field in catalog['fields']:
        if field.get('disabled'):
            continue  # admin-disabled questions are not asked at all
        field_id, klass = field['field_id'], _normalized_required_class(field)
        answer = by_field.get(field_id)
        confirmed = bool(answer and answer.get('status') == 'confirmed')
        not_applicable = bool(answer and answer.get('status') == 'not_applicable')
        if klass == 'pending_confirmation':
            pending_judgement += 1
            pending_fields.append(field_id)
            continue
        if klass == 'conditionally_required_candidate':
            state = _condition_state(field)
            if state == 'confirmed':
                applicable = _evaluate_confirmed_condition(field, by_field)
                if applicable is False:
                    condition_inapplicable += 1
                    continue
                if applicable is None:
                    pending_judgement += 1
                    pending_fields.append(field_id)
                    continue
                # applicable is True -> counts as required below
            else:
                pending_judgement += 1
                pending_fields.append(field_id)
                continue
        if klass in ('required_candidate', 'conditionally_required_candidate'):
            if not_applicable:
                processed_not_applicable += 1
                not_applicable_fields.append(field_id)
                continue  # confirmed not-applicable leaves the denominator
            required_applicable += 1
            if confirmed:
                required_done += 1
        elif klass == 'optional_candidate':
            if not_applicable:
                processed_not_applicable += 1
                not_applicable_fields.append(field_id)
                continue  # confirmed not-applicable leaves the denominator
            optional_total += 1
            if confirmed:
                optional_done += 1
    return {'required': {'done': required_done, 'applicable': required_applicable},
            'optional': {'done': optional_done, 'total': optional_total},
            'pending_judgement': pending_judgement,
            'not_applicable': processed_not_applicable,
            'condition_inapplicable': condition_inapplicable,
            'pending_fields': pending_fields, 'not_applicable_fields': not_applicable_fields,
            'catalog_version': catalog.get('version'),
            'rules': '仅confirmed计入完成；不适用计入已处理单列；条件未确认或损坏保持待判断，不自动跳题。'}


def _evaluate_confirmed_condition(field, by_field):
    """Three-valued evaluation of a business-confirmed condition using the
    confirmed trigger answers currently on file. True/False/None(unknown)."""
    conditions = field.get('conditions') or {}
    expression = conditions.get('confirmed_expression')
    if not expression:
        return None
    trigger_id = expression.get('field_id')
    operator = expression.get('operator')
    expected = expression.get('value')
    trigger = by_field.get(trigger_id)
    if operator not in ('equals',) or not trigger or trigger.get('status') != 'confirmed':
        return None
    value = (trigger.get('value') or {})
    actual = value.get('choice') if isinstance(value, dict) else None
    if actual is None:
        actual = value.get('boolean') if isinstance(value, dict) else None
    if actual is None:
        return None
    return actual == expected


# List-structure definitions for frozen v2 snapshots whose payloads did not
# embed structure definitions. They mirror the v3 catalog (single maintained
# source); each structure keeps its own candidate review status.
_LIST_STRUCTURE_SOURCE = pathlib.Path(__file__).with_name('data') / 'questionnaire_v3_full_catalog.json'
LIST_STRUCTURE_FALLBACK = json.loads(_LIST_STRUCTURE_SOURCE.read_text(encoding='utf-8')).get('list_structures', {})
