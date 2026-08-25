# AI Data Governance for VSC Extension

This production-only module installs one global governance Agent and the native AIDP resources that a future VS Code extension will discover. It does not deploy OKE, OCI API Gateway, Vault, KMS, an OAuth client, a JDBC identity, or a separate policy-enforcement gateway.

## Runtime boundaries

| Component | Responsibility | Persistent state |
| --- | --- | --- |
| Global AIDP Agent on dedicated AI Compute | DAMA-DMBOK catalog inventory and entity/column lineage | AIDP `checkpointer` backed by Autonomous AI Database |
| Continuous AIDP workflow | Synchronize all active Master Catalog metadata every 30 seconds without overlapping runs | Four Delta control tables in `oci_medallion.oci_artifacts` |
| Registration VM | Idempotent install, repair, pause, RBAC, and full module deletion | Protected global operation manifest outside the control tables |
| Future VS Code extension | Discover the fixed bucket and update configuration or access mappings as an administrator | Outside this repository |

Autonomous AI Database remains mandatory for Agent memory. Governance metadata and access mappings are separate Delta state and are never used as an Agent-memory fallback.

## Fixed storage contract

The artifacts bucket is always named `oci_artifacts`. Deploy Studio can create it or connect to an existing bucket, but the name is not editable. The logical schema is `oci_medallion.oci_artifacts`; every table has the physical path `oci://oci_artifacts@<namespace>/oci_artifacts/<table>`.

| Table | Contract |
| --- | --- |
| `data_governance_config` | One module row: `module_id`, `schema_version`, `enabled` as `0` or `1`, `updated_at`, and `updated_by`. |
| `data_governance_metadata` | Stable object identity, catalog/schema/table keys and names, column name and ordinal, data type, description, fingerprint, source version, identity status, soft-delete state, and timestamps. |
| `data_governance_access_policy` | One logical row per group and column: `permission_id`, `object_id`, `group_ocid`, `group_name`, `has_access` as `0` or `1`, and audit fields. A missing row means no access. |
| `data_governance_sync_state` | One row per source with snapshot version/hash, status, counters, start and last-success timestamps, and a sanitized error code. |

Synchronization never changes access-policy rows. Removed columns are soft-deleted so mappings remain available until the module is deleted. An unambiguous rename keeps its `object_id`; ambiguous identity creates a new identifier so a permission cannot be inherited accidentally.

## Authorization model

- `AIDP_DEVELOPER` receives `USE` on the Agent and can invoke or test it.
- `AI_DATA_PLATFORM_ADMIN` receives `ADMIN` on the Agent and is required to install, edit, deploy, delete, or manage permissions.
- Participants receive no Agent `MANAGE`/`ADMIN` permission and no direct AI Compute grant unless the AIDP invocation contract later proves one is required.
- The Agent exposes only `catalog_inventory` and `catalog_lineage`, reads all active Master Catalog catalogs, and excludes `oci_medallion.oci_artifacts` to prevent control-table self-ingestion.
- The Agent has no gateway session token and cannot execute arbitrary SQL or update access mappings.

This follows the [Oracle AIDP permissions model](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/permissions-model.html): `USE` is sufficient to invoke and test, while `ADMIN` includes deletion and permission administration.

## Continuous synchronization

The module creates one native continuous AIDP job with `maxConcurrentRuns=1`. Its loop:

1. Reads all active catalogs, schemas, tables, and columns with bounded pagination.
2. Excludes the control schema and computes a canonical snapshot hash.
3. Skips the MERGE when the snapshot is unchanged.
4. Upserts metadata, preserves access mappings, and marks missing columns as deleted when the hash changes.
5. Records a sanitized success or failure state and waits until at least 30 seconds from the cycle start.

If a cycle takes longer than 30 seconds, the next begins only after it finishes. With `enabled=0`, the workflow records `DISABLED`, pauses, and retains its resources. Reactivation requires setting `enabled=1` and resuming the workflow from the VM or future extension.

The continuous job is intentional because normal AIDP schedules have a minimum frequency of 30 minutes. See [AIDP limits](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/limits.html) and the [Create Job API](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/op-aidataplatforms-aidataplatformid-workspaces-workspacekey-jobs-post.html).

## Lifecycle

Installation is available only in production and only when the selected OCI user belongs to `AI_DATA_PLATFORM_ADMIN`. The first administrator starts the singleton operation; concurrent requests reuse the same operation and cannot create duplicates. The VM reconciles these phases:

1. Validate production mode, fixed storage, administrator role, and singleton state.
2. Create the four tables with `enabled=0`.
3. Reconcile the dedicated OCI credential, protected notebook, continuous workflow, and first snapshot.
4. Reconcile dedicated AI Compute, the global Agent, and its deployment.
5. Apply exact role permissions.
6. Set `enabled=1` only after synchronization and deployment succeed.

Redeploy repairs code, workflow, Agent, compute, and permissions while preserving all table data and the previous `enabled` value. Delete first disables and pauses the workflow, then removes the deployment, Agent, dedicated AI Compute, credential, notebook, workflow, all four tables, and only their exact Object Storage prefixes. It keeps the bucket, schema, shared Spark compute, Autonomous database, and other starter kits; the global manifest is removed last.

## Acceptance gates

- The module is absent from public config, participant registration, user creation, and laboratory mode.
- Admin APIs return `401` without a session and reject non-platform-admin targets with `403`.
- Concurrent lifecycle calls are idempotent and interrupted phases can resume from the protected manifest.
- `AIDP_DEVELOPER=USE` and `AI_DATA_PLATFORM_ADMIN=ADMIN`; participants cannot edit Agent source.
- Catalog add/change/delete, safe rename, ambiguous identity, no-change snapshots, failures, slow cycles, and `enabled=0` are covered by executable checks.
- No OKE, Vault, KMS, API Gateway, JDBC, OAuth/gateway variables, resources, outputs, packages, or release steps remain.
- No OCI configuration, PEM, wallet, token, secret, or Terraform state enters Git or logs.
