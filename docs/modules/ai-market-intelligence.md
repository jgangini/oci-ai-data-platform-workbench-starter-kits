# AI Market Intelligence

[Documentation](../README.md) · [Operations](../operations.md) · [Compatibility](../reference/compatibility.md)

## Current implementation boundary

The canonical candidate source is [ai_market_intelligence](../../apps/backend/app/labs/ai_market_intelligence/lab.json).
It replaces Social Network in the public catalog with an independent research
graph, not a renamed ingestion pipeline. The package is **planned**. The portal
does not yet install it, deploy its endpoint or create its Agent Hub registration.
An existing development experiment does not establish installer acceptance.

The package declares twenty hash-checked runtime files, including its own memory
adapter, with no shared dependency on the legacy Social package. The loader
rejects undeclared paths and changed asset bytes. Its fourteen `ai_mi_` framework
tables and 145 documented columns are defined in
[schema.json](../../apps/backend/app/labs/ai_market_intelligence/schema.json).
All belong in the configured `oci_artifacts` schema. The installation contract
requires empty tables, research disabled and no schedule or demonstration data.
No permanent `create_model.ipynb` or diagnostic `ops` folder belongs in the
installed runtime.

[model_install.py](../../apps/backend/app/labs/ai_market_intelligence/model_install.py)
only prepares a bootstrap document and an unscheduled job payload; it submits
nothing. Its fixture checks reject ownership, schema, comment and existing-data
drift before creating any missing table. Integrating that preparation with a
temporary bootstrap lifecycle and cleanup is still an installer task.

## Research and output

[workflow.py](../../apps/backend/app/labs/ai_market_intelligence/workflow.py)
and [workflow_actions.py](../../apps/backend/app/labs/ai_market_intelligence/workflow_actions.py)
describe the research graph. Runtime bindings select reviewed credentials,
scope, model and spending caps. The package keeps evidence, findings, limitations
and references, with charts only when useful. Its
[streaming adapter](../../apps/backend/app/labs/ai_market_intelligence/streaming.py)
emits progress milestones; it does not claim token-by-token model streaming.

## Remaining release acceptance

- Implement the dedicated, resumable empty-installation lifecycle for tables,
  Credential Store, AI Compute, agent and native Registry/Agent Hub registration.
- Bind a verified Agent Hub chat link from that registration. Until then the
  separate Open and Copy controls remain disabled. An inference endpoint is not
  an Agent Hub chat URL.
- Verify TEST with the installed revision before deployment; preserve Governance
  and shared credentials, computes, permissions and data.
- Validate the immutable release installation from Settings → Application, with
  all fourteen tables still empty. Only then run the separately authorized
  research questions with real data and reconcile their costs.

The pinned runtime CI job exercises the graph with synthetic model/provider
fixtures, including milestone streaming, retries, evidence references and budget
admission. The backend job checks the package and empty-model contracts. These
tests neither launch Actors nor establish factual/native conversation acceptance.

The retained [Social module](ai-social-network.md) supports existing installations
only. Its ingestion, first batch and periodic scheduler are not acceptance tests
for this candidate.
