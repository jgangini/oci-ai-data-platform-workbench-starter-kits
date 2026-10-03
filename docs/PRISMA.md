# PRISMA Bogotá

PRISMA is a shared operational project in Starter Kits. `/admin/prisma` configures
the sources and replay; `/prisma/` opens the private God’s Eye viewer through the
same authenticated nginx ingress. It does not provision a project per student.

## Local acceptance

Use `docker/docker-compose.dev.yml` and a private `.env.dev` with
`LOCAL_DEVELOPMENT_MODE=true`. It starts the administration and viewer as separate
containers. The existing localhost ports are 18081 (redirect) and 18444 (HTTPS).
Configure a new fixture account in that profile; never replace production user
passwords. Keep the test account JSON in the ignored `.tmp/prisma-local-access.json`.

Run `python scripts/smoke_prisma_local.py` to check authentication, four source
controls, replay, the nginx viewer boundary, evidence references, stale versions
and pause. The runner is restricted to loopback and labels chat as a local fixture.
Local results do not certify Spark, Oracle memory, IAM or a deployed AIDP agent.

## Deployment

Deploy an immutable Starter Kits release with `enable_prisma_viewer=true` into a
new tenancy/stack. This toggle splits the new network into public/private subnets;
it is not a migration of an existing stack. VM1 exposes HTTPS. VM2 accepts only
VM1 on its viewer port. The reverse bridge to VM1 accepts only VM2 and still
requires the original administrator session.

The post-apply hook first installs the existing encrypted operator runtime and
then creates the PRISMA database package, writer/reader identities, AIDP credentials,
versioned code agent, and finite scheduled Job. The agent reader has SELECT on
curated PRISMA views; it cannot change control documents or issue arbitrary SQL.
The workflow and its source bundle are versioned independently of captured data.

X requires a separate API Bearer credential and sufficient X access/credits. Add
it through the source form; the credential is write-only. Only X supports real
mode. Facebook, Instagram and TikTok are simulations. The collector never treats
HTTP errors as empty searches, and commits its cursor only after durable Bronze.

The Job checks due sources once a minute, using one shared Spark cluster and no
periodic run queue. Explicit actions submit finite runs to the same Job and may
queue behind the current run. The periodic schedule pauses when replay finishes
and no real source is enabled. A five-minute interval is not a latency SLA.
Pause the job and stop idle compute outside demo/test sessions. Do not run the
educational `Redeploy lab` cleanup against PRISMA data.

## Publications and conversation

AIDP Bronze/Silver/Gold are the processing authority. A publication is activated
only after its Autonomous copy and private JSON snapshot exist. Both carry one
version and the same evidence IDs. The viewer rejects chat based on an outdated
version and rejects references/actions absent from that publication.

Questions are forwarded over the authenticated VM bridge and signed by the
existing AIDP operator. The private viewer's instance principal only reads the
published Object Storage objects. No OCI key, X token or database password is
shipped in the browser bundle or release images.

## Demonstration script

1. Open source administration; show simulation versus the separately tested X connector.
2. Start replay and open the Bogotá viewer. Flooding appears in Kennedy/Bosa,
   followed by slope movement and vegetation fire. Every synthetic record is labelled.
3. Filter locality/platform and inspect the original supporting publications.
4. Ask “¿Qué reportes de inundación hay en Kennedy?” and then ask for its evidence.
5. Focus a returned incident using its explicit action button and record a human review.
6. Pause replay. Show the latest pipeline/source timestamps and any connector errors.

## Acceptance evidence

Retain test logs and DOM assertions, not screenshots. Cloud acceptance must show a
successful native Spark run, matching publication versions, a real X record, an
AIDP answer citing that record, conversational follow-up, session isolation, and
recovery after retry/restart. `ACTIVE`, a healthy container, or a simulated chat
alone is not evidence that the integrated deployment works.

The current bounded demo publishes at most 5,000 events per snapshot. For larger
operation replace snapshots with a paginated serving API, add individual operator
identities and retention/erasure policies appropriate to each source. Field
dispatch, autonomous critical decisions and unrestricted social-network scraping
are outside this implementation.

## Bogotá geography

Kennedy is locality 08 and Bosa is locality 07, as described by
[Bogotá's locality directory](https://bogota.gov.co/mi-ciudad/localidades/kennedy).
The six approximate locality anchors were checked against the official IDECA
polygons republished by [CAR](https://sig.car.gov.co/arcgis/rest/services/visor/Division_Territorial/MapServer/5).
Run `PRISMA_VERIFY_OFFICIAL_GEOGRAPHY=1 python -m pytest apps/backend/tests/test_prisma_geography.py`
to repeat the read-only geographic check (set the environment variable using your shell).
These anchors are representative points, not verified incident coordinates.
Ambiguous risk reports stay in the review list without a map point; reports
explicitly outside Bogotá are not promoted to Bogotá incidents.

## Architecture review

The PRISMA change adds deliberate dependencies from administration to its control
runtime, from the post-apply hook to its bootstrap, and from the isolated viewer
to the authenticated administration boundary. Sentrux's coupling regression is
accepted for these requested integrations; removing them would duplicate the
existing auth, deployment and data-access implementations. No new dependency
cycle or god file is accepted. Input validation, collection, deployment waiting
and publication retain explicit failure branches covered by executable checks;
the absolute complex-function count grows with this new subsystem. The gate's
reported warning is documented rather than suppressed or rebased away.
