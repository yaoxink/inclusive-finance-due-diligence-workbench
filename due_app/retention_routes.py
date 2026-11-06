import asyncio

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .diligence import DiligenceError
from .retention import RetentionService


class DeleteConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm_title: str = Field(min_length=1,max_length=160)


class EmptyRetentionAction(BaseModel):
    model_config = ConfigDict(extra='forbid')


def install_retention_routes(app,store,recordings,maintenance=None,clock=None):
    service=RetentionService(store,recordings,clock)
    app.state.retention=service
    if maintenance is not None: maintenance.retention=service
    router=APIRouter(prefix='/api/due-diligence')

    def invoke(fn,*args):
        try: return fn(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status,{'code':exc.code,'message':exc.message}) from exc

    @router.get('/cases/{case_id}/deletion-preview')
    def preview(case_id:str): return invoke(service.preview,case_id)

    @router.post('/cases/{case_id}/delete')
    async def delete(case_id:str,body:DeleteConfirmation,response:Response):
        async with recordings.maintenance_guard():
            result=await asyncio.to_thread(invoke,service.delete,case_id,body.expected_fingerprint,body.confirm_title)
        if result['state']=='cleanup_pending': response.status_code=202
        return result

    @router.get('/retention/status')
    def status(): return invoke(service.status)

    @router.post('/retention/cleanup')
    async def cleanup(body:EmptyRetentionAction):
        async with recordings.maintenance_guard():
            return await asyncio.to_thread(invoke,service.cleanup)

    app.include_router(router)
    return service
