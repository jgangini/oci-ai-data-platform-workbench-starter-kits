# Local Codex Policy for oci-aidp-cloud-migration-lab

This file supplements the global `~/.codex/AGENTS.md`.

Keep this file repo-specific. Do not duplicate universal rules that already live in the global policy.

## Project Identity

- Repo root: `D:\dev\oci-aidp-cloud-migration-lab`
- Purpose: Deploy Studio-owned OCI AI Data Platform migration laboratory.
- Technical audience: OCI platform engineers and lab instructors.
- Primary surfaces: Terraform, AIDP post-apply REST hook, FastAPI registration API, React admin/registration UI, nginx/Docker.

## Repo Operating Defaults

- Preferred validation commands: `terraform fmt -check -recursive`, `terraform validate`, `terraform test`, `pytest apps/backend/tests terraform/tests`, `npm test`, `npm run build`, and `docker build -f docker/Dockerfile`.
- Preferred search and inspection tools: Semble first, then direct file inspection; use the official AIDP Swagger as the REST contract.
- Default runtime or environment assumptions: OCI Provider 8.x, Terraform 1.7+, Python 3.12, Node 22, Oracle Linux 9, Default Identity Domain.

## Local Validation Policy

- Required checks beyond global Graphify and Sentrux: mock-provider Terraform contract, Python tests, frontend build/tests, and container health smoke test.
- Safe shortcuts for docs-only work: no runtime checks when only prose changes.
- Windows sandbox pytest runs must use a fresh, nonexistent `--basetemp=.tmp/<task-specific-name>` directory; the global TEMP pytest directory repeatedly fails with access denied. Check the path does not exist first, because pytest clears an existing basetemp.
- Native viewer tests must use the Cesium version pinned in `apps/prisma-viewer/.upstream/package-lock.json` and installed under `.upstream/node_modules`; the Docker build installs that tree only. The legacy viewer's root `node_modules` may contain a different Cesium version and internal module paths.
- AIDP source uploads and diagnostic payloads must read and write UTF-8 explicitly. The Windows default can corrupt Spanish prompts and docstrings. Normalize line endings only when comparing source equivalence; verify artifact hashes against the original bytes.
- Release, deploy, or approval gates: never tag/release until Terraform and all application checks pass; OCI APPLY is always an explicit external action.

## Repo-Specific Friction

- Sensitive paths or fragile areas: `terraform/hooks/post_apply.py`, `terraform/templatefile/user_data.sh`, `apps/backend/app/credential_bootstrap.py`, SCIM filters, and the one-use credential envelope.
- Credentials, external systems, or approval boundaries: the uploaded OCI config/key may exist only in Deploy Studio private temporary files, the authenticated encrypted envelope, and the root-only VM runtime directory. They never enter Git, Terraform variables/state, VM metadata, artifacts, or logs.
- Noisy, slow, or expensive commands to avoid by default: live OCI APPLY and Identity Domains mutations; use provider mocks and HTTP fakes first.
- Do not make the Default Identity Domain's **Access Signing Certificate** a deployment requirement. The lab was live-validated with that setting disabled; it remains a tenant security-policy decision.
- Identity Domains and the OCI AIDP control plane reuse the uploaded operator profile from `OCI_CONFIG_FILE`; do not create a dedicated provisioner user/group/policy/key, `AIDP_LAB_PROVISIONER`, OAuth client, or Vault secret without explicit authorization.
- Use the live-validated Workbench base `https://datalake.<region>.oci.oraclecloud.com/20240831/dataLakes/<platform-ocid>` for the operator profile. Do not infer data-plane access from control-plane success, `created_by`, or an interactive console session: require a signed read-only `/roles` probe with the exact runtime identity and preserve its OCI request ID.
- The VM instance principal may manage only the exact `.bootstrap/operator-credentials.json` object during bootstrap. Require authenticated encryption, exact operator/fingerprint validation, atomic `0600` installation, verified object deletion, and temporary RSA-key removal before runtime is ready.
- A manual AIDP platform must have its own stable 9-statement required policy. Never rely on a policy owned by another Terraform stack; VNIC, subnet, NSG, and Object Storage service deletion permissions remain optional.

## Continuous Improvement Triggers

- Promote a repeated friction to this local file after 2 recurrences in the same repo.
- Promote a repeated manual sequence to a script or skill after 3 recurrences or when it is safety-critical.
- Promote a rule to the global policy only when it is cross-repo or clearly universal.
- Review `.codex/improvement-log.md` before large tasks and record only meaningful signal after non-trivial work.

## Future Delegation Hooks

- Candidate explorer roles: OCI provider schema and AIDP Swagger investigator.
- Candidate reviewer roles: IAM/SCIM security reviewer and Terraform deployment reviewer.
- Candidate repo-specific skills or MCPs: AIDP live acceptance and lab cleanup workflow.
