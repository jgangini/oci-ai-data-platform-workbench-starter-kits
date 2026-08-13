# OCI AI Data Platform Cloud Migration Lab

An end-to-end Oracle Cloud Infrastructure laboratory for learning data engineering with Oracle AI Data Platform (AIDP). It deploys an AIDP platform and shared workspace, shared Spark compute, a governed Oracle-managed Object Storage data plane, and a self-service registration application.

Release v2.0.0 keeps one private `aidp-data-<suffix>` bucket with `01_landing/`, `02_bronze/`, `03_silver/`, and `04_gold/` prefixes. Notebooks address these locations with OCI URIs and external tables; the package creates neither external AIDP volumes nor an explicit OSCS/OpenSearch resource. The workspace uses the opaque participant key as its folder name, so email characters and PII never enter AIDP paths; the same key and lab ID scope jobs, Object Storage paths, and table names.

The assigned labs, package versions and hashes, exact workspace paths, reconciliation phases, and independent administrator operation journals live in a layout-v3 manifest at `/Workspace/medallon/.control/<participant>.json`, outside each participant's `ADMIN` subtree. Layout-v2 manifests are migrated in place without replacing active assets. The visible `lab-manifest.json` is tutorial metadata only; neither it nor the student-writable bucket controls authorization, overwrite behavior, or cleanup scope.

The data bucket uses the default Oracle-managed encryption key. The lab creates no OCI Vault, KMS key, OAuth client, dedicated provisioner identity, or additional OCI API key. Identity Domains and AIDP requests use the same operator profile uploaded to Deploy Studio.

## Versioned lab packages

`apps/backend/app/labs/catalog.json` lists immutable, versioned packages. Each available lab has four canonical CSV datasets with deliberate quality defects and exactly five canonical notebooks: Landing, Bronze, Silver, Gold, and Lineage. `agent` is catalogued as planned and cannot be assigned until it has functional assets and the required AI infrastructure.

| Lab | Package | Dataset row counts |
| --- | --- | --- |
| Banking | 1.0.0 | branches 20; customers 200; accounts 320; transactions 4,000 |
| Telecommunications | 1.0.0 | plans 12; network sites 30; subscribers 250; usage events 6,000 |
| Retail | 1.0.0 | customers 300; products 150; orders 1,200; order items 3,000 |
| Healthcare | 1.0.0 | patients 240; providers 48; appointments 900; encounters 700 |

The participant root is `/Workspace/medallon/<participant_key>/<lab_id>`. CSV and notebook bytes are identical for every participant. Landing adds the technical `participant_key`; AIDP job parameters provide `participant_key`, `lab_id`, `workspace_root`, `bucket_name`, and `objectstorage_namespace`. One workflow per lab derives its task graph from `lab.json`, so adding a notebook does not require Python changes. All participants use the shared `oci_landing`, `oci_bronze`, `oci_silver`, and `oci_gold` schemas; table names retain the opaque participant key and lab ID to prevent collisions.

## RBAC and registration lifecycle

The permissions are intentionally split:

| Principal | AIDP access |
| --- | --- |
| Pending participants | Workspace `USER` only; no OCI IAM permission to operate AIDP |
| Developer group | Workspace `USER`, catalog `SELECT`, shared compute `USE`, and `ADMIN` on the four collaborative schemas |
| Deployment operator | Built-in `AI_DATA_PLATFORM_ADMIN`, inherited from creating the platform and verified by post-apply |
| Individual participant | Root `READ` without cascade, own opaque-key folder `ADMIN` with cascade, and own job `MANAGE` |

Developer IAM can `use ai-data-platforms`, read bucket metadata, and manage objects only in the exact `aidp-data-<suffix>` bucket. The deployment operator retains its existing administrative identity; the lab creates no operator-specific user, group, role, policy, grant, or API key. Pending participants receive no AIDP IAM grant.

Registration first validates every selected lab and then creates or reconciles the Identity Domains user in the pending group. The API provisions each lab idempotently and promotes the user to the developer group only when all initially selected labs are active. Participants may select multiple available labs only during registration. Administrators can later add, redeploy, or remove a lab independently; the last lab cannot be removed without deleting the participant. A durable operation ID and per-lab journal make retries resume the same mutation without touching other labs.

## Safety contract

