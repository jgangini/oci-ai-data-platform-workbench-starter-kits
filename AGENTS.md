# Oracle AI Data Platform Workbench Starter Kits

- Keep `terraform/deploy-studio.json` and the `terraform/` package compatible with Deploy Studio schema v1.
- Never commit OCI config files, PEM keys, OAuth client secrets, passwords, access codes, Terraform state, or generated certificates.
- Preserve the single-bucket medallion contract: `01_landing/`, `02_bronze/`, `03_silver/`, `04_gold/`.
- Treat the post-apply hook as idempotent. It may add missing AIDP resources, but it must never delete or replace a mismatched live resource.
- Run `./scripts/arch-preflight.ps1` before non-trivial edits and `./scripts/arch-postflight.ps1` after them.

## OpenWiki

This repository has documentation located in the /openwiki directory.

Start here:
- [OpenWiki quickstart](openwiki/quickstart.md)

OpenWiki includes repository overview, architecture notes, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

When working in this repository, read the OpenWiki quickstart first, then follow its links to the relevant architecture, workflow, domain, operation, and testing notes.
