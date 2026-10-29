# AI Social Network

[Documentation](../README.md) · [Architecture](../architecture.md) · [Operations](../operations.md)

AI Social Network is an optional shared module, independent of AI Data Governance and God's Eye View. Its [package contract](../../apps/backend/app/labs/ai_social_network/lab.json), [installer](../../apps/backend/app/social_network.py), [ingestion](../../apps/backend/app/labs/ai_social_network/notebooks/social_ingest.py) and [agent](../../apps/backend/app/labs/ai_social_network/notebooks/agent/social_agent.py) are the authoritative sources.

## Deploy and connect

Open **Settings → Application → AI Social Network**, select an AI Data Platform administrator and review the compute costs. Choose the source bucket and JSON prefix; an empty bucket field uses the selected Landing bucket. The Gold and artifacts buckets reuse the existing deployment selections and must be distinct from the source. Deployment creates one protected module folder, a native ingestion workflow and a dedicated agent AI Compute; it reuses the shared Spark compute and existing OCI runtime credential. No additional VM, OCI identity or database credential is created.

The installer pauses the managed writer before changing its notebook, waits for active runs, publishes the complete task graph and starts an initial ingestion. It enables the native UTC `0 0/5 * * * ?` schedule only after that run, agent deployment and exact permissions succeed. Concurrency is one and queueing is disabled. A five-minute trigger is not a guaranteed five-minute end-to-end SLA: long runs skip overlapping triggers and the durable ledger preserves pending work.

After the module becomes active, the agent icon before the deployment button reads back its exact ACTIVE native deployment and copies its chat URL. This URL contains no token and still requires OCI authentication and native USE permission. External web applications should invoke it through an authenticated server proxy; do not embed OCI keys in browser code. The independent Huginn integration implements this pattern with its existing per-user encrypted OCI connection.

Source bucket/prefix, Gold/control locations and runtime identity are immutable once installed. Redeploy preserves their data and resource identities. Changing the source requires an explicit data migration; this version does not silently adopt another source or expose a destructive Social deletion action. Resume retains its operation ID; a failed initial sync gets a new native run attempt after the source is repaired.

## Data model and incremental processing

Inputs follow the Huginn JSON envelope `events[]`, with `event_id`, timezone-qualified `created_at` and a publication `payload` containing `platform`, original `id`, text, URL, optional `published_at`, raw attributes, media and geography. The `network=<platform>/` folder must agree with the payload. Missing metrics, publication times and locations remain NULL; no sample publications are created. An empty source is valid and publishes empty Gold tables.

| Table | Grain / logical key | Partition | Role |
| --- | --- | --- | --- |
| `oci_gold.ai_social_current` | One latest observation per SHA-256(network, original post ID) | `platform` (six supported networks) | Post details and bounded searches; metrics are nullable |
| `oci_gold.ai_social_history` | A distinct observed business version; idempotent key includes post, version hash and observed time | UTC `observation_month` | Append-only observed changes, not a count of distinct posts |
| `oci_gold.ai_social_daily` | Network and nullable UTC publication date | `published_month`, with `unknown` for NULL date | Counts and reported metric sums over current posts |
| `oci_gold.ai_social_publication` | One committed publication receipt | None | Exact Delta versions of current, history and daily plus scan/backlog coverage |
| `oci_artifacts.ai_social_files` | SHA-256(object name, ETag) | None | File-version discovery, PENDING/PROCESSED/ERROR/SUPERSEDED receipts |
| `oci_artifacts.ai_social_ingest_state` | Module singleton | None | Listing cursor, run receipt and durable affected-date recovery journal |

Gold data lives under the selected Gold bucket's `ai_social_network/gold/<table>/`; protected controls live under the artifacts bucket's `ai_social_network/control/<table>/`. Existing format, location, partition and column types are checked before use. Deployment supplies Delta comments and independently fills missing Master Catalog descriptions after checking exact table identity and external location. Operator-edited descriptions are preserved. Delta comments alone do not populate the native catalog description. Other tables and source objects are not modified.

Discovery lists bounded pages and persists its continuation cursor. It records new object+ETag versions without resetting completed ones. Conditional `GET If-Match` binds each download to the listed version. Defaults bound a run to 25 files, 32 MiB, 20,000 events and five listing pages; an individual input file must not exceed 10 MiB. Oversized or malformed inputs remain visible errors; changed ETags are rediscovered. Bad files do not become empty successful business results.

Publication identity is the network and original provider ID. Business hashes exclude the outer capture event, ingestion timestamp, run ID and known raw scrape/query/order metadata. Repeated unchanged captures update the latest observation without adding a historical version. Text, attributes or metric changes add history; stale observations cannot overwrite a newer current state. Observation time and publication time are separate. History describes captured observations, not proof of exactly when an external post changed.

