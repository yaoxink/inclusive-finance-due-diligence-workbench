"""Case-scoped report APIs with immutable versions and permission-checked exports."""
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .diligence import DiligenceError
from .reports import ReportService


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Generate(StrictBody):
    cover: dict[str, Any] | None = None
    expected_fingerprint: StrictStr | None = Field(default=None, max_length=64)


class Edit(StrictBody):
    expected_revision: StrictInt = Field(ge=1)
    section_id: StrictStr = Field(min_length=1, max_length=80)
    text: StrictStr = Field(min_length=1, max_length=12000)
    basis: StrictStr | None = Field(default=None, max_length=2000)


class SaveCover(StrictBody):
    expected_revision: StrictInt = Field(ge=0)
    settings: dict[str, Any]


def install_report_routes(app, store, diligence, calculations=None):
    service = ReportService(store, diligence, calculations)
    app.state.reports = service
    router = APIRouter(prefix='/api/due-diligence/cases/{case_id}')

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    @router.get('/report-cover')
    def get_cover(case_id: str):
        return invoke(service.get_cover, case_id)

    @router.put('/report-cover')
    def save_cover(case_id: str, body: SaveCover):
        return invoke(service.save_cover, case_id, body.settings, body.expected_revision)

    @router.get('/reports')
    def list_reports(case_id: str):
        return invoke(service.list, case_id)

    @router.post('/reports', status_code=201)
    def generate(case_id: str, body: Generate):
        return invoke(service.generate, case_id, body.cover, body.expected_fingerprint)

    @router.get('/reports/{report_id}')
    def get_report(case_id: str, report_id: str):
        return invoke(service.get, case_id, report_id)

    @router.post('/reports/{report_id}/edits', status_code=201)
    def edit_report(case_id: str, report_id: str, body: Edit):
        return invoke(service.edit, case_id, report_id, body.expected_revision, body.section_id, body.text, body.basis)

    @router.get('/reports/{report_id}/evidence.json')
    def evidence(case_id: str, report_id: str):
        value = invoke(service.evidence, case_id, report_id)
        return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="report-evidence.json"', 'Cache-Control': 'no-store'})

    @router.get('/reports/{report_id}/export.docx')
    def export_docx(case_id: str, report_id: str):
        value = invoke(service.docx, case_id, report_id)
        return Response(value, media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        headers={'Content-Disposition': f'attachment; filename="due-diligence-v{report_id[-8:]}.docx"', 'Cache-Control': 'no-store'})

    app.include_router(router)
    return service