- Operator credentials never enter Git, Terraform variables, Terraform state, VM metadata, hook artifacts, or logs. Post-apply delivers the uploaded `config` and `key.pem` to the VM exactly once through an application-encrypted Object Storage envelope.
- The uploaded `key.pem` must be an unencrypted RSA API key. Preflight rejects encrypted, unreadable, or non-RSA keys before OCI provisioning and never echoes key material or passphrases.
- The VM generates a temporary 3072-bit RSA bootstrap key. Post-apply wraps an AES-256-GCM data key with RSA-OAEP/SHA-256 and uploads only the encrypted envelope as `.bootstrap/operator-credentials.json`; no Vault, KMS key, OAuth client, or additional OCI API key is created.
- The administrator password and registration code reach Terraform only as PBKDF2 hashes. Lab users activate their own Identity Domains password from the standard OCI welcome email.
- The VM decrypts the envelope locally, verifies that the profile user matches the preflight operator OCID and that `key.pem` matches the configured fingerprint, writes both files atomically with mode `0600`, then deletes the Object Storage object and verifies its absence. The temporary bootstrap key is removed afterward.
- Participant and developer access is granted through AIDP RBAC. Post-apply verifies that the deployment operator is a direct member of built-in `AI_DATA_PLATFORM_ADMIN`; it never creates `AIDP_LAB_PROVISIONER`.
- The runtime signs both Identity Domains and AIDP requests with the installed operator profile selected by `OCI_CONFIG_FILE`. Instance principals can access only the exact one-use bootstrap object and are not a runtime authentication fallback. The VM `.env` contains identifiers and PBKDF2 hashes, but no private key, OAuth secret, or plaintext administrator credential.
- The v2.0.0 path has no explicit OSCS/OpenSearch deployment and no external AIDP volumes.
- The lab does not require the Default Identity Domain's **Access Signing Certificate** setting and does not request public JWK access; that setting remains a tenant security-policy decision.
- OCI Provider 8.21 does not expose `force_destroy`; its native delete refuses a non-empty data bucket. The medallion prefixes therefore stay virtual until the first workload write, while real lab data must be emptied before destroying the stack.
- The HTTPS certificate is self-signed and includes the public IP/FQDN as SANs, so browsers will show a trust warning.
- Tenancy-level IAM and Identity Domains resources use an OCI provider alias pinned to the tenancy home region; regional AIDP, Compute, Networking, and Object Storage resources continue to use the deployment region.

## Local application

```powershell
docker build -f docker/Dockerfile -t aidp-lab .
docker run --rm -p 8080:80 -p 8443:443 --env-file .env aidp-lab
```

Required runtime values are documented in `apps/backend/.env.example`. They include `IDENTITY_DOMAIN_URL`, `OCI_CONFIG_FILE=/etc/aidp-lab/oci/config`, `OBJECTSTORAGE_NAMESPACE`, and `BUCKET_NAME`. Identity and AIDP use the uploaded operator profile installed at `OCI_CONFIG_FILE`; there is no OAuth secret or separate provisioner setting.

`GET /api/health` is strict: missing runtime configuration, a failed signed Identity Domains query, or an inaccessible required AIDP workspace/catalog/compute or exact data bucket returns `503`. It returns `200 {"status":"ok"}` only when those registration dependencies are usable; upstream details and credentials are never returned. Successful deep probes are cached for 30 seconds and failures for 5 seconds so Docker and browser polling cannot throttle OCI.

### Local VM-equivalent profile

The development profile runs the same nginx, FastAPI and React image as the VM, but substitutes Identity Domains with in-memory users. It is intentionally local-only and cannot validate OCI policies, API signing, or AIDP permissions.

```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```

Open `https://localhost:18444` and accept the local self-signed certificate. The sample profile uses `admin` / `admin` and registration code `AIDP-2026`; change the hashes in `.env.dev` before sharing the environment. Stop it with `docker compose -f docker/docker-compose.dev.yml down`.

### OCI-connected local profile

To exercise the same image against deployed Identity Domains and AIDP resources, generate the ignored `.env` and sanitized OCI config, then use the localhost-only profile:

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml config --quiet
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

The bootstrap discovers exactly one active lab, its `aidp-data-<suffix>` bucket, and its Object Storage namespace. It writes runtime values to ignored `.env` and a non-secret config to `.tmp/oci-local/<suffix>/config`; that config rewrites only `key_file` to `/etc/aidp-lab/oci/key.pem`. Compose bind-mounts the generated config and the original operator `--key` file read-only. Neither file contents nor host paths are printed. Because passphrases are never copied, the supplied operator key must be unencrypted for this local profile.

Open `http://127.0.0.1:18082`. This profile has no restart policy and binds only to `127.0.0.1`; it is for development testing, not a replacement for the OCI VM. It deliberately uses HTTP so DOM-based tests can run without accepting the VM-style self-signed certificate. Stop it with `docker compose --env-file .env -f docker/docker-compose.oci-local.yml down`.