The writer compares only affected post keys, bounds collected records and affected dates, and uses explicit partition predicates on historical/current/daily MERGEs. It recalculates only affected publication dates, including dates emptied by source corrections. Replaying an interrupted batch cannot advance file acknowledgements before the Gold writes. Delta does not offer a cross-table transaction here: the durable dirty-date journal repairs interrupted aggregates, and only then does the writer publish a receipt of the three committed versions. Each agent turn reads that receipt and queries those exact `VERSION AS OF` snapshots. Until publication, the previous coherent snapshot remains usable.

## Agent and memory

The LangGraph flow captures the publication, plans a bounded query, executes a fixed read-only Gold SQL tool, and formats an evidence-bound answer. It supports summary, search, details and observation history; the model cannot submit arbitrary SQL, DDL or a source bucket path. String literals are encoded rather than concatenated from user/model SQL. Results are capped at 50 rows and four queries per turn. Default periods cover the previous 30 days relative to the publication; explicit periods are at most 366 days. History requires a post reference and an observation-month window. A returned sample is not a total; daily aggregates answer totals. Records lacking publication dates are not silently dated into those windows.

The model and signer reuse the deployment's selected regional model and shared OCI identity. Native invocation context is established before SQL tools or memory access. `AsyncProxyCheckpointClient` uses the platform's existing AI Lakehouse memory; missing memory/context fails closed rather than falling back to stateless responses. Follow-ups use prior conversation only to choose fresh current-turn queries. Cited post IDs must exist in those results. Public post text is untrusted data, never an instruction to the agent. Semantic full-corpus/vector search and sentiment classification are not implied by the bounded SQL search.

## Scaling and retention

Delta is selected because the native standard-catalog SQL tool and the existing runtime use Spark 3.5 / Delta 3.2; both Delta and Iceberg exist in AIDP, but this module does not introduce a second engine. Monthly history partitions avoid tiny per-post/per-file partitions at initial volume. The current table uses six stable network partitions for bounded-key MERGE pruning; daily aggregates have month pruning. Auto optimized writes and auto compaction are enabled using OSS Delta-supported properties; compaction uses its default target (128 MB), not an unverified vendor-specific table setting. Long post/JSON fields are excluded from default data-skipping statistics.

These defaults are a starting configuration, not a benchmark or proof of an unlimited scale. Measure ingest duration, pending/error files, full-scan age, file counts, scanned bytes and query plans under representative data. The object listing remains O(number of objects), although downloads/processing are incremental. If full-scan age/backlog breaches the intended SLA, replace repeated listings with an OCI Events/Queue-backed discovery adapter while retaining the same exact-version ledger; use periodic listing as reconciliation. That queue adapter is an upgrade path, not implemented behavior.

At sustained larger volume, separate Gold-serving Spark from ingestion, size both from measured concurrency, and evaluate partition-scoped `OPTIMIZE ... ZORDER BY (post_key, published_date)` with current file statistics. Very large history may require adaptive partition evolution and dedicated semantic indexes. Do not pre-create thousands of sparse partitions. No automatic history expiry, source deletion or VACUUM policy is enabled. Define retention, legal requirements and maximum reader lag before adding cleanup; removing Delta versions must preserve referenced publication snapshots.

## Reproducible acceptance

- [Normalizer and file contract tests](../../apps/backend/tests/test_social_network_ingest.py): metadata-insensitive hashing, changed records, unknown values, input limits, ETags, paging and native injected secrets.
- [SQL boundary tests](../../apps/backend/tests/test_social_network_agent.py) and [lifecycle tests](../../apps/backend/tests/test_social_network_lifecycle.py): committed snapshots, fixed tools, five-minute serial schedule and operation recovery.
- `python scripts/check_social_graph.py` with LangGraph 0.6.7 and OCI 2.189: real graph compilation, current-turn queries, same-session follow-up and isolated sessions with offline native transports. This does not prove the remote memory service.
- `spark-submit --packages io.delta:delta-spark_2.12:3.2.0 scripts/check_social_delta.py` with Spark 3.5 and the Delta Python package: real MERGE, repeated captures, date corrections, NULL dates and interruption recovery. Test data stays ephemeral and local.
- Native acceptance must additionally verify the current-revision task output and Gold/control counts, an unchanged-source rerun without duplicate history, the active schedule, exact Agent permissions, a real answer and follow-up through the browser, session isolation, endpoint copy, reload persistence and the portal release updater. HTTP 202/ACTIVE alone is insufficient. Keep private native receipts outside this reusable guide.

Native contract details matter: notebook secrets may be injected as an `aidputils` API instead of an importable module, and Master Catalog Delta operations use validated unquoted identifiers. Delta MERGE resolves the already-verified table location with `forPath` to avoid the OSS `forName` parser's two-part identifier restriction.

References: [AIDP tables](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/tables.html), [native agent tools](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/agent-flow-tools.html), [AI memory](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/agent-memory.html), [Delta OSS optimizations](https://docs.delta.io/optimizations-oss/).
