import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from .diligence import DiligenceError
from .maintenance import MaintenanceService


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra='forbid')


def install_maintenance_routes(app, store, recordings):
    service = MaintenanceService(store, recordings)
    app.state.maintenance = service
    router = APIRouter(prefix='/api/due-diligence/maintenance')

    def invoke(fn, *args):
        try:
            return fn(*args)
        except DiligenceError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from exc

    @router.get('/backups')
    def backups():
        return service.list()

    @router.post('/backups', status_code=201)
    async def create(body: EmptyAction):
        async with recordings.maintenance_guard():
            return await asyncio.to_thread(invoke, service.create)

    @router.post('/backups/{identifier}/verify')
    async def verify(identifier: str, body: EmptyAction):
        async with recordings.maintenance_guard():
            return await asyncio.to_thread(invoke, service.verify, identifier)

    @router.get('/backups/{identifier}/download')
    def download(identifier: str):
        # Keep the permission check and the complete stream in the same guard as
        # deletion/restore. An old prechecked URL cannot begin streaming after a
        # deletion has committed while it waited for the event loop.
        class GuardedDownload(Response):
            async def __call__(self, scope, receive, send):
                async with recordings.maintenance_guard():
                    path=await asyncio.to_thread(invoke, service._path, identifier)
                    response=FileResponse(path,media_type='application/zip',filename=identifier+'.zip')
                    local_scope={**scope,'extensions':{k:v for k,v in scope.get('extensions',{}).items()
                        if k!='http.response.pathsend'}}
                    await response(local_scope,receive,send)
        return GuardedDownload()

    app.include_router(router)
    return service
