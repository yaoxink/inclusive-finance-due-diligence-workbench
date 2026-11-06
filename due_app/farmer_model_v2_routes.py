"""Strict, case-scoped APIs for the versioned farmer assessment model."""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .diligence import DiligenceError
from .farmer_model_v2 import FarmerModelV2Service


class InputsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=0)
    inputs: dict[str, Any]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=1)


def install_farmer_v2_routes(app, store, diligence, legacy=None):
    service = FarmerModelV2Service(store, diligence)
    app.state.farmer_model_v2 = service
    if legacy is not None:
        legacy.v2 = service
    router = APIRouter(prefix='/api/due-diligence/cases/{case_id}/calculation/v2')

    def invoke(method, *args):
        try:
            return method(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc
        except ValueError as exc:
            raise HTTPException(422, {'code': 'model_v2_invalid', 'message': str(exc)}) from exc

    @router.get('')
    def workspace(case_id: str):
        result = invoke(service.workspace, case_id)
        bridge = getattr(app.state, 'answers_bridge', None)
        if bridge is not None:
            latest_run = result.get('latest_run') or {}
            result['answers_recheck'] = bridge.recheck_state(case_id, latest_run.get('created_at'))
        return result

    @router.get('/inputs')
    def inputs(case_id: str):
        return invoke(service.workspace, case_id)

    @router.get('/examples')
    def examples(case_id: str):
        return invoke(service.examples, case_id)

    @router.get('/inputs/history')
    def input_history(case_id: str):
        return invoke(service.input_history, case_id)

    @router.put('/inputs')
    def save_inputs(case_id: str, body: InputsRequest):
        return invoke(service.save_inputs, case_id, body.inputs, body.expected_revision)

    class FillBody(BaseModel):
        model_config = ConfigDict(extra='forbid')
        overwrite: bool = False

    @router.post('/fill-from-example')
    def fill_from_example(case_id: str, body: FillBody):
        return invoke(service.fill_missing_from_example, case_id, body.overwrite)

    @router.post('/run', status_code=201)
    def run(case_id: str, body: RunRequest):
        return invoke(service.run, case_id, body.expected_revision)

    @router.get('/runs/{run_id}')
    def detail(case_id: str, run_id: str):
        return invoke(service.run_detail, case_id, run_id)

    app.include_router(router)
    return service


install_farmer_model_v2_routes = install_farmer_v2_routes
