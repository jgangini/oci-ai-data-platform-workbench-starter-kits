# Territorial Control · God’s Eye View Bogotá

Territorial Control is a shared operational project in Starter Kits.
`/admin/gods-eye-view` configures sources inside Settings → Application;
`/gods-eye-view/` opens the private God’s Eye viewer through the
same authenticated nginx ingress. It does not provision a project per student.
Legacy public URLs redirect to these routes. Internal API, database and storage
identifiers remain stable so the rename does not move or erase existing data.

## Local acceptance

Use `docker/docker-compose.dev.yml` and a private `.env.dev` with
`LOCAL_DEVELOPMENT_MODE=true`. It starts the administration and viewer as separate
containers. Open `http://localhost:18081`; the development profile binds only to
loopback, uses the existing local nginx configuration and requires no certificate
installation. Public OCI deployments retain their separate TLS configuration.
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
HTTP errors as empty searches, and commits its cursor only after durable Landing persistence.

The VM captures due synthetic sources into immutable Landing CSV, with `id` and
JSON `payload` columns; commas, quotes, multiline text and photo metadata retain
their content. Each source defaults to a five-minute interval. Each nonempty
query line is a separate search (up to ten, 512 characters each); query-specific
X checkpoints preserve continuation while original platform IDs deduplicate
overlapping results. A successful empty search produces a header-only CSV;
upstream failures remain errors, never fabricated empty results.

“Run now” starts continuous capture for that source. Disabling the source and
saving stops capture without deleting evidence. Synthetic input repeats the
Bogotá scenario with distinct cycle IDs; ordinary service restarts preserve its
progress. The legacy finite replay API remains available for reproducible tests.

The AIDP Job runs finite `availableNow` file streams once a minute, using an
external Landing Volume, a managed checkpoint Volume and one shared Spark
cluster. This uses Spark file streaming, not the OCI Streaming service. Legacy
NDJSON keeps its checkpoint; CSV uses a separate checkpoint and the same Bronze
MERGE. Real cloud X capture uses the AIDP credential store and writes the same
Landing CSV envelope from AIDP. There is no periodic run queue. Explicit actions
submit finite runs to the same Job and may queue behind the current run. The
schedule pauses when no capture or legacy replay is running. A five-minute
capture interval is not an end-to-end latency SLA.
Pause the job and stop idle compute outside demo/test sessions. Do not run the
educational `Redeploy lab` cleanup against PRISMA data.

## Publications and conversation

AIDP Bronze/Silver/Gold are the processing authority. A publication is activated
only after its Autonomous copy and private JSON snapshot exist. Both carry one
version and the same evidence IDs. The viewer rejects chat based on an outdated
version and rejects references/actions absent from that publication.

The continuous-correlation upgrade changes local publication versions to
`local-v2-{revision}`, invalidating earlier versions without rewriting evidence.
Continuous incident IDs also change when sources are grouped together. Old review
records remain stored; new aggregates start pending and require a new human review,
since merged incidents may have conflicting earlier decisions. Real incidents and
bounded legacy replays retain their review IDs. The pre-upgrade OCI acceptance
publication contained no incidents, so no reviews were affected there.

Questions are forwarded over the authenticated VM bridge and signed by the
existing AIDP operator. The private viewer's instance principal only reads the
published Object Storage objects. No OCI key, X token or database password is
shipped in the browser bundle or release images.

## Demonstration script

1. Open source administration; show simulation versus the separately tested X connector.
2. Press “Run now” for the selected sources and open the Bogotá viewer in its new
   tab. Flooding appears in Kennedy/Bosa, followed by slope movement and vegetation
   fire. Source provenance remains available in each evidence detail.
3. Filter locality/platform and inspect the original supporting publications.
4. Ask “¿Qué reportes de inundación hay en Kennedy?” and then ask for its evidence.
5. Focus a returned incident using its explicit action button and record a human review.
6. Disable the sources and save. Confirm capture stops while published evidence remains.

## Acceptance evidence

