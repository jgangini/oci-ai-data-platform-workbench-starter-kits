# Testing guide

Release v1.0.2 leans on tests to protect the lab’s security and workflow boundaries. The most useful thing to know is not just where tests live, but what each suite is defending.

## Backend tests
[`apps/backend/tests/test_api.py`](../apps/backend/tests/test_api.py) verifies the service contract around:
- registration code rejection before any identity calls
- strict health-check behavior
- SCIM filter safety in email validation
- active/pending/conflict registration results
- phase progression, public immutable-industry conflicts, administrator industry reset, and pending-to-developer promotion
- email-named participant folders, four shared schemas, opaque-key table isolation, RBAC alignment, and idempotent retries
- deterministic industry row counts and medallion notebook contracts
- admin session handling and protected routes
- admin create/reset/delete lifecycle, exact AIDP cleanup, retry operation IDs, and Industry inventory without exposing user OCIDs
- local-development mode without OCI
- identity client singleton behavior and shutdown

## Frontend tests
[`apps/frontend/tests/security.test.mjs`](../apps/frontend/tests/security.test.mjs) is a source-based safety suite. It guards UI behavior that is easy to regress accidentally, such as:
- not storing secrets in browser storage
- keeping password inputs as password fields
- preserving the segmented registration code control
- preserving phased backoff/deadline/abort behavior and exposing the AIDP link only for active users
- keeping admin routes and destructive actions behind protected UI flows
- preserving reset confirmation, industry selection, progress, and completion feedback

## Terraform tests
The Terraform tests under [`terraform/tests/`](../terraform/tests/) cover infrastructure shape and post-apply behavior. The recent git history shows many fixes in this area, so these tests are especially important when changing:
- VM bootstrapping or networking
- OCI capacity selection and public IP handling
- Identity Domains reconciliation
- operator OCID extraction, authenticated envelope encryption, exact-object access, identity/fingerprint validation, verified object deletion, temporary-key cleanup, and signed Identity Domains requests
- AIDP workspace/catalog/compute/folder/schema/job permissions
- post-apply idempotency and conflict handling

## OCI-local and live acceptance
Run `python scripts/bootstrap_local_oci_env.py --self-check`, then bootstrap with the successful Deploy Studio `email.html` passed through `--access-email` and validate `docker/docker-compose.oci-local.yml` with the generated `.env`. The profile must mount the sanitized config and original operator key read-only, bind only to localhost, use the deployment's administrator and registration verifiers, and report healthy only when a signed Identity Domains query plus the operator-profile AIDP workspace/catalog/compute and exact-bucket checks pass.

The release acceptance uses one real Banking participant. Before registration, require the operator to be a direct member of built-in `AI_DATA_PLATFORM_ADMIN`, confirm there is no `AIDP_LAB_PROVISIONER`, and prove `.bootstrap/operator-credentials.json` is absent after bootstrap. Then require the email-named folder; 20/200/320/4,000 Landing rows; four notebooks; four shared schemas; 15 opaque-key-prefixed catalog tables; `quality_issues > 0`; Bronze totals equal to Landing; Silver totals no greater than Bronze; a successful chained job; and Gold `banking_customer_value` plus `banking_branch_daily`. Run it twice and require identical counts. Promotion from pending to developers happens only after permissions complete. Use structured state and logs, not screenshots, and leave the participant active for follow-up.

## Suggested validation order
When changing multiple layers, validate in this order:
1. backend tests
2. frontend tests
3. Terraform validation/tests
4. local compose-based smoke test if the change spans the full stack

## Practical rule
If a change affects user identity, registration, session handling, or OCI permissions, there should be a test adjustment nearby. That repository pattern is already visible in the recent commit history and in the current test suite.
