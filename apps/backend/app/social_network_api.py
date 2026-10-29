"""Authenticated Settings routes for the independent social module."""
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from .social_network import module_payload


class SocialOperationRequest(BaseModel):
    operation_id: UUID | None = None
    source_bucket: str = Field(default='', max_length=256)
    source_prefix: str = Field(default='aidp-social/', min_length=1, max_length=512)


def mount_social_network(app, require_admin, selected_platform_admin):
    def client_ready():
        if not app.state.settings.aidp_ready():
            raise HTTPException(503, 'AIDP workspace provisioning is not configured')
        return app.state.social_factory()

    @app.get('/api/admin/modules/ai_social_network')
    async def social_status(_admin: str = Depends(require_admin)):
        try:
            return await client_ready().status()
        except (AidpProvisionPending, AidpProvisionError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/api/admin/modules/ai_social_network/agent-endpoint')
    async def social_endpoint(_admin: str = Depends(require_admin)):
        try:
            return {'url': await client_ready().agent_endpoint()}
        except AidpProvisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (AidpProvisionPending, AidpProvisionError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post('/api/admin/users/{user_id}/modules/ai_social_network')
    @app.post('/api/admin/users/{user_id}/modules/ai_social_network/{action}')
    async def social_operation(user_id: str, payload: SocialOperationRequest, action: str = 'install',
                               _admin: str = Depends(require_admin)):
        if action not in ('install', 'redeploy'):
            raise HTTPException(404, 'Unknown social module operation')
        user = await selected_platform_admin(user_id)
        operation_id = str(payload.operation_id or uuid4())
        client = client_ready()
        try:
            return await client.operate(str(user['ocid']), operation_id, action,
                payload.source_bucket, payload.source_prefix, role_membership_verified=True)
        except AidpProvisionPending as exc:
            try:
                observed = await client.status()
            except (AidpProvisionPending, AidpProvisionError):
                observed = module_payload()
            if observed['status'] == 'not_installed':
                observed.update(status='installing' if action == 'install' else 'redeploying', installed=True, operation_id=operation_id, operation_type=action)
            return JSONResponse(status_code=202, content={**observed, 'phase': exc.phase, 'message': str(exc)})
        except AidpProvisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except AidpProvisionError as exc:
            raise HTTPException(503, str(exc)) from exc


class LocalSocialClient:
    async def close(self):
        pass

    async def status(self):
        return module_payload()

    async def agent_endpoint(self):
        raise AidpProvisionConflict('Deploy the social module in AIDP before copying its Agent URL')

    async def operate(self, *_args, **_kwargs):
        raise AidpProvisionConflict('Native AIDP is required to deploy AI Social Network')
