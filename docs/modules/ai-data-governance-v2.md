# Governance V2: portable TEST candidate

This is a development candidate for continuing Governance acceptance in a fresh tenant. It is **not a certified production agent**. Local regressions and selected native behaviors have been exercised, but the remaining useful-answer and recovery cases are not closed. The previous tenant returned persistent OCI model throttling, including after bounded backoff. Credits alone do not establish model availability or effective throughput in another tenancy.

The maintained implementation is [app/ai_data_governance](../../apps/backend/app/ai_data_governance/). Prompts and the eighteen-table model are under [the Governance lab](../../apps/backend/app/labs/ai_data_governance/). Runtime assembly produces readable Python files with explicit target bindings; it does not copy an operator profile, token, wallet, historical conversation or permission grant into the deployment.

## Deploy Studio selection

Choose **Governance installation → Governance V2 · TEST candidate** when creating the new deployment. The manifest passes `aidp_runtime_mode=v2_test` to Terraform and the application environment as `GOVERNANCE_RUNTIME_MODE`. The default remains `legacy`.

Deploy Studio prepares the platform, workspace, selected artifacts bucket and application. In the portal, open **Settings → Application → AI Data Governance**, select an authorized platform administrator, review the resources, and start installation. The same durable module operation is resumed after polling, navigation or a transient failure. Selecting the V2 mode does not convert an already-installed legacy module. Do not delete or replace an existing installation as a migration shortcut.

The installer derives all resource identities from the new deployment. It reuses that tenant's shared `AidpRuntime` credential and verifies its identity digest. It preserves the central metadata identity; it does not introduce per-asset user delegation. Choose a model supported in the target region and verify that the target tenancy can actually invoke it. An ACTIVE model listing or available credits is not an inference acceptance result.

## Installed content

| Component | Maintained artifact / behavior |
| --- | --- |
| Delta controls | Eighteen `AI_DG_*` tables in `oci_medallion.oci_artifacts`, using the separately selected artifacts bucket. All table and column descriptions come from the reviewed model. Bootstrap is additive; existing incompatible tables cause failure without drop/truncate. |
| Consultative agent | `ai_data_governance`, readable entry/runtime under `/Workspace/.artifact/ai_data_governance/agents/governance/`; the installer requests **TEST only**. |
| Documentary worker | `ai_data_governance_sync_master_catalog`, under `/Workspace/.artifact/ai_data_governance/agents/documentation/`; a native worker deployment is required for the workflow's `AGENT_TASK`. This is separate from publication of the consultative agent. |
| Workflow | One maintained `wf_ai_dg_sync_master_catalog`, four tasks, at most one active run and no queue. A single-task additive bootstrap runs before the final four-task graph is installed. |
| Manifest | Explicit bindings, component revisions and per-file hashes under the owned artifact root. Native agent libraries remain managed by AIDP. |

| Task | Type | Workspace source |
| --- | --- | --- |
| `sync_inventory` | Python | `.artifact/ai_data_governance/tasks/sync_inventory.py` |
| `document_metadata` | Agent | Documentary worker, using the immutable run handoff |
| `publish_metadata` | Python | `.artifact/ai_data_governance/tasks/publish_metadata.py` |
| `replicate_model` | Python | `.artifact/ai_data_governance/tasks/replicate_model.py` |

Each workflow task has one bounded retry without retry-on-timeout. Replica recovery uses retained publication receipts instead of regenerating documentation or rewriting a description already present. The final workflow is scheduled every thirty minutes only after its first exact four-task run succeeds. Scheduling of the previously accepted workflow and acceptance of the consultative agent are separate checks.

The workflow discovers visible catalog/schema metadata under its configured technical identity and freezes each run's scope. Consultation uses explicit source-scope bindings captured during installation. A new schema added later requires review and regeneration of the consultation bindings before claiming coverage. Neither an empty traversal nor a partial inventory establishes that dependencies are absent.

## Documentation and consultation contracts

The documentary worker uses the field name/type, actual code and workflow references, transformations and available lineage to document missing descriptions. Names alone are insufficient to invent business meaning. Only blank descriptions are eligible; any nonempty text, including an existing `[IA]` description, is preserved. Generated text carries `[IA]`. Re-reading the catalog and replicating the observed metadata into the model are required before reporting a completed publication. Evidence without sufficient context is retained as a gap rather than fabricated documentation.

The consultative agent supports inventory, meaning, usage, measured quality, lineage, impact and the approved case lifecycle. A static reference is not an observed execution, a description is not a quality measurement, and an approval is not an executed action. Responses must expose scope, evidence and material limitations. Small lineage/impact graphs require supported nodes and directed relations, sources and a textual alternative; their specific Agent Hub presentation is not certified by local graph tests.

The native inference adapter retries only explicit provider HTTP 429 rejection: two additional attempts after 15/30 seconds plus jitter, within nine outer model invocations per consultation. Identity is checked again before each attempt. Unknown transport outcomes, cancellation, effects and invalid output are not retried by this mechanism. Persistent rejection is a failed consultation, not a useful-answer pass. See [Oracle's throttling guidance](https://docs.oracle.com/en-us/iaas/Content/generative-ai/dynamic-throttling.htm).

## Acceptance and operations

Installation completion means the owned resources, bootstrap and initial workflow were reconciled. The module still reports `validation_stage=TEST_PENDING`. Its Agent Hub endpoint remains unavailable until the consultative candidate has passed the agreed matrix and an explicitly authorized publication is implemented and performed. An ACTIVE TEST deployment is not functional acceptance.

Use the installed AIDP Agent Flow LangGraph CLI with a new target-specific local configuration and campaign. Preserve the existing development campaign and receipts separately; do not copy an old tenant's authority, native session identifiers or checkpoints into the new tenant. Verify the actually served component revision and record expected/observed behavior for each required case. Keep local tests, native integration and client presentation distinct.

This candidate's portal lifecycle supports **fresh installation only**. Legacy redeploy/delete paths must not operate on its resources. Failed or uncertain native submissions retain their identifiers and block duplicate POSTs until reconciled. Repair or retirement of a V2 installation requires an explicit, reviewed V2 operation; there is no automatic destructive fallback. Existing legacy installations continue through their original lifecycle.

## Local verification

```powershell
python -m pip install -r apps/backend/requirements-dev.txt
python -m pytest apps/backend/tests terraform/tests --basetemp .tmp/pytest-new-run
python scripts/check_ai_dg_documentary_graph.py
python scripts/check_ai_dg_governance_graph.py
```

Use a fresh nonexistent pytest temporary directory. These commands use deterministic fixtures; they do not establish credentials, model quality, effective quotas or native acceptance in the target tenant. CI pins its local graph dependencies without upgrading AIDP's managed libraries.

The pure [deployment bundle builder](../../apps/backend/app/ai_data_governance/deployment_bundle.py) and resumable [Deploy Studio installer](../../apps/backend/app/ai_data_governance/deploy_studio_install.py) are the installation source. Private `.tmp` scripts, `.oci`, `.aidp-local`, `aidp-agent.toml`, campaign receipts and account-specific test evidence are excluded from the release.
