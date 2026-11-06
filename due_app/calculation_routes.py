"""Strict case-scoped S5 reference calculation endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .calculations import CalculationService
from .diligence import DiligenceError


class EmptyRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class FixtureRequest(EmptyRequest):
    fixture_id: StrictStr=Field(min_length=1,max_length=100)


class DecisionReview(EmptyRequest):
    note: StrictStr=Field(min_length=1,max_length=4000)


def install_calculation_routes(app,store,diligence):
    service=CalculationService(store,diligence)
    app.state.calculations=service
    router=APIRouter(prefix='/api/due-diligence/cases/{case_id}/calculation')

    def invoke(method,*args):
        try: return method(*args)
        except DiligenceError as exc: raise HTTPException(exc.status,{'code':exc.code,'message':exc.message}) from exc
        except ValueError as exc: raise HTTPException(422,{'code':'calculation_invalid','message':str(exc)}) from exc

    @router.get('')
    def workspace(case_id:str): return invoke(service.workspace,case_id)

    @router.get('/model')
    def model(case_id:str): return invoke(service.model_view,case_id)

    @router.post('/run',status_code=201)
    def run(case_id:str,body:EmptyRequest): return invoke(service.run,case_id)

    @router.post('/fixture',status_code=201)
    def fixture(case_id:str,body:FixtureRequest): return invoke(service.run,case_id,body.fixture_id)

    @router.get('/runs/{run_id}')
    def detail(case_id:str,run_id:str): return invoke(service.run_detail,case_id,run_id)

    @router.post('/decisions/{decision_id}/review')
    def review(case_id:str,decision_id:str,body:DecisionReview): return invoke(service.review_decision,case_id,decision_id,body.note)

    app.include_router(router)
    return service
