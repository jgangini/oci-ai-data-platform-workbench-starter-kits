"""Retired routes remain authenticated and cannot operate legacy resources."""
from fastapi import Depends, HTTPException


def mount_social_network(app, require_admin):
    @app.get('/api/admin/modules/ai_social_network')
    @app.get('/api/admin/modules/ai_social_network/agent-endpoint')
    @app.post('/api/admin/users/{user_id}/modules/ai_social_network')
    @app.post('/api/admin/users/{user_id}/modules/ai_social_network/{action}')
    async def retired(_admin=Depends(require_admin)):
        raise HTTPException(410, 'AI Social Network has been retired. Install AI Market Intelligence separately; no legacy resources were changed.')
