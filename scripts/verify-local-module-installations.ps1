param([Parameter(Mandatory)][string]$ContainerName)

# ponytail: reuse the native read-only journals before restarting a local portal.
$statusCheck = @'
import asyncio, json
from app.config import Settings
from app.aidp import AidpClient
from app.gods_eye_view.installation import ModuleInstallation
try:
    settings = Settings.from_env()
    assert settings.portal_managed_modules and not settings.gods_eye_view_local_mode
    client = AidpClient(settings)
    state = ModuleInstallation(settings, lambda: client, None).read()
    modules = asyncio.run(client.list_modules())
    statuses = {'gods_eye_view': state.get('status', 'available'), 'governance': [item.get('status') for item in modules]}
    print(json.dumps(statuses))
    assert statuses['gods_eye_view'] != 'activating'
    assert not any(item in {'installing', 'redeploying', 'deleting'} for item in statuses['governance'])
    print('No active module installation. Read-only status checks passed.')
except Exception as exc:
    print('Status check blocked: ' + type(exc).__name__)
    raise SystemExit(1)
'@
docker exec $ContainerName python -c $statusCheck
if ($LASTEXITCODE -ne 0) { throw 'Local portal restart blocked: module installation status is active or unverified.' }
