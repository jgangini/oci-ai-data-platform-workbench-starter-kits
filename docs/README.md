# Documentation

[Project overview](../README.md)

## Choose a path

| Reader | Start with | Continue with |
| --- | --- | --- |
| Participant or instructor | [Getting started](getting-started.md) | A starter kit guide below |
| Platform administrator | [Operations](operations.md) | [Architecture](architecture.md), global module guides |
| Developer | [Development](development.md) | [Architecture](architecture.md), [compatibility](reference/compatibility.md) |

## Participant starter kits

Every guide contains source data, task dependencies, Gold outputs and reproducible acceptance checks.

- [Banking](kits/banking.md)
- [Telecommunications](kits/telecommunications.md)
- [Telco Customer 360 Lineage](kits/telco-lineage.md)
- [Retail](kits/retail.md)
- [Healthcare](kits/healthcare.md)

## Shared global modules

- [AI Data Governance](modules/ai-data-governance.md): catalog synchronization, permissions, agent tools and lifecycle.
- [God's Eye View](modules/gods-eye-view.md): sources, streaming, map publications, agent queries and synthetic cleanup.

## Reference and scope

- [Architecture](architecture.md): deployment, trust boundaries, storage and processing flows.
- [Operations](operations.md): installation, updates, recovery and scoped deletion.
- [Development](development.md): local profiles, source ownership and checks.
- [Compatibility](reference/compatibility.md): retained names and rolling-upgrade contracts.
- [Repository instructions](../AGENTS.md): deployment, content, UI and validation rules for contributors.
- [Implementation limits](getting-started.md#current-implementation-limits): differences between source intent, packaged assets and accepted native behavior.

These guides describe the checked-in implementation. The linked `lab.json` files define package versions, asset hashes and expected results; deployment manifests and native job outputs establish what is installed and has actually run. A local test, diagram or historical validation report is not evidence that a different cloud release passed acceptance.

Keep reusable explanations here. Credentials, tenant identifiers, private run logs and temporary migration diagnostics do not belong in this documentation.
