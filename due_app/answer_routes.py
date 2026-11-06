"""HTTP routes for P2 customer info, answers and progress.

Mounted under /api/due-diligence/cases/{case_id}. Local-only middleware and
case scoping come from the app; every handler is case-scoped and cross-case
ids are rejected by the service layer.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .answers import AnswerService
from .diligence import DiligenceError


class Free(BaseModel):
    """Answers carry structured payloads validated by the service layer; the
    HTTP envelope only forbids unspecified extra noise at the top level."""
    model_config = ConfigDict(extra='allow')


def install_answer_routes(app, store, diligence):
    service = AnswerService(store, diligence)
    app.state.answers = service
    router = APIRouter(prefix='/api/due-diligence')

    def invoke(fn, *args):
        try:
            return fn(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    @router.get('/cases/{case_id}/customer-info')
    def get_customer_info(case_id: str):
        return invoke(service.customer_info, case_id)

    @router.put('/cases/{case_id}/customer-info')
    def put_customer_info(case_id: str, body: Free):
        return invoke(service.update_customer_info, case_id, body.model_dump())

    @router.get('/cases/{case_id}/answers')
    def list_answers(case_id: str):
        return invoke(service.answers, case_id)

    @router.post('/cases/{case_id}/answers', status_code=201)
    def create_answer(case_id: str, body: Free):
        return invoke(service.create_answer, case_id, body.model_dump())

    @router.get('/cases/{case_id}/answers/{answer_id}')
    def get_answer(case_id: str, answer_id: str):
        return invoke(service.answer, case_id, answer_id)

    @router.get('/cases/{case_id}/answers/{answer_id}/history')
    def answer_history(case_id: str, answer_id: str):
        return invoke(service.answer_history, case_id, answer_id)

    @router.put('/cases/{case_id}/answers/{answer_id}')
    def revise_answer(case_id: str, answer_id: str, body: Free):
        return invoke(service.revise_answer, case_id, answer_id, body.model_dump())

    @router.post('/cases/{case_id}/answers/{answer_id}/items/{item_id}/revoke')
    def revoke_item(case_id: str, answer_id: str, item_id: str, body: Free):
        return invoke(service.revoke_item, case_id, answer_id, item_id, body.model_dump())

    @router.post('/cases/{case_id}/answers/{answer_id}/items/{item_id}/reopen')
    def reopen_item(case_id: str, answer_id: str, item_id: str, body: Free):
        return invoke(service.reopen_item, case_id, answer_id, item_id, body.model_dump())

    @router.get('/cases/{case_id}/progress')
    def progress(case_id: str):
        return invoke(service.progress, case_id)

    @router.get('/cases/{case_id}/answer-board')
    def answer_board(case_id: str):
        return invoke(service.board, case_id)

    @router.post('/cases/{case_id}/answers/adopt-candidate')
    def adopt_candidate(case_id: str, body: Free):
        return invoke(service.adopt_candidate, case_id, body.model_dump())

    app.include_router(router)
    return service


def install_extraction_routes(app, store, diligence):
    from .extraction import AnswerExtractionService
    service = AnswerExtractionService(store, diligence)
    app.state.extraction = service
    router = APIRouter(prefix='/api/due-diligence')

    def invoke(fn, *args):
        try:
            return fn(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    @router.post('/cases/{case_id}/extraction/run')
    def run_extraction(case_id: str, body: Free):
        force = bool(body.model_dump().get('force'))
        return invoke(lambda: service.run(case_id, force=force))

    @router.get('/cases/{case_id}/extraction/runs')
    def extraction_runs(case_id: str):
        return invoke(service.runs, case_id)

    @router.get('/cases/{case_id}/extraction/candidates')
    def extraction_candidates(case_id: str):
        return invoke(service.candidates, case_id)

    @router.get('/cases/{case_id}/extraction/candidates/{candidate_id}')
    def extraction_candidate(case_id: str, candidate_id: str):
        return invoke(service.candidate, case_id, candidate_id)

    class AdoptSuggestion(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        suggestion_id: str = Field(min_length=8, max_length=64)

    @router.post('/cases/{case_id}/extraction/suggestions/adopt')
    def adopt_suggestion(case_id: str, body: AdoptSuggestion):
        return invoke(service.adopt_suggestion, case_id, body.suggestion_id)

    @router.post('/cases/{case_id}/extraction/candidates/adopt-all')
    def adopt_all_candidates(case_id: str):
        view = invoke(service.candidates, case_id)
        return invoke(app.state.answers.adopt_candidates_bulk, case_id, view['candidates'])

    app.include_router(router)
    return service


def install_answer_bridge_routes(app, store, answers, model_v2):
    """P5: answer -> model-v2 input mapping, mounted with the calculation API."""
    from .answers_model_bridge import AnswersModelBridge
    bridge = AnswersModelBridge(store, answers, model_v2)
    app.state.answers_bridge = bridge
    router = APIRouter(prefix='/api/due-diligence')

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    class FromAnswersRequest(BaseModel):
        model_config = ConfigDict(extra='forbid', strict=True)
        expected_revision: int = -1
        only: list[str] | None = None

    @router.get('/cases/{case_id}/calculation/v2/answers-bridge')
    def bridge_proposal(case_id: str):
        proposal = invoke(bridge.propose, case_id)
        latest = None
        with store.connect() as connection:
            row = connection.execute('SELECT created_at FROM s5v2_runs WHERE case_id=? '
                                     'ORDER BY created_at DESC LIMIT 1', (case_id,)).fetchone()
            latest = row[0] if row else None
        proposal['recheck'] = bridge.recheck_state(case_id, latest)
        return proposal

    @router.post('/cases/{case_id}/calculation/v2/from-answers')
    def bridge_apply(case_id: str, body: FromAnswersRequest):
        expected = body.expected_revision if body.expected_revision >= 0 else None
        return invoke(bridge.apply, case_id, expected, body.only)

    app.include_router(router)
    return bridge
