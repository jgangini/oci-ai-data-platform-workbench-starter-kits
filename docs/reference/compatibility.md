# Compatibility contracts

[Documentation](../README.md) · [God’s Eye View](../modules/gods-eye-view.md) ·
[Operations](../operations.md)

The project is **God’s Eye View**: `gods_eye_view` in Python and
`gods-eye-view` in browser paths and the viewer directory. AI Data Governance
uses `ai_data_governance` in its package, catalog and module identity. Old names
below identify existing installation contracts, not active source packages.
Renaming source files does not migrate cloud data or retire resources.

## Routes and identity during rolling upgrades

| Current interface | Retained interface |
| --- | --- |
| `/admin/gods-eye-view` | `/admin/prisma` redirects. |
| `/gods-eye-view/` | `/prisma` and `/prisma/*` redirect. |
| `/local/gods-eye-view/login`, `/local/gods-eye-view/workspace` | Corresponding `/local/prisma/*` redirects. |
| `/api/gods-eye-view/*`, `/api/admin/gods-eye-view/*` | Authenticated `/api/territorial/*`, `/api/admin/territorial/*`, `/api/prisma/*` and `/api/admin/prisma/*` aliases. |
| `X-Gods-Eye-View-User` | `X-Territorial-User` and `X-PRISMA-User` remain supported. |

Both [production](../../docker/nginx.conf) and
[local ingress](../../docker/nginx.oci-local.conf) rewrite canonical viewer API
requests to the legacy wire prefix, including voice. Authentication, query
strings and body limits remain enforced. The new viewer accepts both prefixes;
its [backend bridge](../../apps/gods-eye-view/server.py) likewise uses the
legacy wire path so an older administration image remains usable. Only a missing
identity endpoint receives default branding; other failures are not hidden.

Local login continues to sign `local-prisma` subjects for rollback compatibility.
Readers also accept `local-gods-eye-view` and transitional `local-territorial`
subjects. Session responses preserve the canonical subject in the new header
and normalize the two legacy headers for older viewers. Proxies overwrite
these headers after authentication; none grants anonymous access.
See [session boundaries](../../apps/backend/app/gods_eye_view/access.py).

## Configuration, packaging and infrastructure

Canonical environment variables take precedence over the retained aliases:

| Current setting | Legacy settings, in precedence order |
| --- | --- |
| `GODS_EYE_VIEW_ENABLED` | `TERRITORIAL_VIEWER_ENABLED`, `PRISMA_VIEWER_ENABLED` |
| `GODS_EYE_VIEW_URL` | `TERRITORIAL_VIEWER_URL`, `PRISMA_VIEWER_URL` |
| Other `GODS_EYE_VIEW_*` settings | Corresponding `TERRITORIAL_*`, then `PRISMA_*` |

Cloud-init retains equivalent legacy values so an older image can start after
rollback. `GODS_EYE_CONTROL_BUCKET` remains the explicit Object runtime bucket
setting; it is not inferred from Landing.

Terraform resource addresses use `gods_eye_view` with explicit `moved` blocks;
existing physical OCI names and restricted storage policies remain stable.
`enable_gods_eye_view`, when supplied, overrides `enable_territorial_viewer`
and then `enable_prisma_viewer`.
The Deploy Studio manifest deliberately keeps serialized field name
`enable_prisma_viewer`: adding a new default-false field would otherwise override
saved enabled deployments. Canonical and legacy viewer outputs remain available.
See [Terraform compatibility](../../terraform/h_gods_eye_view.tf),
[variables](../../terraform/b_variables.tf) and [manifest](../../terraform/deploy-studio.json).

The development Compose service is `gods-eye-view`; private overlays must
use that service name while preserving the existing cache volume. Deployed
container names, `/opt/prisma`, update command and lock remain rollback contracts.
Releases provide canonical and legacy viewer asset names; image archives are
byte-identical aliases and manifests retain their corresponding image tags.
See [release packaging](../../.github/workflows/release.yml).

Python source uses [`app.gods_eye_view`](../../apps/backend/app/gods_eye_view).
The obsolete `app.prisma` shim and `app.territorial` directory are removed;
repository callers import the canonical package. Older notebooks retain their
own installed package until migrated. The deployment archive contains
`gods_eye_view/*` as input to standalone source assembly; the published stream
executes its complete `.py` source. Older agent deployments retain their own
embedded class until their consumers are switched.

## Persisted data and native resources

AI Data Governance publishes only `ai_data_governance` in the package catalog.
The installer recognizes the former `ai_data_governance_vsc_extension` identity
to adopt its manifest, recorded agent ID and existing Delta control rows.
An adopted installation keeps its manifest location and control key; a fresh
installation uses the canonical identity. Conflicting manifests or agent
identities require reconciliation instead of creating another global module.
See [Governance lifecycle](../../apps/backend/app/aidp.py).

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
The [installer](../../terraform/hooks/gods_eye_view_bootstrap.py) rejects ambiguous
matches instead of creating a second managed workflow.

The new Workspace target is `/Workspace/medallion/gods_eye_view`, with readable
standalone Python files. Older `/Workspace/medallon/prisma` and
`/Workspace/territorial` artifacts are not deleted by that change. Retire an
artifact only after checking job, deployment and installed-library references.
The new agent uses memory namespace `ai_gods_eye_view`. Preserve existing memory,
including `prisma_bogota`, until an explicit retention or migration operation is
defined; the new namespace does not automatically import earlier conversations.

The new control path uses `.control/gods_eye_view/` Object documents and an
immutable post journal. Its VM SQLite projection is rebuildable. The
[database facade](../../apps/backend/app/gods_eye_view/database.py) retains Oracle
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
for a separate review. See [credential selection](../../apps/backend/app/gods_eye_view/runtime_secrets.py).

Legacy `simulation` input is normalized to `Synthetic`; historical records and
the `simulation` control document remain compatible. Reset recognizes both
values. Family-scoped sensor resets retain their original operation IDs and
scope; global reset never widens them. Cancelled IDs remain terminal.

Native Gold-agent acceptance, Object control activation, production pointer
cutover and legacy resource/credential retirement are separate operational gates.
None is implied by source compatibility, an active deployment or a local test.
