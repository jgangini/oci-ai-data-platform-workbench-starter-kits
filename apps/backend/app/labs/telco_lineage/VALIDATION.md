# Telco Customer 360 Lineage validation

Validated on 2026-08-14 against the live AIDP candidate environment. This
report excludes credentials, OCIDs, tenant URLs, and participant PII.

## Package contract

- Version: `1.1.1`.
- Package SHA-256: `8af57e3aff715ed4a7839e62ab9ebbc40126ea6313d3d0a20fa3f11a2652eb02`.
- Sources: 10 deterministic CSV files, 7,405 rows, and 30 controlled quality incidents.
- Workflow: 14 parameterized notebooks and 31 declared tables.
- Formats: `CSV -> DELTA -> DELTA -> DELTA`.
- Expected Gold rows: Customer 360 493, service portfolio 1,261, and geographic summary 12.

## Participant isolation

- The deleted legacy participant resources and Identity user were absent before recreation.
- Recreation assigned code `101`, technical key `u101`, and an exact match between email and Identity username.
- The workspace root is `/Workspace/medallon/u101_<email>/telco_lineage` and the job is `wf_u101_telco_lineage`.
- The lab was redesployed independently; Identity access and the participant code were preserved.

## Live Delta workflow

The shared compute was active with `spark.aidp.lineage.enabled=true`. Both runs
used the same 14-task job and emitted `Telco Customer 360 validated: 31 tables,
30 quality issues, Delta Silver/Gold`.

| Run | Result | Tasks | Duration |
| --- | --- | ---: | ---: |
| `0272edcd-dc78-46e2-9241-8adc687954ec` | Success | 14/14 | 508,565 ms |
| `9491a361-372c-4b6c-90bd-f253a23aa6eb` | Success | 14/14 | 430,695 ms |

Master Catalog reported 31 active tables: 10 Landing, 10 Bronze, 8 Silver,
and 3 Gold. The details endpoint confirmed all 11 Silver/Gold tables as
`tableType=EXTERNAL`, `externalTableDataFormat=DELTA`, and located under the
isolated `users/u101/telco_lineage` prefixes.

## Structured lineage acceptance

Queries used `direction=BOTH`, `maxDepth=8`, `shouldIncludeEdges=true`, and
both `level=ENTITY` and `level=COLUMN`.

- Entity relationships: 13/13 declared direct relationships present.
- Column relationships: 17/17 declared segments present (16 unique; one segment is shared by two paths).
- Customer 360: 5 entity nodes / 4 links; 26 column nodes / 13 links.
- Service portfolio: 6 entity nodes / 5 links; 35 column nodes / 18 links.
- Geographic summary: 4 entity nodes / 3 links; 14 column nodes / 7 links.
- The corrected `customer_360.monthly_value_total -> geographic_service_summary.monthly_value_total` derivation is present.

The official export for Customer 360 is stored in
`evidence/customer_360_lineage.csv` (527 bytes, SHA-256
`6ba3d454446df362eda9d15e51f1dcc73cb4c70d1aca430a6f7c0db4846d6106`).

## Iceberg diagnostic retained

Before the Delta package, multiple native external Iceberg write paths were
tested with the same lineage-enabled compute. DataFrameWriter V2 produced no
entity or column links. DataFrameWriter V1 and SQL overwrite/CTAS produced the
three-node entity graph but no column links. An existing Delta table returned
both levels, isolating the limitation to Iceberg lineage capture in this AIDP
runtime rather than authentication or the lineage endpoints.

## Acceptance decision

The Delta package satisfies functional, idempotence, entity-lineage, and
column-lineage acceptance. Iceberg remains unsuitable for this specific
lineage demonstration until the deployed AIDP runtime exposes equivalent
column lineage for native external Iceberg writes.
