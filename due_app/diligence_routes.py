"""Strict S3 HTTP DTOs; the host app installs these routes explicitly."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from .diligence import DiligenceError, DiligenceService

SpeakerRole = Literal["unknown", "applicant", "household_member", "investigator", "third_party"]
AspectState = Literal["unknown", "explicit", "not_applicable"]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoadFixture(StrictBody):
    fixture_id: Literal["SYN-S0-001", "SYN-S0-002", "SYN-S0-003", "SYN-S0-004", "SYN-S0-005"]
    variant: Literal["base", "updated", "unavailable", "denied"] = "base"


class ExpectedRevision(StrictBody):
    expected_revision: StrictInt = Field(ge=1)


class ReviewAction(ExpectedRevision):
    basis: StrictStr | None = Field(default=None, max_length=2000)


class StatementCreate(StrictBody):
    text: StrictStr = Field(min_length=1, max_length=20000)
    interview_id: StrictStr | None = Field(default=None, max_length=160)
    speaker_label: StrictStr = Field(default="未知说话人", min_length=1, max_length=200)
    speaker_role: SpeakerRole = "unknown"
    subject_id: StrictStr | None = Field(default=None, max_length=160)
    subject_basis: StrictStr | None = Field(default=None, max_length=2000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("原文不能为空或包含不可见控制字符。")
        return value  # preserve original whitespace, wording and punctuation


class StatementPatch(ExpectedRevision):
    text: StrictStr = Field(min_length=1, max_length=20000)
    speaker_label: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    speaker_role: SpeakerRole | None = None
    subject_id: StrictStr | None = Field(default=None, max_length=160)
    subject_basis: StrictStr | None = Field(default=None, max_length=2000)
    _text = field_validator("text")(StatementCreate.valid_text.__func__)


class FromSegment(StrictBody):
    interview_id: StrictStr = Field(min_length=1, max_length=160)
    recording_id: StrictStr = Field(min_length=1, max_length=160)
    segment_id: StrictStr = Field(min_length=1, max_length=200)
    segment_revision: StrictInt = Field(ge=1)
    speaker_label: StrictStr = Field(default="未知说话人", min_length=1, max_length=200)
    speaker_role: SpeakerRole = "unknown"
    subject_id: StrictStr | None = Field(default=None, max_length=160)
    subject_basis: StrictStr | None = Field(default=None, max_length=2000)


class RefreshSegment(ExpectedRevision):
    segment_revision: StrictInt = Field(ge=1)


class Period(StrictBody):
    kind: Literal["year", "season", "as_of", "duration", "purchase_date"]
    label: StrictStr = Field(min_length=1, max_length=300)
    start: StrictStr | None
    end: StrictStr | None

    @model_validator(mode="after")
    def dates(self):
        for value in (self.start, self.end):
            if value is not None:
                date.fromisoformat(value)
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("期间起点不能晚于终点。")
        return self


class FactValues(StrictBody):
    statement_id: StrictStr = Field(min_length=1, max_length=160)
    statement_revision: StrictInt = Field(ge=1)
    value_type: Literal["integer", "decimal", "text", "enum"]
    value: StrictStr | None = Field(max_length=2000)
    presence: Literal["known", "explicit_zero", "missing", "unknown", "not_applicable", "withheld"]
    unit: StrictStr | None = Field(default=None, max_length=100)
    unit_status: AspectState = "unknown"
    period: Period | None = None
    period_status: AspectState = "unknown"
    subject_id: StrictStr | None = Field(default=None, max_length=160)
    subject_status: Literal["unknown", "explicit"] = "unknown"
    subject_basis: StrictStr | None = Field(default=None, max_length=2000)


class FactCreate(FactValues):
    field_id: StrictStr = Field(min_length=1, max_length=160)


class FactRevise(FactValues):
    expected_revision: StrictInt = Field(ge=1)


def install_diligence_routes(app, store, recordings):
    service = DiligenceService(store, recordings)
    app.state.diligence = service
    router = APIRouter(prefix="/api/due-diligence")

    def invoke(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc

    @router.get("/previsit/examples")
    def examples():
        return service.examples()

    @router.post("/cases/{case_id}/previsit/load")
    def load(case_id: str, body: LoadFixture):
        return invoke(service.load_previsit, case_id, body.fixture_id, body.variant)

    @router.get("/cases/{case_id}/diligence")
    def workspace(case_id: str):
        return invoke(service.workspace, case_id)

    @router.get("/cases/{case_id}/previsit/history")
    def previsit_history(case_id: str):
        return invoke(service.previsit_history, case_id)

    @router.get("/cases/{case_id}/previsit/snapshots/{snapshot_id}")
    def previsit_snapshot(case_id: str, snapshot_id: str):
        return invoke(service.previsit_snapshot, case_id, snapshot_id)

    @router.post("/cases/{case_id}/statements", status_code=201)
    def create_statement(case_id: str, body: StatementCreate):
        return invoke(service.create_statement, case_id, body.model_dump())

    @router.post("/cases/{case_id}/statements/from-segment", status_code=201)
    def segment(case_id: str, body: FromSegment):
        return invoke(service.statement_from_segment, case_id, body.model_dump())

    @router.get("/cases/{case_id}/statements/{statement_id}")
    def statement(case_id: str, statement_id: str):
        return invoke(service.statement, case_id, statement_id)

    @router.patch("/cases/{case_id}/statements/{statement_id}")
    def revise_statement(case_id: str, statement_id: str, body: StatementPatch):
        return invoke(service.revise_statement, case_id, statement_id, body.model_dump(exclude_unset=True))

    @router.post("/cases/{case_id}/statements/{statement_id}/confirm")
    def confirm_statement(case_id: str, statement_id: str, body: ExpectedRevision):
        return invoke(service.confirm_statement, case_id, statement_id, body.expected_revision)

    @router.post("/cases/{case_id}/statements/{statement_id}/refresh-segment")
    def refresh_segment(case_id: str, statement_id: str, body: RefreshSegment):
        return invoke(service.refresh_segment, case_id, statement_id, body.expected_revision, body.segment_revision)

    @router.get("/cases/{case_id}/statements/{statement_id}/history")
    def statement_history(case_id: str, statement_id: str):
        return invoke(service.statement_history, case_id, statement_id)

    @router.post("/cases/{case_id}/facts", status_code=201)
    def create_fact(case_id: str, body: FactCreate):
        return invoke(service.create_fact, case_id, body.model_dump())

    @router.get("/cases/{case_id}/facts/{fact_id}")
    def fact(case_id: str, fact_id: str):
        return invoke(service.fact, case_id, fact_id)

    @router.post("/cases/{case_id}/facts/{fact_id}/revise")
    def revise_fact(case_id: str, fact_id: str, body: FactRevise):
        return invoke(service.revise_fact, case_id, fact_id, body.model_dump())

    @router.post("/cases/{case_id}/facts/{fact_id}/confirm")
    def confirm_fact(case_id: str, fact_id: str, body: ReviewAction):
        return invoke(service.confirm_fact, case_id, fact_id, body.expected_revision, body.basis)

    @router.post("/cases/{case_id}/facts/{fact_id}/revoke")
    def revoke_fact(case_id: str, fact_id: str, body: ReviewAction):
        return invoke(service.revoke_fact, case_id, fact_id, body.expected_revision, body.basis)

    @router.get("/cases/{case_id}/facts/{fact_id}/history")
    def fact_history(case_id: str, fact_id: str):
        return invoke(service.fact_history, case_id, fact_id)

    app.include_router(router)
    return service
