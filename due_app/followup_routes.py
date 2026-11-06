"""S4 strict, case-scoped API. The host explicitly installs it."""
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .diligence import DiligenceError
from .followups import FollowupService


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Analyze(StrictBody):
    expected_fingerprint: StrictStr | None = Field(default=None, max_length=64)
    retry_ai: StrictBool = False


class Action(StrictBody):
    expected_revision: StrictInt = Field(ge=1)
    action: Literal["accept", "edit", "defer", "ignore"]
    text: StrictStr | None = Field(default=None, max_length=600)
    basis: StrictStr | None = Field(default=None, max_length=2000)


class AISettings(StrictBody):
    enabled: StrictBool
    expected_revision: StrictInt = Field(ge=0)


def _drop_questionnaire_gaps(result, store=None, case_id=None):
    """Presentation filter enforcing question-set independence:
    1. plain missing-item reminders duplicate the onsite question board;
    2. any card whose field already has a presale answer (pre-visit fact),
       an onsite answer, or an extraction candidate is not a suggestion —
       that field is already owned by the prepare or onsite tab.
    Accepted cards stay visible so the person can still ignore them later.
    Stored evidence cards are untouched; only this HTTP view hides."""
    if not isinstance(result, dict) or not isinstance(result.get('cards'), list):
        return result
    covered = set()
    if store is not None and case_id:
        try:
            with store.connect() as connection:
                # HARD DEDUP RULE — a follow-up card is hidden when
                # a) it is a plain missing-item card (its wording IS the
                #    questionnaire question), or
                # b) its field already has an onsite answer or an extraction
                #    candidate (the question board already owns it).
                # Clarification cards on presale facts stay: their wording is a
                # follow-up question, never the original questionnaire text.
                covered |= {row[0] for row in connection.execute(
                    'SELECT DISTINCT field_id FROM s2_answers WHERE case_id=?', (case_id,))}
                covered |= {row[0] for row in connection.execute(
                    "SELECT DISTINCT json_extract(payload_json,'$.field_id') FROM s4x_candidates WHERE case_id=?",
                    (case_id,)) if row[0]}
        except Exception:
            covered = set()
    kept, hidden = [], 0
    for card in result['cards']:
        if card.get('rule') == 'missing':
            hidden += 1
            continue
        if (card.get('rule') not in ('conflict', 'record_pending')
                and card.get('status') != 'accepted' and card.get('field_id') in covered):
            hidden += 1
            continue
        kept.append(card)
    # HARD RULE: the panel renders active_cards too; the same dedup must apply
    # there or questionnaire questions leak back through this second set.
    active = result.get('active_cards')
    if isinstance(active, list):
        exempt = ('conflict', 'record_pending')  # new information even when answered
        kept_active = [card for card in active
                       if card.get('rule') != 'missing'
                       and (card.get('rule') in exempt
                            or not (card.get('status') != 'accepted' and card.get('field_id') in covered))]
        hidden += len(active) - len(kept_active)
        result = {**result, 'active_cards': kept_active}
    if hidden:
        result = {**result, 'cards': kept, 'hidden_questionnaire_gaps': hidden}
    return result


def install_followup_routes(app, store, diligence, adapter=None):
    service = FollowupService(store, diligence, adapter=adapter)
    app.state.followups = service
    router = APIRouter(prefix="/api/due-diligence/cases/{case_id}/followups")

    def invoke(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc

    @router.get("")
    def listing(case_id: str):
        result = invoke(service.list, case_id)
        return _drop_questionnaire_gaps(result, store, case_id)

    @router.post("/analyze")
    def analyze(case_id: str, body: Analyze):
        result = invoke(service.analyze, case_id, body.expected_fingerprint, body.retry_ai)
        return _drop_questionnaire_gaps(result, store, case_id)

    @router.post("/analyze")
    def analyze(case_id: str, body: Analyze):
        return invoke(service.analyze, case_id, body.expected_fingerprint, body.retry_ai)

    @router.get("/ai-settings")
    def settings(case_id: str):
        return invoke(service.settings, case_id)

    @router.post("/ai-settings")
    def configure(case_id: str, body: AISettings):
        return invoke(service.configure, case_id, body.enabled, body.expected_revision)

    @router.post("/{card_id}/actions")
    def act(case_id: str, card_id: str, body: Action):
        return invoke(service.action, case_id, card_id, body.expected_revision, body.action, body.text, body.basis)

    @router.get("/events")
    def events(case_id: str):
        return invoke(service.events, case_id)

    app.include_router(router)
    return service
