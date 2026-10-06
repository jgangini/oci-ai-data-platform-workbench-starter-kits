# Development and validation

[Documentation](README.md) · [Architecture](architecture.md) · [Compatibility](reference/compatibility.md)

## Prerequisites and profiles

Use Git and Docker with Compose for local execution. For direct checks, CI uses Python 3.12 and Node 22 for the portal, Terraform 1.5.7 and 1.15.7, and the OCI provider pinned in [`a_versions.tf`](../terraform/a_versions.tf). The native viewer uses Node 24.14.0 in its [Dockerfile](../apps/gods-eye-view/Dockerfile); its [integration guide](../apps/gods-eye-view/README.md) defines the pinned upstream build.

| Profile | Command/configuration | What it verifies |
| --- | --- | --- |
| Local fixtures | `docker/docker-compose.dev.yml`, private `.env.dev` | Portal, API, viewer integration and deterministic local behavior |
| OCI-connected local app | `docker/docker-compose.oci-local.yml`, protected operator mounts | Behavior against an existing cloud installation; actions affect that installation |
| Deployed OCI VMs | Terraform and immutable release images | Native infrastructure and application deployment; module acceptance remains separate |

## Start local fixtures

From the repository root, copy the example only if your private file does not already exist:

```powershell
if (-not (Test-Path .env.dev)) { Copy-Item .env.example .env.dev }
docker compose -f docker/docker-compose.dev.yml up --build -d
```

Open `http://localhost:18081`. The profile binds to loopback and uses HTTP without installing a certificate. Its demonstration hashes in [`.env.example`](../.env.example) are not production credentials; set local values using [`hash_secret`](../apps/backend/app/security.py) when configuring your own account. Keep `.env.dev`, account fixtures and generated state out of Git.

```powershell
docker compose -f docker/docker-compose.dev.yml ps
docker compose -f docker/docker-compose.dev.yml down
```

Do not add `--volumes` when stopping a profile unless intentionally removing its persistent data. This fixture profile does not validate real Identity Domains, IAM, Spark, model access or AIDP agent memory.

## Connect a local app to an existing OCI deployment

The [bootstrap helper](../scripts/bootstrap_local_oci_env.py) uses the selected operator profile/key and a successful Deploy Studio access email. Treat that email and the resulting `.env` as private.

```powershell
python scripts/bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem> --access-email <deployment-email.html>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

For an installation whose God's Eye View Object runtime is already initialized or migrated, add `--gods-eye-control-bucket <gold-bucket-name>` to the bootstrap command. Use the deployment output `medallion_bucket_names.gold`; the helper verifies the bucket and writes `GODS_EYE_CONTROL_BUCKET` explicitly. It does not infer this value from the Landing bucket. Complete the [migration gates](operations.md#migrate-gods-eye-view-controls) before connecting new consumers to an existing module.

The default URL is `http://127.0.0.1:18082`. The helper prepares protected local configuration; Compose mounts the selected OCI key read-only. This profile targets live services and is not an isolated copy of the deployment. Stop it with the same Compose file and environment:

```powershell
docker compose --env-file .env -f docker/docker-compose.oci-local.yml down
```

The optional parent Deploy Studio host wrapper is outside this repository. Follow that host's instructions if it manages multiple previews; do not assume a developer-specific drive path or port is part of this project's API.

## Validate a change

Read repository instructions before editing. For code/infrastructure changes, run the project's architecture preflight before the change and postflight afterward. Use the smallest relevant checks during development and the release/CI matrix before publishing a release.

```powershell
./scripts/arch-preflight.ps1
python -m pip install -r apps/backend/requirements-dev.txt
python -m pytest apps/backend/tests terraform/tests --basetemp=.tmp/pytest-new-run
```

