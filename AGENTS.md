# Oracle AI Data Platform Workbench Starter Kits

This is the canonical repository guidance. Read docs/README.md and the source-linked contracts. This repository owns only the five participant kits, their portal and required OCI infrastructure.

## Boundaries

- Kits: Banking, Telecommunications, Telco Customer360Lineage, Retail and Healthcare. Keep their explicit catalog in apps/backend/app/packages and sources under apps/backend/app/kits.
- Governance, Market Intelligence and God's Eye View belong to independent repositories listed in docs/module-repository-separation.md. Do not restore module installers, viewer proxies, optional VM switches or module dependencies here.
- Preserve exact participant/resource identities, package hashes and isolation. Existing lab.json/lab_id fields are the current kit contract, not permission to add historical aliases or adoption logic.
- kits-only is a distribution boundary. Never apply reduced Terraform to a combined stack, overwrite its state or relabel its installed profile. Preserve existing deployments until an explicit transition is implemented and accepted.

## Deployment and secrets

- Keep terraform/deploy-studio.json compatible with the trusted Deploy Studio schema and pin releases to immutable commits. Solution logic stays here, not in solution-specific CloudTechNext branches.
- Terraform, hooks and release images must reproduce required configuration. Manual VM repairs are not substitutes for deployment fixes.
- Preserve Terraform resource addresses of retained kit resources. Renaming source files does not authorize changing cloud identities.
- Preserve five distinct bucket selections and exact ownership. Use Master Catalog metadata to establish table locations.
- Reuse the uploaded operator identity. The one-use credential envelope remains encrypted/authenticated, identity/fingerprint verified, atomically installed with restricted permissions and deleted after confirmation.
- Preserve the Autonomous definer package and EXECUTE-only operator for participant isolation. Its internal governance name does not mean the extracted module is installed.
- Never commit OCI keys/configuration, wallets, passwords, registration codes, certificates, Terraform state or private receipts. Parse private evidence selectively without printing secrets.
- Preserve authentication, authorization, validation and recovery. Unsupported formats fail without deleting remote resources.

## Kits and UI

- Version package metadata, assets, dependencies, hashes and expected results together. Never silently change bytes under an immutable version.
- Preserve naming, workflow parameters, permissions and notebook guards. The notebook2.0.0 naming discrepancy in getting-started remains a native acceptance requirement; do not suppress it.
- Registration and administration retain durable operations and verified ownership. Closing progress is not cancellation.
- Reuse confirmations, focus, keyboard and accessible status patterns. Explain exact scope before destructive operations.
- Keep fixtures, provisioning and native workflow execution as separate evidence. ACTIVE or HTTP202 does not prove native acceptance.

## Releases and validation

- kits-only releases contain aidp-release.json and aidp-lab-image-amd64.tar.gz. Enforce updater_protocol=2 and deployment_profile=kits-only in manifest, image and installed receipt; preserve checksum/source/platform checks. Never rewrite historical releases.
- Run scripts/arch-preflight.ps1 before nontrivial work and scripts/arch-postflight.ps1 afterward. One coordinator owns baseline and shared runtime. Do not reset baselines to hide degradation.
- Follow CI/release workflows for Python, frontend, Terraform and images. Report missing/failed checks honestly.
- On Windows use a fresh pytest --basetemp under .local. Exclude private state, caches, upstream builds and nested repos from architecture/search input. Update existing Graphify graphs without implicit initialization.
- Maintain Spanish source-linked docs. Keep migration state in the single separation plan and private runtime evidence outside tracked documentation.