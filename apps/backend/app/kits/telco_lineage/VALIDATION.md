# Telco Customer 360 Lineage validation

Validated on 2026-08-14 against the live AIDP candidate environment. This
report excludes credentials, OCIDs, tenant URLs, and participant PII.

## Package contract

- Version: `1.1.2`.
- Package SHA-256: `b3718984e4c967007cd623da195c72678b61406ec4d8db821cc4b5c0a80ed054`.
- Sources: 10 deterministic CSV files, 7,405 rows, and 30 controlled quality incidents.
- Workflow: 14 parameterized notebooks and 31 declared tables.
- Formats: `CSV -> DELTA -> DELTA -> DELTA`.
- Storage: all 31 catalog tables are managed so that AIDP lineage retains their qualified catalog and schema identity.
- Expected Gold rows: Customer 360 493, service portfolio 1,261, and geographic summary 12.

## Root cause and correction

The previous package registered catalog tables as external tables with an
Object Storage location. The current AIDP lineage runtime resolved reads from
those tables by their physical path and emitted synthetic source nodes such as
`aidp_lab.telco_lineage.customer_master`. Those nodes had no catalog format or
column metadata, so the visible graph stopped at the Silver-to-Gold boundary.

Version `1.1.2` writes the same deterministic data to managed catalog tables in
the four governed schemas. It does not create a synthetic `telco_lineage`
schema. The resulting physical path is now:

`aidp_lab.oci_landing.* -> aidp_lab.oci_bronze.* -> aidp_lab.oci_silver.* -> aidp_lab.oci_gold.*`

## Participant isolation

- The existing participant identity, code `101`, technical key `u101`, and Banking assignment were preserved.
- The workspace root remains `/Workspace/medallon/u101_<email>/telco_lineage` and the job is `wf_u101_telco_lineage`.
- Only `telco_lineage` was redeployed, idempotently, from package `1.1.2`.

## Live workflow

The shared compute was active with `spark.aidp.lineage.enabled=true`. Both runs
used the same 14-task job and completed successfully.

| Run | Result | Tasks | Client-observed duration |
| --- | --- | ---: | ---: |
| `436a0290-3c05-4b04-9c68-17913e85996d` | Success | 14/14 | approximately 731 s |
| `66086863-f8ff-4f99-ac76-5687cdfb0e49` | Success | 14/14 | approximately 1,282 s |

Master Catalog reported 31 active managed tables with populated columns:

| Layer | Tables | Format | Minimum columns per table |
| --- | ---: | --- | ---: |
| Landing | 10 | CSV | 7 |
| Bronze | 10 | Delta | 9 |
| Silver | 8 | Delta | 6 |
| Gold | 3 | Delta | 10 |

## Structured lineage acceptance

Queries used `direction=BOTH`, `maxDepth=8`, `shouldIncludeEdges=true`, and
both `level=ENTITY` and `level=COLUMN`.

| Gold anchor | Entity nodes / links | Column nodes / links |
| --- | ---: | ---: |
| Customer 360 | 50 / 56 | 85 / 73 |
| Service Portfolio | 54 / 61 | 135 / 119 |
| Geographic Summary | 36 / 46 | 43 / 37 |

Across the three Gold anchors, the physical schema coverage is 10 Landing
tables, 10 Bronze tables, 7 Silver tables, and all 3 Gold tables. The eighth
Silver table, `quality_issues`, is intentionally a validation sink rather than
an input to a Gold table. There are 163 entity links and 229 column links in
the combined responses. No node matches the forbidden
`aidp_lab.telco_lineage.*` prefix.

The second workflow execution returned the same physical layer counts, zero
forbidden nodes, 163 entity links, and 229 column links.

After validation, the shared compute returned from `ACTIVE` through `STOPPING`
to its prior `STOPPED` state. The temporary OCI-local container was removed
without deleting its persistent volume.

## Master Catalog UI acceptance

The Customer 360 lineage panel was validated from structured DOM state, not a
screenshot. The Lineage settings upstream and downstream depths were both set
to 8. The rendered graph contained these exact schema-label counts:

- `oci_landing`: 9
- `oci_bronze`: 10
- `oci_silver`: 7
- `oci_gold`: 3
- `telco_lineage`: 0

The managed source node `u101_telco_lineage_customer_master` was present and
the legacy unqualified `customer_master` node was absent. A lower UI depth can
still intentionally hide earlier layers even though the API contains them.

The official Customer 360 export is stored in
`evidence/customer_360_lineage.csv`. Participant email paths are sanitized.
The file is 6,269 bytes with SHA-256
`3fd7aa3ccda01857e5ec82e1c72b17fdb2c2a07db120d9abbc6c296c6d2ab71c`.

## Iceberg diagnostic retained

Native external Iceberg was previously tested on the same lineage-enabled
runtime. DataFrameWriter V2 produced no entity or column links; Writer V1 and
SQL overwrite/CTAS produced a three-node entity graph but no column links.
Delta produced both levels. The current package therefore uses Delta for the
complete entity-and-column lineage demonstration instead of presenting a
partial Iceberg graph as complete.

## Acceptance decision

Package `1.1.2` satisfies functional, idempotence, managed-table, entity-lineage,
column-lineage, and complete medallion-schema acceptance. The full upstream
path to every Gold table is available through the structured API and is visible
in Master Catalog when the lineage depth is set to 8.