Retain test logs and DOM assertions, not screenshots. Cloud acceptance must show a
successful native Spark run, matching publication versions, synthetic VM capture through Landing, an
AIDP answer citing that evidence, conversational follow-up, session isolation, and
recovery after retry/restart. `ACTIVE`, a healthy container, or a simulated chat
alone is not evidence that the integrated deployment works.

The current bounded demo publishes at most 5,000 events per snapshot. For larger
operation replace snapshots with a paginated serving API, add individual operator
identities and retention/erasure policies appropriate to each source. Field
dispatch, autonomous critical decisions and unrestricted social-network scraping
are outside this implementation.

## Bogotá geography

The viewer's public name is Territorial Control. “Filter map area” fixes the
current WGS84 view rectangle and applies it
inclusively to points, the list and agent queries. Unresolved locations stay
outside an active area filter. Clear the area to restore those reports.

Context layers stay separate from incident evidence: NASA GIBS MODIS imagery is
dated daily imagery (clouds may obscure Bogotá), and Open-Meteo weather is a model
estimate for six locality anchors. News lists only linked reports already in the
publication. Camera references link to the official Bogotá source; no public live
camera feed has been verified or invented. NASA uses its documented
[GIBS WMTS interface](https://nasa-gibs.github.io/gibs-api-docs/access-basics/).

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


## Local access and module administration

Open `http://localhost:18081/admin/login` with the explicitly configured local
`admin` / `admin` account. Registration code: `aidp-2026`. HTTP binds only to
loopback; deployed OCI ingress keeps HTTPS. Application settings include the
Territorial Control global module version and a gear link in the Configuration
column. Its configuration replaces the release block within the Application tab;
“Return” restores the versions table. Lab registration code lives at the bottom
of the Workbench tab. Deployment is still owned by Deploy Studio, not a web form.
Source references use `gods-eye-view-{platform}`. Existing configured legacy
credentials continue working until an explicit token update replaces their reference.

Create a local participant with **Territorial Control** selected. Docker writes
private welcome files and identity state to the host `.local/prisma` directory.
The welcome file supplies the generated sign-in credentials at
`/local/gods-eye-view/login`. No email is sent and no OCI identity is created in this
mode. Participants can read published data and ask questions; source changes,
review decisions and administration require the administrator session. Revoking
access invalidates existing sessions. Local lab material survives restarts.
The workspace preview is explicitly simulated; it is not the AIDP cloud UI.

Cloud participant sign-in is not enabled by this local adapter. Until a real
participant identity integration is configured, cloud Territorial Control
assignment requests fail before any user mutation. The cloud demo uses the
existing authenticated administrator session. This remains a separate acceptance
item for multi-user production access.

## Evidence and context

The incident card leads with the event category. The operational view combines
all sources without an Origin filter or separate real/synthetic counters; each
evidence item retains provenance. Synthetic records are never relabelled as real.
The chat composer has a circular send control and a per-conversation submitted
question count, including failed or cancelled requests. Corroboration is a bounded heuristic based
on independent author/platform sources after near-copy suppression, not a
probability or automatic confirmation. Classification confidence remains separate.
Only real X photos from the allowed media host can appear in a human-validated
incident. Synthetic, pending and rejected incident photos are never rendered.

Area filtering uses the visible map bounds and carries the same validated bounds
into the agent query, evidence validation and focus actions. Approximate locality
anchors remain approximate. A map filter never changes the underlying publication.

Postflight for the Territorial Control increment (2026-10-03): quality 6712 →
6722, coupling 0.09 → 0.08, cycles 0, god files 0. The gate reports an intentional
increase from 7 to 14 complex functions across source filtering, media/area
validation, persisted local access and cloud activation checks. These explicit
failure paths implement requested boundaries and are covered by unit/integration
checks; the warning is retained and is not reported as a passing gate. No baseline
reset is used to suppress it.

Postflight for the continuous CSV capture and viewer navigation increment
(2026-10-03), including the newly tracked tests: quality 6722 → 6689,
coupling 0.08, cycles 0, god files 0. Sentrux gate passed without architectural
degradation. The separate rules check is not configured because this repository
has no `.sentrux/rules.toml`; no constraints were invented. The final
refactor removed duplicate validation and repeated capture-window selection;
the historical v2.3.2 warning above remains part of the record.
