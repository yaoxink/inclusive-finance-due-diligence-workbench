"""Master questionnaire administration: view onsite questions, flip
required/disabled flags, and push the edited master into existing case
snapshots. Admin-only surface — the same fixed local policy as the rest of
the app applies."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .diligence import DiligenceError


class FieldPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    required: bool | None = None
    disabled: bool | None = None


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fields: dict[str, FieldPatch]


def install_questionnaire_routes(app, store, diligence):
    router = APIRouter(prefix='/api/due-diligence/questionnaire')

    def invoke(fn, *args):
        try:
            return fn(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    @router.get('')
    def view():
        return {'version': diligence.questionnaire.get('version'),
                'fields': [{'field_id': f['field_id'], 'label': f.get('label') or f.get('question_text'),
                            'question_text': f.get('question_text'), 'section': f.get('section'),
                            'handling': f.get('handling'), 'required': bool(f.get('required')),
                            'disabled': bool(f.get('disabled')), 'value_type': f.get('value_type'),
                            'unit': f.get('default_unit')}
                           for f in diligence.questionnaire['fields']
                           if f.get('handling') in ('interview', 'extension_required')
                           and not f['field_id'].startswith(('derived.', 'output.'))]}

    @router.put('')
    def update(body: UpdateBody):
        patches = {field_id: {k: v for k, v in patch.model_dump().items() if v is not None}
                   for field_id, patch in body.fields.items()}
        return invoke(diligence.update_questionnaire_fields, patches)

    @router.post('/apply')
    def apply_to_cases():
        return invoke(diligence.apply_questionnaire_to_cases)

    app.include_router(router)