## Terraform

```powershell
cd terraform
terraform init -backend=false
terraform validate
```

The v2.0.0 preflight accepts only the trusted repository and the immutable `v2.0.0-rc.1` through `v2.0.0-rc.7`, or `v2.0.0`, release SHA in `us-chicago-1`, while keeping the compartment name as the editable Deploy Studio input. Names follow OCI's 1-100 character alphanumeric, period, hyphen, and underscore contract. In `new` mode validation confirms that the exact name is available to create; in `existing` mode it confirms one unambiguous ACTIVE compartment. It also rejects forbidden infrastructure and policies, rejects conflicting AIDP work requests for the selected name, and checks current VM capacity. For a saved Terraform plan, run `python terraform/release_gate.py --plan-json <plan.json>`; it fails unless every managed resource action is create-only.

Deploy Studio manifest v1 currently has no hook between Resource Manager PLAN and its automatic APPLY, and it does not pass the plan JSON to repository preflight. Therefore the create-only plan check is available for manual/CI validation but cannot be enforced by this repository inside the current CloudTechNext PLAN/APPLY sequence. Do not start a lab APPLY until that explicit plan check has passed or CloudTechNext adds a post-plan/pre-apply hook.

Deploy Studio creates or resolves the target compartment before starting Resource Manager. The repository preflight discovers the tenancy home region and operator user OCID, then uses OCI `create_compute_capacity_report` in the selected availability domain for E5/E4 Flex with the requested OCPUs and memory. Only the non-secret operator OCID reaches Terraform; the uploaded config and key remain hook inputs until the encrypted one-use delivery.

The base deployment reconciles the AIDP workspace, catalog, shared compute, four collaborative schemas, root `/Workspace/medallon` folder, and pending/developer roles after verifying the operator's built-in platform administration. Registration then installs the selected canonical packages and individual folder/job permissions before promoting pending membership. The bucket is addressed directly through OCI URIs; no external volumes or explicit OSCS/OpenSearch deployment participate in this path. Reconciliation waits for credential consumption, asynchronous AIDP resources, and strict HTTPS health, and refuses ambiguous or conflicting resources.

The VM shape remains explicit per APPLY. The capacity report is a preselection, not a reservation, so capacity can change before instance creation. When the report says E5 is unavailable and E4 is available, preflight selects E4 without requiring another secret or user choice. If creation later fails because capacity changed, run a new APPLY; this package deliberately does not claim an automatic post-failure retry.

## Lab acceptance

For a release candidate, use structured deployment events, API responses, logs, and DOM/accessibility state rather than screenshots:

1. Require a successful Resource Manager APPLY and post-apply result, then verify `GET /api/health` returns exactly `200` and `{"status":"ok"}`.
2. Verify the workspace, catalog, shared compute, `/Workspace/medallon` root, four collaborative schemas, and the RBAC matrix above. Confirm operator membership in `AI_DATA_PLATFORM_ADMIN`, absence of `AIDP_LAB_PROVISIONER`, deletion of `.bootstrap/operator-credentials.json`, zero external volumes, and no explicit OSCS/OpenSearch resource.
3. Register participant A with all four available labs and participant B with Banking and Retail. Observe pending phases for identity, workspace, schemas, content, and permissions; each user must remain pending until all initial labs are active.
4. Confirm five notebooks per lab, exact package SHA-256 values, canonical dataset row counts, and distinct participant paths, tables, and jobs.
5. Run every workflow through Lineage and compare counts, aggregates, quality checks, and the lineage result with each package's `expected_results`. Banking results and asset hashes must match between A and B.
6. Run every workflow a second time and require identical results. Verify upstream/downstream and column derivation in Master Catalog from structured DOM/API state.
7. Add Telecommunications to B, redeploy only B's Banking lab, and remove only B's Retail lab. A must remain unchanged and Agent must remain visible but disabled.
8. Leave the validated candidate active for review; cleanup of any baseline is a separate, exact-OCID operation.

## License

This project is licensed under the [MIT License](https://github.com/jgangini/oci-aidp-cloud-migration-lab/blob/main/LICENSE).

OCI AIDP Cloud Migration Lab is an independent project and is not an official Oracle product. It is not affiliated with, endorsed by, or sponsored by Oracle Corporation. Oracle, OCI, and related marks are trademarks or registered trademarks of Oracle and/or its affiliates. Third-party trademarks, logos, service names, and assets remain the property of their respective owners.
