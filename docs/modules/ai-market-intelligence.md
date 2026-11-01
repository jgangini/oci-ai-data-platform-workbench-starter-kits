# AI Market Intelligence

[Documentation](../README.md) · [Operations](../operations.md) · [Compatibility](../reference/compatibility.md)

## Current implementation boundary

The canonical candidate source is [ai_market_intelligence](../../apps/backend/app/labs/ai_market_intelligence/lab.json).
It replaces Social Network in the public catalog with an independent research
graph, not a renamed ingestion pipeline. The package is **planned** pending native installation acceptance. The portal now
provides an explicit, resumable installer; its fixture tests do not establish
installation acceptance. Agent Hub registration and activation are outside this
installer. Apify uses Bearer authentication through the project Credential Store
entry; OAuth through AI Registry is out of scope and is not an acceptance gate.
An existing development experiment is not an installation receipt.

## Agent card and Agent Hub

`lab.json → agent.card` is the maintained definition of the A2A card: lowercase
agent name, package version, streaming and three skills covering approved-source
research, stored-report follow-up and registered-source listing. It explicitly
excludes automatic scheduled dispatch and rendered visual charts. This metadata
does not enable a tool or grant access.

Oracle distinguishes the development card from the card snapshotted with a
[published deployment](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/agent-flow-deployment.html).
Apply the package definition through **Edit agent metadata** in Workbench and
verify its readback before publishing the TEST-accepted candidate. The current
installer does not automate card reconciliation or AI Registry registration;
those remain explicit native operations. The GA read exposes `agentCardConfig`,
but that alone does not prove a conditional GA update contract. Do not infer it
from the older AgentFlows API or overwrite a published card automatically.

Use AIDP authentication and the existing dedicated compute when publishing for
Agent Hub. Registration and permissions must be checked separately; a successful
TEST deployment is not a registered agent, and registration does not establish
conversation or streaming acceptance. Apify remains authenticated by its Bearer
credential inside the agent, independently of the Agent Hub caller.

### Sharing and runtime credentials

