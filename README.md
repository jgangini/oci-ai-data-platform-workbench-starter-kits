# OCI AI Data Platform Cloud Migration Lab

OCI AI Data Platform Cloud Migration Lab is a hands-on data engineering environment for Oracle AI Data Platform (AIDP). It provides ready-to-run, versioned laboratories that guide participants through Landing, Bronze, Silver, and Gold data layers, workflow execution, data quality, and lineage analysis.

The project deploys the shared OCI infrastructure once. Participants can then register for one or more laboratories without receiving generated or user-specific copies of the source data. Every participant uses the same canonical CSV files and notebooks, which makes exercises and expected results reproducible.

Current stable release: **v2.0.0**. The `main` branch is preparing **v3.0.0-rc.2**; do not use it as a validated release until the regional, lineage, Autonomous, and Agent acceptance gates pass.

## What the project provides

- A shared AIDP workspace and Spark compute, plus a private catalog with four medallion schemas per participant.
- A private Object Storage bucket organized as `01_landing/`, `02_bronze/`, `03_silver/`, and `04_gold/`.
- A registration and administration web application hosted on an OCI Compute VM.
- Identity Domains onboarding with pending and active participant groups.
- Versioned laboratory packages containing canonical CSV files, notebooks, task dependencies, expected results, and SHA-256 hashes.
- One independent AIDP workflow per participant and laboratory.
- Per-laboratory administration for adding, redeploying, or removing content safely.
- A Deploy Studio package for guided infrastructure deployment.

## Medallion learning path

Each laboratory follows the same progression while using a different business scenario.

| Layer | Participant activity | Typical output |
| --- | --- | --- |
| Landing | Register the canonical CSV sources in the participant area. | Source-aligned CSV tables |
| Bronze | Ingest source records and add technical metadata without changing the business meaning. | Raw governed tables |
| Silver | Validate, standardize, join, and quarantine invalid records. | Curated domain tables and quality issues |
| Gold | Build business-ready aggregations and analytical views. | Customer, service, operational, or financial insights |
| Lineage | Validate known transformations and inspect upstream, downstream, and column derivations. | Master Catalog lineage graph |

The standard laboratories contain five notebooks, one for each stage. The Telco Customer 360 Lineage laboratory uses a larger 14-task directed acyclic graph to demonstrate parallel domain processing and multi-source convergence.

## Available laboratories

| Laboratory | Package | Sources | Workflow | Main outcome |
| --- | ---: | ---: | ---: | --- |
| Banking | 2.0.0 | 4 CSV files | 5 tasks | Customer value, branch activity, transaction quality, and full medallion lineage |
| Telecommunications | 2.0.0 | 4 CSV files | 5 tasks | Subscriber usage, network-site activity, service quality, and full medallion lineage |
| Telco Customer 360 Lineage | 2.0.0 | 10 CSV files | 14 tasks | Cross-domain Customer 360, service ownership, geographic summaries, and detailed lineage |
| Retail | 2.0.0 | 4 CSV files | 5 tasks | Customer value, product sales, order quality, and full medallion lineage |
| Healthcare | 2.0.0 | 4 CSV files | 5 tasks | Patient utilization, provider activity, encounter quality, and full medallion lineage |
| Data Governance Agent | 1.1.0 | — | — | Participant-editable DAMA-DMBOK Agent for catalog inventory, entity/column lineage, and allowlisted governance metrics; release remains gated on live accuracy and isolation testing |

Laboratory order, descriptions, versions, and availability come from [`apps/backend/app/labs/catalog.json`](apps/backend/app/labs/catalog.json) and each package's `lab.json`. Adding a future package does not require hard-coded changes to the registration interface.

### Banking

Banking introduces a governed pipeline for branches, customers, accounts, and transactions.

- Source rows: 20 branches, 200 customers, 320 accounts, and 4,000 transactions.
- Silver work: standardize records, preserve valid relationships, and isolate quality issues.
- Gold tables: `banking_customer_value` and `banking_branch_daily`; the final notebook validates the real pipeline lineage.
- Reproducible check: transaction amount total `600066.50`.

### Telecommunications

Telecommunications analyzes plans, network sites, subscribers, and usage events.

- Source rows: 12 plans, 30 network sites, 250 subscribers, and 6,000 usage events.
- Silver work: curate subscriber, plan, site, and usage data and record quality issues.
- Gold tables: `telecommunications_subscriber_monthly` and `telecommunications_site_daily`; the final notebook validates the real pipeline lineage.
- Reproducible check: usage charge total `15026.56`.

### Telco Customer 360 Lineage

