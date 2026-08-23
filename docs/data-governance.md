# AI Data Governance add-on

> Status: `v2.1.17` validation target. Local contracts and the live AIDP schema-owner permission check are implemented; the next acceptance is one clean deployment after all local gates pass.

## Runtime boundaries

| Component | Responsibility | Identity | Persistent state |
| --- | --- | --- | --- |
| AIDP Agent on `aidp_agent_shared_compute` | DAMA-DMBOK evidence, explanations, and registered query requests | Effective AIDP caller plus a non-logged governance session variable | AIDP `checkpointer` backed by Autonomous AI Database |
| Governance gateway on OKE | Authorization, column policy, masking, tokenization, registered query execution, and audit | OCI Workload Identity plus the effective user's OAuth token | Delta tables in the standard catalog's `data_governance` schema |
| Autonomous AI Database 26ai | AI Compute Agent memory and platform AI bootstrap | AIDP/VM bootstrap identities | Agent conversation/checkpoint state |
| VS Code extension | Catalog navigation, governed SQL/notebooks, Permissions UI, and Agent chat | OCI request signing plus user OAuth PKCE | Tokens only in VS Code SecretStorage |

Autonomous is mandatory for the deployed Agent-memory path. The governance gateway does not read or replace Agent memory. Conversely, policies are authoritative only in `data_governance`; Autonomous is never used as a policy fallback.

Deploy Studio creates one private Object Storage bucket named `oci_artifact` when the add-on is enabled. The governance schema is `data_governance`: catalog tables are addressable as `aidp_lab.data_governance.<table>` and their Delta files live at `oci://oci_artifact@<namespace>/data_governance/<table>`. The JDBC bundle is stored at `data_governance/runtime/aidp-jdbc-driver.zip`. This hierarchy keeps governance tables and runtime assets centralized while preserving one path per Delta table.

## Authorization model

- `AI_DATA_PLATFORM_ADMIN` can read and update policy records.
- `AIDP_DEVELOPER` can execute only the effective access granted by existing policies and registered queries.
- The server enforces authorization independently of UI state. A disabled Permissions panel is convenience, not a security boundary.
- The Agent has no direct `data_governance` permission and cannot submit arbitrary SQL.
- Every request is evaluated using the end user's subject, groups, and roles. Agent or gateway service identity never upgrades the user's decision.

## Query decisions

1. Parse one read-only statement and resolve all table and column references.
2. Compare the versioned catalog snapshot with policy state.
3. Fail closed for missing, new, ambiguous, or conflicting columns.
4. Reject explicit denied columns with `403` and the column names.
5. Rewrite `SELECT *` to omit denied columns.
6. Apply `NULL`, `MASK`, and `TOKENIZE` before serialization.
7. Enforce row and response-byte limits.
8. Record the effective principal, query, policy revision, affected columns, outcome, and request identifier without storing source secrets or unmasked values.

Python and Scala notebook cells receive only a governed result materialized as a DataFrame prelude. SQL cells execute at the gateway. Switching notebook language does not grant direct catalog access.

## Catalog synchronization

Synchronization upserts catalogs, schemas, tables, and columns by stable identifiers. Renames update metadata without replacing policy identifiers. Removed objects become tombstones; policies and audit history are retained. New columns remain blocked until an administrator classifies and reviews them. Explicit lineage rules propagate the most restrictive effective decision and record conflicts for review.

## Agent contract 2.0.0

- `catalog_inventory`: live participant-scoped catalog evidence.
- `catalog_lineage`: live entity or column lineage evidence.
- `governance_policy_explain`: explains one registered `query_id`; it never exposes control-table rows.
- `governed_query`: executes one registered `query_id` with at most 20 named scalar parameters.

The AIDP Agent defines `governance_access_token` as required with `shouldLog: false`. VS Code sends it in A2A message metadata as `sessionvariables.governance_access_token`. Generated Agent code reads the session context at invocation time, forwards the token only in the HTTPS Authorization header, rejects redirects, caps responses, and returns sanitized failures.

## Deployment and network gate

OCI API Gateway is the only public entry point. It terminates TLS, validates the OCI Identity Domains token, audience, issuer, signing key, and governance scope, then forwards requests to one fixed private OKE load-balancer address. The OKE service accepts port `8080` only from the API Gateway subnet. The container is non-root, read-only, and stripped of Linux capabilities; it validates OIDC again before executing a request.

Terraform waits once for the exact DevOps pipeline policy to propagate before starting the server-side apply. The DevOps project emits a 30-day OCI service log so a failed manifest or rollout has stage-level evidence. Kubernetes uses `/healthz` for pod availability; `/readyz` remains the stricter operational check and returns `503` until the JDBC runtime, `data_governance` tables, and first catalog synchronization are ready. This allows the gateway to be installed before the licensed driver is uploaded without claiming data access is ready.

OCI APIs are accessed through Workload Identity. Deploy Studio creates the dedicated JDBC API key after Terraform apply and stores its private half only in OCI Vault. The administrator imports the licensed Oracle AIDP JDBC driver from the VM settings page; it is validated, sent directly to `oci_artifact/data_governance/runtime/`, and never stored in Git or Terraform state. The pod materializes both artifacts only on its ephemeral filesystem.

The technical JDBC identity receives only the documented external-table discovery permissions on `oci_artifact`: `read buckets` and `inspect objects`. It cannot read object contents, create objects, manage objects, or delete objects directly. AIDP's existing service-principal policy remains the only writer for buckets governed by that AIDP instance.

## Acceptance gates

- No `.oci`, PEM, token, wallet, secret, or Terraform state in Git, Docker build contexts, logs, VSIX packages, or OKE pod specifications.
- One gateway, one `data_governance` schema, and one participant Agent are reconciled without duplicates.
- One Agent redeploy occurs only when the package, source, or tool contract changes.
- Administrator policy operations succeed; developer operations return `403`.
- SQL, Python, Scala, notebooks, and Agent tools reach the same policy decision.
- Prompt injection cannot change identity, policy, or tool allowlists.
- Autonomous stays deployed and the AIDP `checkpointer` path remains enabled.
- The isolated OCI deployment is preserved after acceptance unless destruction is separately authorized.
