# AI Market Intelligence

[Documentation](../README.md) · [Operations](../operations.md) · [Compatibility](../reference/compatibility.md)

## Current implementation boundary

The canonical candidate source is [ai_market_intelligence](../../apps/backend/app/labs/ai_market_intelligence/lab.json).
It replaces Social Network in the public catalog with an independent research
graph, not a renamed ingestion pipeline. The package is **planned** pending native installation acceptance. The portal now
provides an explicit, resumable installer; its fixture tests do not establish
installation acceptance. Agent Hub registration and activation are outside this
installer. An existing development experiment is not an installation receipt.

The package declares twenty-two hash-checked runtime assets, including its own memory
adapter, with no shared dependency on the legacy Social package. The loader
rejects undeclared paths and changed asset bytes. Its fourteen `ai_mi_` framework
tables and 145 documented columns are defined in
[schema.json](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/schema.json).
All belong in the configured `oci_artifacts` schema. The installation contract
requires empty tables, research disabled and no schedule or demonstration data.
No permanent `create_model.ipynb` or diagnostic `ops` folder belongs in the
installed runtime.

## Empty installation and recovery

Settings → Application → Install AI Market Intelligence opens the shared module
manager. Opening it performs only status and administrator reads. Select an AIDP
administrator, review resource/cost effects, optionally enter an Apify token, and
submit **Deploy** to start. A recorded operation exposes **Resume installation**
and its current phase; it is never reported installed merely because the portal
image was updated.

The administrator-only [installer](../../apps/backend/app/market_intelligence.py)
uses the existing OCI runtime identity, shared Spark compute and configured
artifacts bucket. It creates or explicitly reuses only project-owned resources.
An incompatible existing agent, credential, compute, table or active production
binding blocks reconciliation rather than being replaced. Governance is retained.

| Phase | Required evidence |
| --- | --- |
| Environment / credentials | Verified workspace, artifacts schema and shared OCI credential; separate `AiMarketIntelligenceApifyBearer` metadata |
| Temporary model bootstrap | Native task output proving fourteen Delta tables, 145 column descriptions, the expected schema hash, dedicated artifacts locations and zero rows |
| Bootstrap cleanup | Delete only the operation-owned job and `.setup_<operation>` directory after verified success |
| AI Compute / runtime / agent / access | Dedicated bounded compute, declared runtime assets, lowercase agent and exact role bindings |
| TEST | Saved-source equality, current TEST deployment and selected Playground session; live MCP `tools/list`, progress SSE and a completed paused response for this generation |
| Production endpoint | Publish only after TEST passes; research, paid Actors and schedules remain disabled |

The durable `installer.json` receipt lives under
`/Workspace/.control/modules/ai_market_intelligence/`. Every POST advances or
reconciles one phase, retaining its operation identity, resource keys and package
hash. GET observes without provisioning. Installation and agent-descriptor JSON use the
same compact, sorted representation as native workspace readback, so an AIDP
JSON response can be verified without a false content-drift retry. Browser storage holds no secret; tokens
are sent only to the authenticated installation API and are cleared from the form
after the credential phase. Failed writes with unknown outcomes require native
reconciliation, not automatic duplicate creation.

When TEST needs a session, open the agent in Workbench Playground, create a TEST
session, then refresh the installer and select it before resuming. No automatic
conversation or research run is created to manufacture test data. The SSE check
lists MCP tools but starts no Actor and invokes no model.

The installer expects one portal process to coordinate an operation; its in-process
lock and native receipt are not a distributed compare-and-swap guarantee. Before
running multiple portal replicas, introduce shared operation admission. Existing
nonempty tables or storage/schema drift require a separate reviewed migration;
this installer never drops them. An uncertain TEST submission remains actionable
and blocked until the native operation is reconciled.

**Open Agent Hub** is a separate navigation link. It derives the Oracle Agent Hub
origin from the verified Workbench hostname and opens `/static/ah/` in a new tab,
retaining only public `tenant`/`domain` context when configured. It does not pass
session tokens, register tools, activate an agent or imply an agent-specific chat.
The native agent endpoint has its own copy control after installation completes.

## Research and output

[workflow.py](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/workflow.py)
and [workflow_actions.py](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/workflow_actions.py)
describe the research graph. Runtime bindings select reviewed credentials,
scope, model and spending caps. The package keeps evidence, findings, limitations
and references, with charts only when useful. Its
[streaming adapter](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/streaming.py)
emits progress milestones; it does not claim token-by-token model streaming.

## Remaining release acceptance

- Verify the complete empty installation from the immutable release through
  Settings → Application, including actual bootstrap output, cleanup and TEST.
- Confirm the paused production endpoint and that all fourteen tables remain
  empty; preserve Governance and shared resources throughout recovery.
- Only after installation acceptance, run separately authorized research questions
  with real data, test memory/SQL projection and reconcile provider costs.
- Agent Hub registration, OAuth Registry execution and an agent-specific chat
  remain separate native integration work. The general navigation link does not
  establish any of those checks.

The pinned runtime CI job exercises the graph with synthetic model/provider
fixtures, including milestone streaming, retries, evidence references and budget
admission. The backend job checks the package and empty-model contracts. These
tests neither launch Actors nor establish factual/native conversation acceptance.

The [Social module](ai-social-network.md) is retired. Its ingestion, first batch
and periodic scheduler are not acceptance tests for this candidate.

## Package layout

```text
ai_market_intelligence/
  lab.json
  notebooks/
    agent/
      market_intelligence.py
      requirements.txt
      ai_market_intelligence/    # reviewed Python modules, prompts and schema
  source/
    README.md                   # empty-installation and evidence policy
```

This uses the same manifest, `notebooks/` runtime boundary and `source/` contract
as the other kits. Runtime assets declare hashes and workspace paths; there is
no Market-specific exception in the asset loader. Banking has persistent
medallion notebooks and CSV inputs because it is a data pipeline. This module
has no seed data and generates only an operation-owned temporary model notebook,
which is removed after verified success. Host-side provisioning stays outside
the package in `app/market_installation/`, and is not uploaded to AIDP.