Telco Customer 360 Lineage is the most complete lineage exercise. It combines CRM, prepaid, postpaid, product, and home-service data into a unified view of each customer and every service registered in that customer's name.

The ten sources contain 7,405 rows:

- CRM customers and addresses.
- Product catalog.
- Prepaid lines and recharges.
- Postpaid accounts, lines, and invoices.
- Home services and installations.

The workflow contains 14 tasks. Bronze processing branches by domain, Silver tasks curate each domain in parallel, and the branches converge into service ownership and Gold analytics.

Silver tables include `customer_master`, `customer_addresses`, `prepaid_service`, `postpaid_service`, `home_service`, `service_ownership`, and `quality_issues`. Gold produces:

- `customer_360`: 493 valid customers with primary address, service counts, and monthly value.
- `customer_service_portfolio`: 1,261 prepaid, postpaid, and home services with product and ownership details.
- `geographic_service_summary`: 12 regional summaries of customers, services, and monthly value.

The source data contains exactly 30 controlled quality incidents. Landing uses CSV, while Bronze, Silver, and Gold use Delta tables so AIDP can expose the complete entity and column lineage demonstrated by the laboratory.

### Retail

Retail transforms customers, products, orders, and order items into sales analytics.

- Source rows: 300 customers, 150 products, 1,200 orders, and 3,000 order items.
- Silver work: validate customer, product, order, and item relationships and quarantine quality issues.
- Gold tables: `retail_customer_value` and `retail_product_daily`; the final notebook validates the real pipeline lineage.
- Reproducible check: item quantity total `7487`.

### Healthcare

Healthcare prepares patient, provider, appointment, and encounter data for operational analysis.

- Source rows: 240 patients, 48 providers, 900 appointments, and 700 encounters.
- Silver work: standardize operational records, validate references, and isolate quality issues.
- Gold tables: `healthcare_patient_utilization` and `healthcare_provider_daily`; the final notebook validates the real pipeline lineage.
- Reproducible check: encounter cost total `557597.00`.

### Data Governance Agent

The optional Agent laboratory creates `u101_agent_data_governance` for participant `u101`. It acts as a DAMA-DMBOK data-governance specialist: it can describe that participant's Master Catalog, inspect entity and column lineage, and run only the predefined read queries shipped with the package. Answers must separate observed evidence from explanation, governance implications, and recommendations or limitations. The Agent does not accept arbitrary SQL and does not infer missing owners, stewards, controls, or lineage.

The package includes a versioned acceptance matrix covering catalog scope, quality, entity and column lineage, stewardship gaps, DAMA control mapping, participant isolation, unsupported certification claims, and arbitrary-SQL refusal. Candidate releases execute these questions live and retain the tool trace and response text as structured evidence without participant data from another catalog.

The deployment uses one shared Autonomous AI Database 26ai DW, but creates isolated `U101_AGENT` and `U101_AGENT_RO` database users for each participant. A participant deletion removes the AIDP Agent and external catalog, drops only that participant's Autonomous users, removes the participant workspace and data, and finally deletes the Identity Domains user.

## End-to-end user guide

### 1. Deploy the shared environment

Use OCI Deploy Studio with an immutable validated release. Deploy Studio discovers subscribed regions and compatible AIDP, Autonomous AI Database 26ai DW, and OCI Generative AI Chat capabilities before provisioning the network, private data bucket, registration VM, Identity Domains groups and policies, AIDP workspace, compute, and permissions.

When the deployment completes, retain these outputs:

- Application URL for participant registration.
- Administrator URL for user and laboratory management.
- AIDP Workbench URL.
- Lab access summary artifact.

