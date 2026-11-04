"""Administrator-only installation and observed progress; GET never provisions."""
import json
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from oci._vendor.requests.exceptions import RequestException

from .aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending
from .market_intelligence import public_status
from .identity import IdentityPending


class MarketInstallRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    reuse_existing: bool = False
    apify_token: SecretStr | None = None
    test_session_id: str | None = None
    runtime_version: Literal['legacy', 'v2'] = 'legacy'

    def validate_values(self):
        if str(UUID(self.operation_id)) != self.operation_id:
            raise ValueError('Invalid operation')
        if self.test_session_id and str(UUID(self.test_session_id)) != self.test_session_id:
            raise ValueError('Invalid session')
        if self.apify_token:
            value = self.apify_token.get_secret_value()
            if not 20 <= len(value) <= 512 or any(ord(char) <= 32 for char in value):
                raise ValueError('Invalid secret')


class MarketReinstallRequest(MarketInstallRequest):
    previous_operation_id: str
    confirmation: str

    def validate_values(self):
        super().validate_values()
        if (self.confirmation != 'ai_market_intelligence' or self.reuse_existing is not False
                or str(UUID(self.previous_operation_id)) != self.previous_operation_id
                or self.operation_id == self.previous_operation_id):
            raise ValueError('Invalid reinstall scope')


def mount_market_intelligence(app, require_admin, selected_platform_admin):
    def ready(runtime_version='legacy'):
        if not app.state.settings.aidp_ready():
            raise HTTPException(503, 'AIDP workspace provisioning is not configured')
        return app.state.market_v2_factory() if runtime_version == 'v2' else app.state.market_factory()

    @app.get('/api/admin/modules/ai_market_intelligence')
    async def observe(runtime_version: Literal['legacy', 'v2'] = 'legacy', _admin=Depends(require_admin)):
        try:
            result = await ready(runtime_version).status()
            owner = result.pop('administrator_ocid', None)
            if owner:
                try:
                    users = await app.state.identity_factory().list_users_by_ocids({owner})
                    matches = [user for user in users if user.get('ocid') == owner and user.get('id')]
                    if len(matches) == 1:
                        result['administrator_id'] = matches[0]['id']
                except (IdentityPending, RequestException):
                    pass
            return result
        except (AidpProvisionError, AidpProvisionPending, AidpProvisionConflict):
            raise HTTPException(503, 'Unable to read the native installation. No installation was started.') from None

    @app.post('/api/admin/users/{user_id}/modules/ai_market_intelligence')
    async def install(user_id: str, request: Request, _admin=Depends(require_admin)):
        user = await selected_platform_admin(user_id)
        try:
            # Validation failures must not echo a submitted token in FastAPI's 422 body.
            if len(await request.body()) > 8192:
                raise ValueError('Oversized settings')
            values = MarketInstallRequest.model_validate(await request.json())
            values.validate_values()
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(400, 'Invalid installation settings. Check the operation, test session and credential selection.') from None
        try:
            result = await ready(values.runtime_version).operate(str(user['ocid']), values.operation_id,
                reuse_existing=values.reuse_existing, token=values.apify_token, test_session_id=values.test_session_id)
            return JSONResponse(result, status_code=202 if result['status'] == 'installing' else 200)
        except AidpProvisionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (AidpProvisionError, AidpProvisionPending):
            raise HTTPException(503, 'Native setup is unavailable. Preserve this operation and retry its status.') from None

    @app.get('/api/admin/modules/ai_market_intelligence/agent-endpoint')
    async def endpoint(runtime_version: Literal['legacy', 'v2'] = 'legacy', _admin=Depends(require_admin)):
        try:
            return {'url': await ready(runtime_version).agent_endpoint(), 'runtime_version': runtime_version,
                    'deployment_surface': 'TEST' if runtime_version == 'v2' else 'PROD'}
        except AidpProvisionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (AidpProvisionError, AidpProvisionPending):
            raise HTTPException(503, 'Unable to verify the native agent endpoint.') from None

    @app.post('/api/admin/users/{user_id}/modules/ai_market_intelligence/reinstall')
    async def reinstall(user_id: str, request: Request, _admin=Depends(require_admin)):
        user = await selected_platform_admin(user_id)
        try:
            if len(await request.body()) > 8192:
                raise ValueError('Oversized settings')
            values = MarketReinstallRequest.model_validate(await request.json())
            values.validate_values()
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(400, 'Confirm the project removal, previous operation and new token before reinstalling.') from None
        try:
            result = await ready(values.runtime_version).reinstall(str(user['ocid']), values.operation_id, values.previous_operation_id,
                                            token=values.apify_token, test_session_id=values.test_session_id)
            return JSONResponse(result, status_code=202 if result['status'] == 'redeploying' else 200)
        except AidpProvisionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (AidpProvisionError, AidpProvisionPending):
            raise HTTPException(503, 'Native reinstall is unavailable. Preserve this operation and retry its status.') from None


class LocalMarketClient:
    def __init__(self, *, runtime_version='legacy'):
        self.runtime_version = runtime_version

    async def close(self):
        pass

    async def status(self):
        return {**public_status({'runtime_version': self.runtime_version}), 'apify_credential_available': False}

    async def operate(self, *_args, **_kwargs):
        raise AidpProvisionConflict('Native AIDP is required; the local fixture does not create a simulated installation.')

    async def agent_endpoint(self):
        raise AidpProvisionConflict('A native installed agent is required.')

    async def reinstall(self, *_args, **_kwargs):
        raise AidpProvisionConflict('Native AIDP is required; the local fixture cannot reinstall a project.')
