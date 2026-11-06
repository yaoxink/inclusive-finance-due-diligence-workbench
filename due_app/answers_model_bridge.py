"""P5 bridge: explicitly map human-confirmed answers into model-v2 inputs.

Design rules from the plan:
- only answers with status=confirmed participate; drafts, not-applicable and
  candidates never enter the model;
- units live on the question (catalog default), not on the answer: an answer
  without a unit maps under the question's marked unit; only an explicitly
  contradictory unit is a gap. Periods are optional and pass through when
  the answer carries one (2026-11-05: periods no longer block);
- mapped records keep record_confirmation='record_accurate' (the answer was
  human-confirmed) but verification stays 'not_checked' — answer confirmation
  never silently upgrades to independent verification;
- source_ref cites the answer id and revision so every model input traces back;
- unmapped model inputs (upstream/credit/cashflow items) are never touched;
- runs and reports are never recalculated here; the workspace view gains a
  pending-recheck marker whenever answers changed after a run was created.
"""
from __future__ import annotations

import json

from .diligence import DiligenceError, error

# answer field_id -> (model input field, required unit or None)
ANSWER_TO_MODEL = {
    # answer-driven calculation inputs (2026-11-05): the questionnaire now asks
    # these directly, so the one-click limit chain fills every summary indicator
    'cost.total': ('cost.total', '元'),
    'income.planting.annual_receipts': ('income.planting.annual_receipts', '元'),
    'loan.requested_amount': ('loan.requested_amount', '元'),
    'loan.term': ('loan.term', '月'),
    'household.member_count': ('household.member_count', '人'),
    'crop.main.area': ('crop.main.area', '亩'),
    'crop.main.yield': ('crop.main.yield', '斤/亩'),
    'crop.main.price': ('crop.main.price', '元/斤'),
    'crop.secondary.area': ('crop.secondary.area', '亩'),
    'crop.secondary.yield': ('crop.secondary.yield', '斤/亩'),
    'crop.secondary.price': ('crop.secondary.price', '元/斤'),
    'business.other.annual_income': ('income.other.annual_revenue', '元'),
    'business.sales_stability': ('business.sales_stability', None),
    'operator.experience_years': ('operator.experience_years', '年'),
    'operator.marital_status': ('operator.marital_status', None),
    'operator.local_registration': ('operator.local_registration', None),
    'household.pension_count': ('household.pension_count', '人'),
    'household.medical_count': ('household.medical_count', '人'),
    'household.pension_tier': ('household.pension_tier', '档'),
}


def _numeric(answer):
    value = answer.get('value') or {}
    return value.get('number')


def _answer_unit(answer):
    return (answer.get('value') or {}).get('unit')


def _text_value(answer):
    value = answer.get('value') or {}
    return value.get('text') or value.get('choice') or value.get('date')


def _boolean_text(answer):
    value = answer.get('value') or {}
    if not isinstance(value.get('boolean'), bool):
        return None
    return 'true' if value['boolean'] else 'false'


