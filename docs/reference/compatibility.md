# Compatibility contracts

[Documentation](../README.md) · [God’s Eye View](../modules/gods-eye-view.md) ·
[Operations](../operations.md)

User-facing naming is God’s Eye View. The implementation uses `territorial`;
selected `prisma` identities remain compatibility contracts. Renaming source
files does not migrate cloud data, remove credentials or retire resources.

## Routes and identity during rolling upgrades

| Current interface | Retained interface |
| --- | --- |
| `/admin/gods-eye-view` | `/admin/prisma` redirects. |
| `/gods-eye-view/` | `/prisma` and `/prisma/*` redirect. |
| `/local/gods-eye-view/login`, `/local/gods-eye-view/workspace` | Corresponding `/local/prisma/*` redirects. |
| `/api/territorial/*`, `/api/admin/territorial/*` | Authenticated `/api/prisma/*`, `/api/admin/prisma/*` aliases. |
| `X-Territorial-User` | `X-PRISMA-User` remains supported. |

Both [production](../../docker/nginx.conf) and
[local ingress](../../docker/nginx.oci-local.conf) rewrite canonical viewer API
requests to the legacy wire prefix, including voice. Authentication, query
strings and body limits remain enforced. The new viewer accepts both prefixes;
its [backend bridge](../../apps/territorial-viewer/server.py) likewise uses the
legacy wire path so an older administration image remains usable. Only a missing
identity endpoint receives default branding; other failures are not hidden.

Local login continues to sign `local-prisma` subjects for rollback compatibility.
Readers accept transitional `local-territorial` subjects too; session responses
normalize both identity headers to the legacy wire subject. Proxies overwrite
these headers after authentication. Neither header grants anonymous access.
See [session boundaries](../../apps/backend/app/territorial/access.py).

## Configuration, packaging and infrastructure

`TERRITORIAL_*` environment variables take precedence over `PRISMA_*` aliases.
Cloud-init writes both names with equal values so an older image can start after
rollback. Do not remove legacy variables while older images remain supported.

Terraform resource addresses use `territorial` with explicit `moved` blocks;
existing physical OCI names and restricted storage policies remain stable.
`enable_territorial_viewer`, when supplied, overrides `enable_prisma_viewer`.
The Deploy Studio manifest deliberately keeps serialized field name
`enable_prisma_viewer`: adding a new default-false field would otherwise override
saved enabled deployments. Canonical and legacy viewer outputs remain available.
See [Terraform compatibility](../../terraform/g_territorial_viewer.tf),
[variables](../../terraform/b_variables.tf) and [manifest](../../terraform/deploy-studio.json).

The development Compose service is `territorial-viewer`; private overlays must
use that service name while preserving the existing cache volume. Deployed
container names, `/opt/prisma`, update command and lock remain rollback contracts.
Releases provide canonical and legacy viewer asset names; image archives are
byte-identical aliases and manifests retain their corresponding image tags.
See [release packaging](../../.github/workflows/release.yml).

Python source uses `app.territorial`. The
[`app.prisma` namespace shim](../../apps/backend/app/prisma/__init__.py) redirects
legacy module imports. Compatibility runtime archives also cover existing
`from prisma.pipeline import run` notebooks. This is not a promise of a
`PrismaAgent` class alias: older standalone agent deployments retain their own
embedded class; new source uses the current agent contract.

## Persisted data and native resources

Preserve the governed `prisma_ingest` and Delta table/storage identities, Landing
prefixes, `04_gold/prisma/*`, `.control/prisma/agent.json` and file-stream
checkpoints. Legacy `PRISMA_*` database objects remain the source until explicit
control/history migration and acceptance; a rename does not migrate them. Local `prisma.sqlite3`, `prisma-secrets`,
`prisma-landing`, `.local/prisma` and cache volumes also retain their identities.
An API or display-name change must not copy, reset or delete these stores.

Canonical workflows adopt recognized legacy jobs by ID. Their task keys are
`social_network` and `sensor_stream`; scheduling still recognizes `prisma_tick`
and preserves its `prisma-run` idempotency namespace. Old workflow names include
`prisma_bogota_tick`, `prisma_colombia_sensors` and their `territorial_*` successors.
The [installer](../../terraform/hooks/territorial_bootstrap.py) rejects ambiguous
matches instead of creating a second managed workflow.

The new Workspace target is `/Workspace/medallion/gods_eye_view`, with readable
standalone Python files. Older `/Workspace/medallon/prisma` and
`/Workspace/territorial` artifacts are not deleted by that change. Retire an
artifact only after checking job, deployment and installed-library references.
Preserve existing agent memory namespaces, including `prisma_bogota`, until an
explicit migration defines their replacement.

The new control path uses `.control/gods_eye_view/` Object documents and an
immutable post journal. Its VM SQLite projection is rebuildable. The
[database facade](../../apps/backend/app/territorial/database.py) retains Oracle
procedures for legacy migration, but native streams export only the Object
implementation. There is no automatic fallback from missing Object state to an
Oracle connection. The explicit [migration procedure](../operations.md#migrate-gods-eye-view-controls)
preserves document revisions, post sequence bounds and cancellation receipts.

`AidpRuntime` is the preferred OCI-only credential, with the supported Governance
OCI credential as a compatibility choice. Database-bearing writer names,
including `AidpControlStore`, are not runtime API authentication fallbacks.
Duplicate or invalid preferred credentials fail instead of selecting another
identity. Remove old module database credentials only after native consumers
and migrated history are accepted; unrelated platform consumers remain in scope
for a separate review. See [credential selection](../../apps/backend/app/territorial/runtime_secrets.py).

Legacy `simulation` input is normalized to `Synthetic`; historical records and
the `simulation` control document remain compatible. Reset recognizes both
values. Family-scoped sensor resets retain their original operation IDs and
scope; global reset never widens them. Cancelled IDs remain terminal.

Native Gold-agent acceptance, Object control activation, production pointer
cutover and legacy resource/credential retirement are separate operational gates.
None is implied by source compatibility, an active deployment or a local test.