Choose a **new, nonexistent** `--basetemp` directory for each run; pytest clears an existing one. The [package loader](../apps/backend/app/lab_packs.py) validates manifests, declared asset hashes, CSV counts and notebook dependencies. These checks do not replace a native workflow run; see the [layout acceptance gap](getting-started.md#current-implementation-limits).

```powershell
Push-Location apps/frontend
npm ci
npm test
npm run build
Pop-Location
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
terraform -chdir=terraform test
docker build -f docker/Dockerfile -t aidp-lab:test .
docker build -f apps/gods-eye-view/Dockerfile -t gods-eye-view:test .
./scripts/arch-postflight.ps1
```

Run `terraform test` with the newer CI-tested Terraform version. The viewer image runs native integration tests against its pinned upstream dependencies; testing an unrelated root `node_modules` tree is not equivalent. If Graphify or Sentrux is unavailable or a gate fails, report it explicitly. Do not reset a baseline to hide degradation.

For portal-managed module changes, also run `python terraform/tests/check_module_plan.py`. It uses mock providers to compare the installed base with the module plan and permits exactly the viewer VM, dynamic group and two policy creates. [Installer tests](../apps/backend/tests/test_gods_eye_view_installation.py) cover source/stack identity, recovery and plan guards; [frontend tests](../apps/frontend/tests/gods-eye-view-module-manager.test.mjs) cover approval and progress. These checks make no live acceptance claim. The `enabled_vm_modules` registry currently allows only `gods_eye_view`; adding a module requires explicit Terraform resources, lifecycle code, packaging and a checked allowlist, not a new deployment checkbox or arbitrary uploaded VM.

For a documentation-only change, check links, headings, diagrams and claims against source. Runtime builds and architecture gates are not required unless executable/configuration/package files also change. The canonical automation is [CI](../.github/workflows/ci.yml); the stricter tag and artifact rules live in [Release](../.github/workflows/release.yml).

## Check Object controls and migration

God's Eye View's new cloud path uses Object Storage for controls and an immutable post journal. Its SQLite index is a local read projection; tests must prove it can rebuild without losing source revisions, sequence bounds or cancellation receipts. No Oracle credential is required for these isolated checks:

```powershell
$env:PYTHONPATH = "apps/backend"
python -m pytest apps/backend/tests/test_gods_eye_view_control_store.py apps/backend/tests/test_gods_eye_view_control_migration.py apps/backend/tests/test_gods_eye_view_object_post_index.py --basetemp=.tmp/pytest-controls-new-run -o addopts= -q
```

Use fake storage and fresh local indexes for these tests. Exercise stale ETags, concurrent writers, interrupted staging, history mismatches and terminal cancellation. Existing Oracle facade tests protect migration compatibility; passing them does not authorize a live fallback.

The [migration module](../apps/backend/app/gods_eye_view/control_migration.py) separates read-only export, create-only staging and activation. Its tests do not connect to OCI or stop writers. The [operational procedure](operations.md#migrate-gods-eye-view-controls) requires a verified writer freeze, actual Gold/Object history comparisons and acceptance of the installed consumers.

## Change packages and AI workflows safely

- The five participant packages keep their source data and notebook dependency contracts under `apps/backend/app/labs/<lab_id>/`. The two global modules keep readable packaged runtimes there too; the [module guides](README.md#shared-global-modules) distinguish their deployment configuration and native acceptance checks.
- Change kit assets through their generator/source contract, then update the declared version, hashes and expected results together. Do not silently modify bytes under an already released package version.
- Keep participant parameters, notebook guards and provisioner naming aligned. Verify both the emitted workflow and a native run.
- God's Eye View publishes one readable `.py` per stream and one agent entry point from [its packaged notebooks](../apps/backend/app/labs/gods_eye_view/notebooks). Regenerate those artifacts with `python scripts/render_gods_eye_view_runtime.py`; use `--check` to fail on source or hash drift without writing. Deployment preserves the code body and substitutes only explicit nonsecret `RUNTIME_CONFIG`. Do not claim total-file byte equality across environments or hide decoding/import discovery in a running stream.
- Governance's [sync notebook and agent](../apps/backend/app/labs/ai_data_governance/notebooks) are edited directly as the authoritative source. Update their manifest hashes with a package change; deployment verifies those assets and substitutes only `CONFIG`. Its [runtime tests](../apps/backend/tests/test_governance.py) check source-body equivalence and configured execution; there is no second renderer or duplicate embedded template to edit.
- Test agent authorization, query boundaries, evidence/version checks and synthetic labels independently of model prose. A plausible answer alone is not acceptance.
- Do not put DB credentials, wallets or private keys into runtime documents, journal events or generated workflow source. The shared `AidpRuntime` credential supplies OCI access; control state belongs in Object Storage.
- Keep legacy aliases required for rolling upgrades until all consumers have migrated. Refer to [Compatibility](reference/compatibility.md) rather than duplicating alias lists in every guide.

## Documentation ownership

Keep the root README as an overview. Put shared setup in `getting-started.md`, runtime boundaries in `architecture.md`, procedures in `operations.md`, and domain behavior in `kits/` or `modules/`. Each behavioral claim should link to its manifest, implementation or executable check. Keep dates, tenant IDs, private receipts, temporary diagnostics and generated graphs out of reusable product guides.