In AI Registry, **Local agent** means an agent hosted in AIDP; it does not mean
localhost or an unpublished agent. A published agent exposes authenticated
`/chat` and `/a2a` endpoints that a separate application can consume.
[Oracle's invocation guide](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/invoke-deployed-agent.html)
requires an authenticated caller with at least USE permission. An Agent Hub link
does not grant that permission or replace sign-in. Native registration, access
for another authorized user and an actual Agent Hub conversation remain separate
acceptance checks; do not distribute a token-bearing login callback as a chat link.

| Boundary | Identity or credential | Purpose |
| --- | --- | --- |
| User to Agent Hub / endpoint | Authenticated caller and resource permissions | Authorize invocation and retain the caller's isolated conversation/research scope. |
| Agent platform operations | Deployment Run As identity, where supported | Execute platform operations; this does not determine downstream tool authentication. |
| OCI services used by the research runtime | Shared OCI-only credential named by the entry's `RUNTIME_CONFIG['credential_name']` | Initialize the configured model and Object Storage clients; verify the installation identity binding. |
| Agent to Apify MCP | `AiMarketIntelligenceApifyBearer`, secret field `token` | Authenticate reviewed research tools without asking each chat user for an Apify token. |

The implementation in
[native.py](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/native.py)
loads both runtime credentials on the server through `aidputils.secrets.get`.
Possession of a chat link does not authorize reading those secret values. Keep
the caller's authenticated `subject_id` distinct from the runtime credential;
sharing OCI/Apify connections must not merge users' journals or reports.
Do not infer another user's credential permissions or successful Run As binding
from an administrator's successful TEST. Confirm those bindings and isolation
with an authorized second-user test before calling multi-user access accepted.

## Native chat in TEST

The research runtime accepts ordinary questions of 1–500 characters. It keeps
the authenticated `subject_id` and session supplied by AIDP, and derives the
turn ID from the native session and `trace_id`; user text cannot choose an
owner. An interrupted checkpoint resumes the same question and paid-operation
ID. A different question cannot replace pending work. Native progress messages
stream before the final response, which is delivered after projection checks.

Explicit JSON commands with a caller-maintained `request_id` remain supported.
They are the retry contract for clients that need to replay a completed request:
a fresh native trace after completion represents a new conversation turn.
Ordinary chat alone cannot distinguish that turn from a network retry with a
new trace. Do not claim exactly-once research across those retries.

Installing the package still leaves research disabled and budgets at zero.
Testing the research graph requires separate development activation and source
approval; a successful paused MCP check does not validate a research answer.

Actor starts request an immediate receipt (`waitSecs=0`), followed by reads of
that run's status through Oracle's standard MCP invoker. An unknown outcome
still blocks another start until reconciled. Website reads explicitly configure
Apify Proxy, as required by the Actor's [input contract](https://apify.com/apify/website-content-crawler/input-schema).
Page reads use the Actor's documented 60-second request timeout, inside a
120-second run limit so startup does not consume the entire page deadline.
Request retries and session rotations are disabled. Usage-priced crawling still
requires a separately verified cost envelope, including proxy usage.
An empty evidence set cannot close as sufficient. Validated terminal provider
costs are recorded even when an Actor fails; missing costs keep their reservation
and are not reported as zero.
The adapter preserves response-level `com.apify/ActorRun` usage metadata,
including the `meta` alias serialized by Oracle/Pydantic. Costs must belong to
the unique run in that same response and agree with any explicit run cost.
USD values are rounded upward to integer microdollars; invalid or conflicting
values cannot become a verified receipt. These are observed run costs, not a
promise that later storage charges or model usage are included. See the
[upstream metadata issue](https://github.com/apify/apify-mcp-server/issues/1429).
An ungrounded model source selection asks for a registered name or an exact
approved URL rather than failing the conversation or widening its scope.
Intake receives up to twenty recent canonical topics with an active approved
base in the authenticated owner's journal. It can reuse an unambiguous topic
without inventing a differently worded objective. This is model-assisted
selection, not guaranteed semantic identity or an exhaustive catalog search.
Intake distinguishes requests to summarize, graph or explain an existing report
(`followup`) from reopening it unchanged (`read_report`). Followups reuse the
published evidence rather than starting another extraction. A requested chart is
calculated from the same validated, persisted evidence without changing the
original report; model routing still
requires native conversation acceptance.
Sources with neither approved targets nor discovery permission are excluded.
For a website step, an omitted target ID can be resolved only from a unique
exact approved URL match; ambiguous or unapproved choices ask for clarification.
When evidence exists but the planner needs a broader source scope, the runtime
publishes a partial report with an unresolved conclusion and the remaining gap;
it does not start unapproved searches or withhold all recovered findings.
Reports default to a short executive summary with readable source links;
technical evidence identifiers stay in the structured findings. Explicit requests
for detail can receive a longer explanation.
Catalog replies use source names, references and plain availability labels. They
state that no new information was searched, without exposing Actor, adapter or
model implementation details. A registered source is not proof of a live
connection; unavailable sources remain visible as unavailable. Provider calls
and model usage remain in operational evidence rather than a client-facing
catalog disclaimer.
Replaying an admitted request also preserves its original research window,
including when its journal is recovered from a new native session.

Projection streams only bounded step counts, never SQL or row contents.
Completed-node milestones follow their updated state; an event alone does not
prove native checkpoint persistence. Disconnect/resume remains subject to the
managed checkpointer and paid-operation journal. Projection failures release their lease
and cannot produce a verified success response.
SQL tool registry names are scoped to tenant, owner and generation. This avoids
concurrent projections replacing another scope's query configuration in Oracle's
name-based tool registry; per-scope leases still serialize that scope's writes.
MERGEs remain sequential. Readback retains each table's scoped row, key and value
checks inside one aggregate SQL request; every table must pass individually, so
errors cannot cancel each other across tables. Empty snapshots issue no SQL.
After a complete verified projection, the journal retains hashes of each MERGE
and its query compute. Later turns omit matching MERGEs but still read back
all nonempty tables. Claiming the projection lease invalidates saved hashes
atomically before SQL. They are restored only after successful verification
and an unchanged snapshot. An interrupted writer, changed destination or failed
readback forces complete writes on the next delivery; it never repeats capture.
External table drift is detected by readback, so that delivery fails before
the following attempt repairs the projection.

The package declares twenty-four hash-checked authoring assets, including its own memory
adapter, with no shared dependency on the legacy Social package. During packaging,
the installer assembles them into one readable `market_intelligence.py` and deploys
only that entry, `requirements.txt` and `agent-manifest.json`. It resolves local
imports and embeds prompts, schema, MCP definitions and nonsecret runtime settings
as visible Python constants. No runtime loader or supplementary source folder is
required. The loader rejects undeclared paths and changed asset bytes; assembly
rejects unknown local imports, changed entry-wrapper behavior and global-definition
collisions. Its fourteen `ai_mi_` framework
tables and 145 documented columns are defined in
[schema.json](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/schema.json).
All belong in the configured `oci_artifacts` schema. The installation contract
requires empty tables, research disabled and no schedule or demonstration data.
No permanent `create_model.ipynb` or diagnostic `ops` folder belongs in the
installed runtime.

## Financial research profiles

Reviewed financial Actor profiles reuse the same admitted MCP calls, operation
ledger and framework tables. They are community-maintained Apify Actors. Metadata
and input schemas were verified from the public Actor/build API on 2026-11-01;
this does not establish native execution acceptance or an official Yahoo/World
Bank integration maintained by those institutions.

| Profile | Pinned build | Bounded selection |
| --- | --- | --- |
| `yahoo_finance` — [`architjn/yahoo-finance`](https://apify.com/architjn/yahoo-finance/input-schema) | `1.1.1` | Suspended: native TEST failed before data retrieval because the bundled SDK rejects the MCP run origin. Compilation, reservation and paid start are blocked. Its output reader remains for historical receipts. |
| `yahoo_history` — [`memo23/yahoo-finance-scraper`](https://apify.com/memo23/yahoo-finance-scraper/input-schema) | `0.0.27` | Explicit historical daily closes for one to three approved tickers, at most 31 days and a total row cap. No bundled products, events, intraday or pre/post-market data. Native acceptance is separate. |
| `world_bank` — [`solidcode/world-bank-indicators-scraper`](https://apify.com/solidcode/world-bank-indicators-scraper/input-schema) | `1.0.6` | One to three countries and indicators, at most ten explicit historical years; compact series with an exact result limit. No aggregate, region or open indicator search. |

The Yahoo suspension is a remote Actor-image compatibility issue, not an AIDP
credential failure. Apify's [Python client releases](https://github.com/apify/apify-client-python/releases)
document the missing MCP enum correction in 3.0.5. Re-enabling the suspended
build requires a verified compatible provider revision and a native acceptance
test; changing the local SDK or retrying the same image does not repair it.
Selecting `yahoo_history` requires its own approved source and target, rather
than silently rebinding the old source. Its public tariff at verification was
US$0.001/start plus US$0.001/row, platform included; the bounded reservation
remains authoritative, and actual receipts determine observed spend.
Daily candles preserve the calendar date and explicit offset of the provider's
timestamp. The publisher's ISO examples do not establish a universal exchange
timezone; provenance records that limit rather than shifting a candle's day
when converting its observation time to UTC.

Provider failures, interruptions and timeouts are recorded as coverage limits
in the report, including partial reports. They must not be described as a
successful search returning no results. No raw error body, token or provider
run ID is added to user-facing failure notices.
If a dataset cannot be retrieved or fails the reviewed output contract, that
step contributes no evidence and the graph closes with an explicit coverage
limit. Previously verified evidence in that investigation remains available;
the same failure does not trigger another paid extraction.
Each registered target can be captured only once per research run, even if the
planner changes its arguments or result limit. Exhausting the approved base
closes with unresolved gaps rather than restoring already visited targets.
Distinct bounded global-discovery queries remain subject to their own scope,
request identity, step and budget limits. An exact retry reconciles its prior
operation; it does not obtain another paid start.

Financial selections must appear in the intersection of the active source policy,
target policy and topic approval. These policies use `tickers`, or `countries`
and `indicator_codes`, as explicit lists. They participate in the authorization
hash rechecked before paid start, so reducing any list invalidates an earlier
reservation. The planner cannot infer or approve another ticker or indicator.
Conversational administration for these policy lists remains pending; they are
not populated by empty installation.

`StepArguments.financial` carries the typed identifiers and explicit daily dates
or years. Missing or invalid financial selections ask for clarification before
reserving a provider call. Actor input, runtime/build, result and charge limits
remain controller-owned. The event charge cap bounds billed events; a Store
headline is not an account-specific price or a guarantee of complete results.

The existing document `metrics_json` stores metric, entity, indicator, unit,
currency and bounded period/value points. `provenance_json` records the Actor,
run, requested range and whether the period is daily or annual. The reference
links to the corresponding official entity/indicator page; the explanatory text
is explicitly a summary derived from Actor output, not a direct quotation from
that page. Publication time remains unknown; retrieval time and data period are
different facts. Retrieval after the admission window's end is expected and
does not invalidate an explicitly requested financial period. A structured
series supports its values without requiring a literal quotation from a web
page. Provider run identifiers remain in the audit journal and are omitted from
evidence presented to the model. An annual national indicator provides macroeconomic context,
not a private retailer's revenue, solvency or market share.

Optional charts use only persisted points of the same metric, entity, indicator,
unit and currency. Unknown units, contradictory duplicate periods or mixed
period granularities suppress the chart. Unknown numeric values remain null;
zero stays zero. Yahoo quote/financial-statement formats and other undocumented
output shapes fail closed. Neither the 14-table model nor its 145 columns change.

## Empty installation and recovery

Settings → Application → Install AI Market Intelligence opens the shared module
manager, using the same layout as Governance. Opening it performs only status
and administrator reads. The **Apify Bearer token** field sits directly below the
administrator. When missing, paste the API token without the `Bearer` prefix.
When an active, project-owned credential matches the saved operation, the field
shows a read-only abbreviated token, such as `apify_api_...7Q2k`, and explains
reuse. New credentials retain only their final four characters as display metadata;
older receipts show masked characters when that suffix is unknown. The form never
retrieves or displays the complete saved secret. A matching name alone does not establish availability. Conflicts or
an unavailable metadata check block submission. While the dialog is open, an
unknown credential status is checked again automatically without starting work.
Connection validity is checked separately in TEST. Normal installation and Resume
do not rotate an existing token; explicit Reinstall removes the project credential
and requires a new token. Entered secrets clear after the new credential phase.
Review resource/cost effects and
submit **Deploy** to start. A recorded operation exposes **Resume installation**
and its current phase; it is never reported installed merely because the portal
image was updated.

New operations verify and reuse project resources by default; saved operations
retain their original choice without an installation-options control. The same
progress panel shows the current phase while running and the saved step where
installation paused, without an expandable step list or a separate refresh button.
Once installation starts, this panel replaces the administrator, token and setup
description at the top. The original administrator is recovered from the native
receipt and resolved to a portal identity; the current administrator inventory
and installation API still validate access. If that lookup is unavailable, the
dialog requests the original administrator below the progress panel. The Bearer
field appears below progress only for missing/conflicting/unverified credentials
or a failed TEST connection check. A failed TEST does not by itself prove that
the token is invalid, and the form does not rotate it.
Resume keeps that phase visible until the server reports the next one, retaining
the operation identity and resources. Completed-step counts describe phases, not
elapsed-time percentages. One status message reports the current error or required
action, without repeating it in the form.
The five service badges match Governance: AIDP Workbench, Object Storage, Spark
Compute, Master Catalog and AI Compute. The field links to Apify's
[official token instructions](https://docs.apify.com/integrations/api#api-token).

The administrator-only [installer](../../apps/backend/app/market_intelligence.py)
uses the existing OCI runtime identity, shared Spark compute and configured
artifacts bucket. It creates or explicitly reuses only project-owned resources.
An incompatible existing agent, credential, compute, table or active production
binding blocks normal reconciliation rather than being replaced. Only the explicit
project-owned retirement described below permits replacement. Governance is retained.

`AidpRuntime` is a shared OCI credential used by Market Intelligence and other
modules, including Governance. Installation reuses its verified identity without
rotation. If absent, it uses the existing shared-credential provisioning helper
with the uploaded operator identity, records creation intent and reconciles an
uncertain result before another write. Neither reinstall nor project removal
deletes `AidpRuntime` or legacy shared-credential aliases.

For an explicitly reused development agent, the installer accepts the canonical
entry file or the former `/Workspace/ai_market_intelligence/agent/` entry. It
records the existing agent identity before publishing files. Its agent phase
checks the native entry file, dependencies and compute even when file bytes have
not changed. Binding repair uses the GET response ETag with `If-Match`, preserves
the agent identity and requires a matching GET afterward. Missing ETags, changed
identities and existing production deployments block repair. This path is specific
to Market Intelligence; it does not change the Governance updater.

These checks do not repair a broken Development editor project root. Oracle's
[UpdateAgentDetails contract](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/op-aidataplatforms-aidataplatformid-workspaces-workspacekey-agents-agentkey-put.html)
supports entry/dependencies/compute updates but does not expose `pathInfo` as an
update field. That separate root issue needs native reconciliation; the installer
does not send an undocumented field or create a substitute agent to hide it.

Model preparation uploads an operation-owned `model.py` through Workspace Objects
and binds it to a `PYTHON_TASK` on the existing Spark compute. It initializes
`SparkSession` and uses the same schema/ownership checks as the model generator.
It does not create a Jupyter notebook. This follows Oracle's
[workspace Python task workflow](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/configure-tasks.html#create-a-python-task)
and avoids the notebook-creation request rejected by the target service. Cleanup
still requires the exact empty-model receipt from successful native task output.

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
session, then select it in the installer before resuming. The open dialog observes
available sessions every two seconds; these GET requests do not advance installation. No automatic
conversation or research run is created to manufacture test data. The SSE check
lists MCP tools but starts no Actor and invokes no model.

The installer expects one portal process to coordinate an operation; its in-process
lock and native receipt are not a distributed compare-and-swap guarantee. Before
running multiple portal replicas, introduce shared operation admission. Existing
nonempty tables or storage/schema drift block normal installation; the explicit
Reinstall action below deletes only verified project tables and their contents.
An uncertain TEST submission remains actionable
and blocked until the native operation is reconciled.

Repairing tables does not turn an already failed bootstrap run into a successful
one. Resume still observes the recorded run and will reject its failed state.
Keep its history and use an explicitly reconciled native recovery before resuming;
do not clear `model_start_requested` or resource keys to force another run. Native
repair has not yet been accepted for this installer. The executable model tests
exercise the artifacts URI used by installation and reject IDL or absent locations
before creating any missing table.

When AIDP rejects an installation request, the operation receipt retains only its
HTTP method, endpoint without query, phase, status and Oracle request ID. The
progress dialog reports the HTTP status; request/response bodies, authorization
headers and provider exception text are not copied into the receipt. Unexpected
exceptions keep the generic public error; their private diagnostic contains only
the exception class and the last five function names/line numbers, without paths
or local variables. Reconcile the failed native step before
resuming the same operation; an error does not prove that a write never occurred.

**Open Agent Hub** is the chat-icon action immediately after Copy URL in the
Market Intelligence row. It is independent of installation and derives the Oracle Agent Hub
origin from the verified Workbench hostname and opens `/static/ah/` in a new tab,
retaining only public `tenant`/`domain` context when configured. It does not pass
session tokens, register tools, activate an agent or imply an agent-specific chat.
The native agent endpoint has its own copy control after installation completes.

## Delete and reinstall

**Reinstall** is separate from **Resume installation**. It is available for a
recorded installation that has failed, needs action or is complete; an operation
already progressing cannot be replaced. The same administrator must confirm the
project removal and enter the Apify token to save for the new installation.
This is destructive: project tables and their contents are removed, not migrated.

The authenticated `/api/admin/users/{user_id}/modules/ai_market_intelligence/reinstall`
endpoint requires a new operation UUID, the preceding operation UUID and the
exact project confirmation. It archives and verifies the prior receipt before
creating a new generation. Repeated requests resume that generation; they do
not clear failed-run flags or copy old TEST/production receipts into it.

Seven retirement phases precede the existing twelve installation phases:

1. Freeze project identities and ownership metadata before any cancellation or
   deletion. Matching names alone do not authorize table, compute or credential
   removal. Unknown model revisions or shared compute consumers block retirement.
2. Cancel project workflow runs and wait for their terminal states. Recheck runs
   during retirement and before tables/storage are touched.
3. Remove the inventoried workflows, deployments, agent and dedicated AI Compute.
   Re-read resource scope and use conditional native deletes when ETag is present;
   wait for absence.
4. Remove only the fourteen allowlisted, model-owned table names in artifacts or
   their previous Gold location. Other tables, schemas and catalogs are retained.
5. Clear only `ai_market_intelligence/` in the selected artifacts bucket, with
   conditional object deletes. Versioned buckets block before retirement; version
   purging requires a separately accepted implementation. Never recursively purge IDL.
6. Remove project runtime/diagnostic files and the recorded temporary bootstrap
   directories. Preserve installation receipts/history. The old top-level project
   directory is removed only when the inventoried agent was bound there.
7. Remove only the verified project Apify credential. Keep shared `AidpRuntime`.

Then the existing installer creates fourteen empty tables with explicit artifacts
locations, a new dedicated AI Compute and agent at the canonical path, recreates
the project Apify credential, verifies permissions and tests the paused revision.
Research, paid Actors, schedules and demo data remain disabled.

The new token stays only in the open form and authenticated requests until the
new credential phase completes. Reloading during retirement requires re-entry;
the history and browser storage never contain the token. Native deletion has
irreversible consequences and remains subject to the service's retention behavior.
The application assumes one portal coordinator, not a distributed admission lock.
For explicit Reinstall only, AIDP may omit ETag for agents, workflows, dedicated
compute and tables. The first three require UUIDs; tables use their qualified
names, which can be reused. All require creator and creation timestamp, the full
project ownership checks, a frozen binding/version/storage fingerprint and a
second GET after persisting deletion intent. Table removal also rechecks absence
of project writers. Any observed drift blocks removal; a newly available ETag is
used. This compatibility path cannot prevent a concurrent edit or table replacement
between the final GET and DELETE and is **not atomic compare-and-swap**. Avoid
concurrent administrative changes during retirement. Credentials and deployments
still require ETag; versions are never fabricated into ETags. Native AI Compute
listing explicitly selects `type=AI_COMPUTE`; the default list only returns Spark.
Native retirement and fresh installation acceptance must be verified separately
from the simulated resource and UI regressions.

## Research and output

[workflow.py](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/workflow.py)
and [workflow_actions.py](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/workflow_actions.py)
describe the research graph. Runtime bindings select reviewed credentials,
scope, model and spending caps. The package keeps evidence, findings, limitations
and references, with charts only when useful. Its
[streaming adapter](../../apps/backend/app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/streaming.py)
emits progress milestones; it does not claim token-by-token model streaming.
The native chat presents those milestones as separate Markdown paragraphs and
the final answer as readable Markdown, preserving citations and any proposal.
Financial or observation chart specifications also have a bounded two-column
table fallback: units and data periods are retained, unknown values stay unknown,
and displayed numbers use at most eight significant digits. Stored evidence and
the in-process structured response in `AIMessage.additional_kwargs.ai_mi_response`
retain their original values. The native SSE gateway omits those extra message
fields; clients must not assume that metadata is an external API contract.
Schedule confirmation shows the weekdays, time, timezone,
lookback, exact USD ceiling and each registered source/target before activation.

This does **not** establish a rendered chart in Agent Hub. Oracle's pinned
[A2UI gallery transport](https://github.com/oracle-samples/oracle-aidp-samples/blob/d006f1eb1843e82278b988366a2d20bd1ba983fc/ai/agent-hub/agent-samples/a2ui-component-gallery/a2ui_sdk/parser.py)
serializes one message beginning with `A2UI`, including its text and component
operations. The [weekly review example](https://github.com/oracle-samples/oracle-aidp-samples/blob/d006f1eb1843e82278b988366a2d20bd1ba983fc/ai/agent-hub/agent-samples/weekly-business-review/agent.py)
uses `OAChart`; neither example demonstrates appending that envelope to multiple
streamed progress messages. The candidate therefore retains streaming Markdown;
an optional negotiated A2UI response and actual client rendering require separate
acceptance. No sample SDK or dependency is copied into this runtime.

Questions such as “Which pages are registered for D1?” use the `list_sources`
administration intent. The agent resolves the canonical topic or asks for it,
then reads the authenticated catalog without starting research. It lists active
source/target/topic relationships, shows suspended adapters explicitly and
separates global discovery connectors from registered destinations. At most fifty
destinations are displayed, with the total stated if truncated. The interaction
does not count towards recurring research questions. This read does not verify
connectivity, register a source, change approvals or call Apify. A pending approval
continues through its existing confirmation flow; this change does not add
side-question handling within that conversation.

## Remaining release acceptance

- Verify the complete empty installation from the immutable release through
  Settings → Application, including actual bootstrap output, cleanup and TEST.
- Confirm the paused production endpoint and that all fourteen tables remain
  empty; preserve Governance and shared resources throughout recovery.
- Only after installation acceptance, run separately authorized research questions
  with real data, test memory/SQL projection and reconcile provider costs.
- Agent Hub consumption and an agent-specific chat remain separate native
  integration work. The general navigation link does not establish those checks.
  Apify OAuth through AI Registry is discarded; do not require its execution or
  provision an OAuth credential as a fallback for Bearer.

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
has no seed data and generates only an operation-owned temporary model script,
which is removed after verified success. Host-side provisioning stays outside
the package in `app/market_installation/`, and is not uploaded to AIDP.

The repository tree above is the maintained authoring layout. The deployed agent
project follows the same three-file layout as Governance:

```text
/Workspace/.control/modules/ai_market_intelligence/agent/
  agent-manifest.json
  market_intelligence.py
  requirements.txt
```

The native agent's create-time `pathInfo` must equal this directory, in addition
to its correct entry and dependencies paths. Correcting only those two file
bindings leaves an old project root unresolved in Development. The installer
rejects that mismatch and requires explicit agent recreation; it does not claim
to repair a create-only root through an update payload. Existing extra workspace
files require separately scoped cleanup after verifying their identity and bytes.

Standalone regressions import and execute the assembled entry with the installed
LangGraph, exercise research, evidence, replay and streaming, and simulate native
service bindings for setup and paused invocation. They complement native TEST;
they do not establish Registry discovery or published endpoint acceptance.

The first TEST must currently be initialized by opening the new agent's native
Workbench Playground. Subsequent revision refreshes use the verified TEST-only
redeploy adapter. Creating a bare deployment record is not a substitute for
starting the native deployment: the adapter refuses an uninitialized TEST before
recording a submission. Fully automated first-time TEST initialization remains
pending a verified execution contract. Existing pending deployments must be
reconciled before retrying; a still-`CREATING` record is not execution acceptance.

For native Registry acceptance, verify that the active deployment returns a
nonempty `agentCardUrl`, not only an HTTP 200 from a manually constructed card
URL. If the metadata is missing, use Workbench's **Edit agent metadata**, save
the reviewed card and redeploy; then read the native deployment again and test
the local-agent selector in AI Registry. This repair was verified separately
from recreating the workspace project. Do not delete sources, tables or
credentials to repair a missing card-discovery URL. The installer does not yet
automate this metadata-repair sequence or Registry registration; these remain
explicit native acceptance steps.