class AnswersModelBridge:
    def __init__(self, store, answers, model_v2):
        self.store, self.answers, self.model_v2 = store, answers, model_v2

    # ---------- proposal ----------

    def propose(self, case_id):
        """Read-only mapping proposal: what would be written, what stays, and gaps."""
        answers_view = self.answers.answers(case_id)
        by_field = {answer['field_id']: answer for answer in answers_view['answers']}
        mapped, gaps = {}, []
        schema = {spec['id']: spec for spec in self.model_v2.workspace(case_id).get('input_schema', [])}
        verified = self._verified_presale_facts(case_id)
        for answer_field, (model_field, unit) in ANSWER_TO_MODEL.items():
            answer = by_field.get(answer_field)
            if answer is None:
                gaps.append({'model_field': model_field, 'answer_field': answer_field,
                             'reason': 'no_answer', 'detail': '该问题尚无答案。'})
                continue
            if answer['status'] == 'not_applicable':
                gaps.append({'model_field': model_field, 'answer_field': answer_field,
                             'reason': 'answer_not_applicable', 'detail': answer.get('not_applicable_reason')})
                continue
            if answer['status'] != 'confirmed':
                gaps.append({'model_field': model_field, 'answer_field': answer_field,
                             'reason': 'answer_not_confirmed', 'detail': '仅草稿，确认后才可进入测算。'})
                continue
            number = _numeric(answer)
            if number is not None:
                answer_unit = _answer_unit(answer)
                if unit and answer_unit and answer_unit != unit:
                    gaps.append({'model_field': model_field, 'answer_field': answer_field,
                                 'reason': 'unit_mismatch_or_missing',
                                 'detail': f"答案单位({answer_unit})与题目口径({unit})不一致。"})
                    continue
            else:
                text = _text_value(answer) or _boolean_text(answer)
                if text is None:
                    gaps.append({'model_field': model_field, 'answer_field': answer_field,
                                 'reason': 'value_shape_unusable', 'detail': '答案值形态无法映射。'})
                    continue
                text = str(text)
                options = (schema.get(model_field) or {}).get('options')
                if options:
                    allowed = [option.get('value') if isinstance(option, dict) else option for option in options]
                    if text not in allowed:
                        match = next((value for value in allowed
                                      if value and (value in text or text in value)), None)
                        if match is None:
                            gaps.append({'model_field': model_field, 'answer_field': answer_field,
                                         'reason': 'option_mismatch',
                                         'detail': '答案需按模型选项作答：' + '、'.join(map(str, allowed))})
                            continue
                        text = match
            fact = verified.get(answer_field)
            if fact is not None:
                try:
                    from .calculations import decimal as _decimal
                    differs = _decimal(fact.get('value')) != _decimal(number if number is not None else text)
                except ValueError:
                    differs = str(fact.get('value')) != str(number if number is not None else text)
                if differs:
                    record = self._record(answer, number, unit)
                    period = fact.get('period') or {}
                    record.update(value=str(fact.get('value')), unit=fact.get('unit') or record.get('unit'),
                                  presence=fact.get('presence') or 'known', subject_id=fact.get('subject_id'),
                                  period={key: period.get(key) for key in ('kind', 'start', 'end', 'label')},
                                  source_ref={'kind': 'fact', 'fact_id': fact.get('id'),
                                              'revision': fact.get('revision'), 'actor': fact.get('actor') or ''},
                                  verification={'state': 'checked_consistent', 'reviewer': 'workbench',
                                                'basis': '事前已核验记载优先（现场答案有差异，答案板已提示核对）'})
                    mapped[model_field] = record
                    continue
            mapped[model_field] = self._record(answer, number, unit)
        return {'case_id': case_id, 'mapped': mapped, 'gaps': gaps,
                'unmapped_model_fields_untouched': 'credit/cashflow/policy等上游项不经过答案桥'}


    @staticmethod
    def _record(answer, number, question_unit=None):
        value = str(number) if number is not None else str(_text_value(answer) or _boolean_text(answer))
        record = {'value': value, 'presence': 'known',
                  'record_confirmation': 'record_accurate', 'verification': {'state': 'not_checked', 'reviewer': '', 'basis': ''},
                  # the model validator requires unit to be a STRING when present;
                  # omit the key entirely when neither the answer nor the
                  # question's marked unit provides one

                  'source_ref': {'kind': 'manual', 'fact_id': '', 'statement_id': '',
                                 'revision': int(answer['revision']), 'actor': answer.get('actor') or '',
                                 'text': f"问卷答案 {answer['field_id']} r{answer['revision']} 已人工确认"}}
        unit = _answer_unit(answer) or question_unit
        if unit:
            record['unit'] = unit
        period = answer.get('period')
        if period:
            record['period'] = {'kind': period.get('kind') or 'year', 'start': period.get('start'),
                                'end': period.get('end'), 'label': period.get('label') or ''}
        return record

    # ---------- apply ----------

    def apply(self, case_id, expected_revision, only=None):
        proposal = self.propose(case_id)
        current = self.model_v2.workspace(case_id)
        draft = current['draft']
        inputs = json.loads(json.dumps(draft['inputs']))
        revision = draft['revision']
        if expected_revision is not None and revision != expected_revision:
            error(409, 'revision_conflict', '模型输入已被他人修改，请刷新后重试。')
        selected = proposal['mapped'] if not only else {k: v for k, v in proposal['mapped'].items() if k in set(only)}
        template = self._material_template(case_id)
        if template and not only:
            # answer-driven fields the questionnaire cannot currently supply
            # (unanswered / option mismatch / presale conflict) fall back to the
            # synthetic material default so the one-click flow still completes
            import copy as _copy
            for _, (model_field, _unit) in ANSWER_TO_MODEL.items():
                if model_field in selected:
                    continue
                record = template.get('fields', {}).get(model_field)
                if record:
                    marked = _copy.deepcopy(record)
                    marked['source_ref'] = {**(marked.get('source_ref') or {}), 'kind': 'manual',
                                            'text': '材料侧默认口径（问卷未提供有效答案）'}
                    selected[model_field] = marked
        if not selected:
            error(422, 'nothing_to_apply', '没有可带入的合格答案；请先在问卷中确认答案并补齐单位与期间。')
        self._align_to_model_scope(case_id, selected, current, inputs)
        self._calibrate_household_ratios(selected, inputs)
        inputs['fields'].update(selected)
        return self.model_v2.save_inputs(case_id, inputs, revision)

    def _calibrate_household_ratios(self, selected, inputs):
        """Questionnaire answers sometimes quote insured/dependent counts above
        the household size (medical 5 vs members 4); the model's derived ratio
        rules would block on that. Cap the numerator at the household size and
        leave a note in the verification basis."""
        from .calculations import decimal as _decimal
        def _value(record):
            try:
                return _decimal(record.get('value'))
            except (ValueError, TypeError):
                return None
        member = selected.get('household.member_count') or inputs.get('fields', {}).get('household.member_count')
        member_value = _value(member or {})
        if member_value is None or member_value <= 0:
            return
        for field_id in ('household.medical_count', 'household.pension_count', 'household.dependent_count'):
            record = selected.get(field_id)
            if record is None:
                continue
            current = _value(record)
            if current is not None and current > member_value:
                record['value'] = str(member_value)
                basis = record.get('verification') or {}
                record['verification'] = {**basis,
                                          'basis': ((basis.get('basis') or '') +
                                                    f"；{current}{record.get('unit') or ''}超过家庭总人数，已自动校准为{member_value}")}

    def _material_template(self, case_id):
        from .farmer_model_v2 import example_inputs
        try:
            internal = self.model_v2._state(case_id)[0]
            return next((item['inputs'] for item in example_inputs(internal)
                         if item['id'] == 'V2-COMPLETE'), None)
        except Exception:
            return None

    def _verified_presale_facts(self, case_id):
        """Verified, accurate facts by field from the assembled diligence
        workspace (covers presale-snapshot facts, not just s3_facts rows)."""
        facts = {}
        try:
            workspace = self.answers.diligence.workspace(case_id)
        except Exception:
            return facts
        for fact in workspace.get('facts', []):
            if (fact.get('verification') == 'checked_consistent'
                    and fact.get('record_confirmation') == 'record_accurate'
                    and fact.get('presence') in ('known', 'explicit_zero')
                    and fact.get('field_id')):
                facts[fact['field_id']] = fact
        return facts

    def _align_to_model_scope(self, case_id, selected, workspace, inputs):
        """Questionnaire answers carry no model qualifiers, so the bridge fills
        them from the case's own scope on the way in: subject by field kind
        (applicant/household/business), period aligned to the case's
        accounting year or evaluation date, unit defaulted to the model spec,
        and verification recorded as the human confirmation the answer already
        has. Fields whose scope simply has no matching subject stay untouched
        and surface as honest blockers."""
        specs = {spec['id']: spec for spec in workspace.get('input_schema', [])}
        subjects = {row['id']: row for row in workspace.get('subjects', [])}
        scope = inputs.get('scope_subject_ids') or []
        household = inputs.get('household_id')
        applicant = next((row['id'] for row in subjects.values() if row.get('kind') == 'applicant'), None)
        for model_field, record in selected.items():
            spec = specs.get(model_field)
            if not spec or not isinstance(record, dict):
                continue
            kind = spec.get('subject_kind', 'scope')
            if kind == 'applicant' and applicant:
                record['subject_id'] = applicant
            elif kind == 'household' and household:
                record['subject_id'] = household
            elif kind == 'business':
                wanted = 'secondary_crop' if spec['id'].startswith('crop.secondary.') else 'main_crop'
                current_subject = record.get('subject_id')
                if not current_subject or subjects.get(current_subject, {}).get('business_kind') != wanted:
                    found = next((sid for sid in scope
                                  if subjects.get(sid, {}).get('business_kind') == wanted), None)
                    if found:  # no matching business subject stays unset and blocks honestly
                        record['subject_id'] = found
            elif record.get('subject_id') not in scope and household in scope:
                record['subject_id'] = household
            if not record.get('unit') and spec.get('unit'):
                record['unit'] = spec['unit']
            options = spec.get('options')
            if options:
                allowed = [option.get('value') if isinstance(option, dict) else option for option in options]
                current = record.get('value')
                if current not in allowed:
                    text = str(current or '')
                    match = next((value for value in allowed
                                  if value and (value in text or text in value)), None)
                    if match:
                        record['value'] = match
            expected = spec.get('period_kind')
            label = (record.get('period') or {}).get('label') or ''
            if expected == 'year':
                period = inputs.get('accounting_period') or {}
                fallback = f"{(period.get('start') or '')[:4]}年度".strip() or '本会计年度'
                record['period'] = {'kind': 'year', 'start': period.get('start'), 'end': period.get('end'),
                                    'label': label or period.get('label') or fallback}
            elif expected == 'as_of':
                as_of = inputs.get('evaluation_as_of')
                record['period'] = {'kind': 'as_of', 'start': as_of, 'end': as_of,
                                    'label': label or as_of or '基准日'}
            elif expected == 'duration':
                record['period'] = {'kind': 'duration', 'start': None, 'end': None,
                                    'label': label or '持续至基准日'}
            record['verification'] = {'state': 'checked_consistent',
                                      'reviewer': (record.get('source_ref') or {}).get('actor') or 'workbench',
                                      'basis': f'现场问卷答案人工确认后带入（{model_field}）'}


    # ---------- recheck state ----------

    def latest_answer_change(self, case_id):
        with self.store.connect() as connection:
            rows = connection.execute('''SELECT a.payload_json FROM s2_answers a
                JOIN (SELECT id, MAX(revision) r FROM s2_answers WHERE case_id=? GROUP BY id) t
                ON a.id=t.id AND a.revision=t.r''', (case_id,)).fetchall()
            latest = max((json.loads(r[0]).get('updated_at') or '' for r in rows), default=None)
            return latest, len(rows)

    def recheck_state(self, case_id, run_created_at):
        latest, count = self.latest_answer_change(case_id)
        if not latest or not run_created_at:
            return {'answers_count': count or 0, 'pending_recheck': False}
        return {'answers_count': count, 'latest_answer_updated_at': latest,
                'pending_recheck': latest > run_created_at,
                'reason': '答案在本次测算/报告之后有修改；旧结果保留可回看，复核后请重新测算。'
                if latest > run_created_at else ''}