See [OCI Deploy Studio compatibility](#oci-deploy-studio-compatibility) for the supported package contract and required inputs.

### 2. Create or register a participant

There are two onboarding paths:

1. A participant opens the application URL, enters the registration code, full name, email address, and one or more available laboratories.
2. An administrator opens the administrator URL, selects **Users**, chooses **Add user**, enters the participant details, and selects laboratories from the catalog table.

The email address is also the OCI Identity Domains username. It is never rewritten with aliases or release-specific suffixes.

Registration progresses through Identity, Workspace, Schemas, Content, and Permissions. The participant remains pending until every initially selected laboratory is active. The application can resume a retryable operation by its operation ID, so a browser timeout does not require creating another participant.

### 3. Activate the OCI account

The participant receives the standard OCI Identity Domains welcome email and sets a password. After activation, the participant signs in to the AIDP Workbench URL using the same email address.

Participant codes begin at `101`. The first participant receives `u101`, the next receives `u102`, and so on. A participant workspace follows this convention:

```text
/Workspace/medallon/u101_participant@example.com/<lab_id>
```

The code is used for technical isolation; the email makes the participant folder easy for an instructor to identify.

### 4. Open and run a laboratory

In AIDP Workbench:

1. Open the participant folder under `/Workspace/medallon`.
2. Select the laboratory folder.
3. Review the canonical source files and notebooks.
4. Open the workflow named `wf_<participant_key>_<lab_id>`.
5. Confirm that the shared Spark compute is running.
6. Run the workflow and monitor every task until it reaches a successful terminal state.
7. Review the validation task and compare the results with the expected values documented in the laboratory package.

Notebooks receive `participant_key`, `lab_id`, `workspace_root`, `bucket_name`, and `objectstorage_namespace` as AIDP task parameters. They do not contain participant-specific rendered values.

### 5. Inspect tables and lineage

Each participant receives a private catalog such as `u101_aidp_lab`, containing governed `oci_landing`, `oci_bronze`, `oci_silver`, and `oci_gold` schemas. Table names include the participant key and laboratory ID, for example:

```text
u101_telco_lineage_customer_360
```

After a successful workflow:

1. Open Master Catalog.
2. Find a Silver or Gold table for the participant.
3. Open Lineage and select both upstream and downstream directions.
4. Inspect entity-level dependencies.
5. Switch to column-level lineage to trace source columns into curated and analytical outputs.

For the Telco Customer 360 Lineage package, useful traces include CRM document to `customer_360`, prepaid or postpaid MSISDN to `customer_service_portfolio`, and customer region and monthly value to `geographic_service_summary`.

## Administrator guide

The **Users** page shows participant status, Identity status, participant code, and a summary of assigned laboratories.

Use the edit action for a participant to open the laboratory manager. From there an administrator can:

- Select an available laboratory and save to provision it.
- Refresh or redeploy one laboratory to install the current package version.
- Clear a laboratory selection to remove only that laboratory's workflow, tables, objects, workspace content, and grants.
- Delete the participant to clean up all laboratories and then remove the Identity Domains account.

At least one laboratory must remain assigned. To remove the final laboratory, delete the participant instead. Operations are serialized per participant and journaled independently per laboratory, so a pending or failed change does not make other active laboratories unavailable.

The **Settings** page lets an administrator update the registration code and review the AIDP Workbench URL without exposing stored secrets.

## Reproducibility and participant isolation

Every active package is immutable for its declared version:

- CSV and notebook bytes have declared SHA-256 hashes.
- Two participants assigned the same package receive byte-for-byte identical assets.
- Expected source counts, quality controls, business aggregations, table counts, and lineage relationships are declared in `lab.json`.
- Re-running a workflow must produce the same results.

Only technical locations and permissions differ:

```text
Workspace:      /Workspace/medallon/<participant_key>_<email>/<lab_id>
Object Storage: <layer>/users/<participant_key>/<lab_id>/...
Table:          <participant_key>_<lab_id>_<dataset>
Workflow:       wf_<participant_key>_<lab_id>
```

## Local development and testing

### Local-only profile

The local development profile runs the same nginx, FastAPI, and React image as the OCI VM. Identity and AIDP operations use local substitutes, so this mode is suitable for UI and API development but does not validate live OCI permissions or lineage.

```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```

Open `https://localhost:18444`. The example profile uses administrator credentials `admin` / `admin` and registration code `AIDP-2026`; change them before sharing the environment.

Stop the profile with:

```powershell
docker compose -f docker/docker-compose.dev.yml down
```

### OCI-connected local profile

Use this profile to run the application locally while connecting to an already deployed Identity Domain and AIDP environment.

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

Open `http://127.0.0.1:18082`. This HTTP endpoint avoids the deployed VM's self-signed certificate during local DOM-based testing. The profile binds only to localhost and is not a production deployment mode.

Stop it with:

```powershell
docker compose --env-file .env -f docker/docker-compose.oci-local.yml down
```

## Repository layout

```text
apps/backend/app/               Registration API, Identity, AIDP provisioning
apps/backend/app/labs/          Canonical laboratory catalog and packages
apps/frontend/                  Registration and administration UI
docker/                         OCI VM and local Compose profiles
scripts/                        Local bootstrap, generators, and architecture gates
terraform/                      OCI infrastructure and Deploy Studio package
terraform/hooks/post_apply.py   AIDP reconciliation and final access artifact
```

## Contributor validation

Documentation-only changes do not require runtime gates. Changes to code, packages, infrastructure, or generated assets should run the relevant checks:

```powershell
python -m pytest apps/backend/tests terraform/tests
cd apps/frontend
npm ci
npm test
npm run build
cd ../..
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
terraform -chdir=terraform test
docker build -f docker/Dockerfile -t aidp-lab:test .
.\scripts\arch-postflight.ps1
```

## Troubleshooting

- **The deployed application certificate is not trusted:** the VM uses a self-signed HTTPS certificate. Use the OCI-connected local HTTP profile for browser automation, or install and trust an organization-approved certificate for normal browser access.
- **A participant remains Pending or Permissions is displayed:** refresh the user list or reopen the laboratory manager. Provisioning is resumable and continues from its operation journal.
- **A request returns 502 or 504:** the application treats transient upstream and gateway timeouts as retryable. Keep the same operation rather than creating a duplicate user or laboratory.
- **A workflow says the cluster cannot be started:** start the shared AIDP compute manually before running the workflow. A cluster explicitly stopped by a user cannot be started by the workflow.
- **Lineage is incomplete:** confirm the workflow completed, the compute configuration does not set `spark.aidp.lineage.enabled=false`, and the four medallion layers use managed catalog tables. Telco Customer 360 Lineage keeps Landing in CSV and Bronze, Silver, and Gold in Delta; managed tables preserve the governed `oci_landing -> oci_bronze -> oci_silver -> oci_gold` entity and column paths in this environment.
- **Agent remains in Database/Pending:** the release fails closed until the participant Autonomous schema, read-only external catalog, and approved least-privilege bootstrap exist. The ADMIN password is used only by the post-apply process and is never delivered to the registration VM.

## Security essentials

- Never commit OCI configuration files, private keys, passwords, registration codes, Terraform state, generated certificates, or deployment artifacts containing identifiers.
- Upload an unencrypted RSA OCI API key only through the deployment workflow. Preflight rejects unreadable, encrypted, or non-RSA keys.
- Deploy Studio does not package OCI credentials in the Terraform source or state.
- The VM receives the operator profile once through an encrypted bootstrap object, validates it, installs it with restrictive permissions, and deletes the bootstrap object.
- The Autonomous wallet is stored only on the registration VM at `/opt/aidp-lab/autonomous` with owner-only permissions and is mounted read-only into the application container. The ADMIN password stays in the post-apply process; the VM receives only a rotated `EXECUTE`-only database operator and the wallet credentials required for participant lifecycle operations.
- Participant database credentials are generated per `u101`-style identifier and stored in the persistent application state volume with owner-only permissions. Deletion calls an ADMIN-owned allowlisted package that can drop only the exact participant owner and reader users.
- Participants receive access only to their folder, workflow, namespaced objects, and namespaced tables.
- The data bucket uses Oracle-managed encryption and remains private.

## OCI Deploy Studio compatibility

The **v3.0.0** candidate remains compatible with OCI Deploy Studio through [`terraform/deploy-studio.json`](terraform/deploy-studio.json), using manifest schema version 1 with optional regional-discovery extensions.

Deploy Studio support includes:

- New or existing compartment modes.
- Guided upload of the OCI configuration and unencrypted RSA API key without packaging either credential in Terraform.
- Administrator username, hashed administrator password, and hashed registration code inputs.
- Preflight validation for compartment selection, tenancy home region, operator identity, supported VM capacity, and release source.
- Terraform plan and apply through OCI Resource Manager.
- A selectable effective region from compatible `READY` subscriptions and a dynamically discovered active Chat model in that same region.
- New or existing Autonomous AI Database 26ai Data Warehouse, with ECPU model and a default of 4 ECPUs for a new database.
- Post-apply reconciliation of the AIDP workspace, catalog, shared compute, AI feature enablement, Identity roles, participant application, and final access artifact.
- Structured deployment steps and outputs for the application URL, administrator URL, AIDP Workbench, bucket, workspace, compute, and identity resources.

The OCI config region is only the initial choice. The selected effective region is applied consistently to AIDP, Autonomous, Generative AI, VCN, VM, and Object Storage without rewriting the original OCI config. Do not deploy from an untagged development commit; wait for `v3.0.0-rc.1` and its acceptance evidence.

Deploy Studio currently applies the Resource Manager plan automatically after planning and does not expose a repository hook between those stages. For controlled deployments, review the generated plan or run `python terraform/release_gate.py --plan-json <plan.json>` in CI before starting the final apply.

## License

This project is licensed under the [MIT License](LICENSE).

OCI AI Data Platform Cloud Migration Lab is an independent project and is not an official Oracle product. It is not affiliated with, endorsed by, or sponsored by Oracle Corporation. Oracle, OCI, and related marks are trademarks or registered trademarks of Oracle and/or its affiliates. Third-party trademarks, logos, service names, and assets remain the property of their respective owners.
