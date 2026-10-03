# Territorial Control · God’s Eye View Bogotá

Territorial Control is a shared operational project in Starter Kits.
`/admin/gods-eye-view` configures sources inside Settings → Application;
`/gods-eye-view/` opens the private God’s Eye viewer through the
same authenticated nginx ingress. It does not provision a project per student.
Legacy public URLs redirect to these routes. Internal API, database and storage
identifiers remain stable so the rename does not move or erase existing data.

The viewer now boots the original pinned God’s Eye View application and its
native Data Layers, globe controls, scenes and voice UI. Territorial Control is
an additional catalog layer with floating evidence and text-assistant panels.
The earlier standalone shell is retained in source for compatibility, not used
as the native image's entry point. The [viewer integration README](../apps/prisma-viewer/README.md)
documents the exact source/archive checksums, six verified patches, exclusions
and upstream-update procedure. `.upstream/` is generated and never committed.
The noncommercial Nepal event and cable datasets are excluded; all other native
layer controls remain, subject to provider access and geographic coverage.

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

Local publications now use `local-v3-{revision}-{content digest}`. Existing event
memberships seed the registry before an upgrade; subsequent reports retain the
event ID across hour boundaries and rule changes. Human reviews remain separate.
Reviewing an event records its current evidence IDs. A photo added afterwards is
not covered by that review and remains hidden until reviewed; legacy reviews
without evidence membership also require review before photos appear.

Questions are forwarded over the authenticated VM bridge and signed by the
existing AIDP operator. The private viewer's instance principal only reads the
published Object Storage objects. No OCI key, X token or database password is
shipped in the browser bundle or release images.

The AIDP Analyst, OCI Assistant and original OpenAI voice are separate paths.
AIDP uses the publication/evidence contract above. OCI Assistant provides general
text inference through the configured server-side OCI model; its answers are not
incident evidence. Original voice retains upstream OpenAI Realtime and native
tools. OCI text is not a replacement implementation of that voice protocol.
“Power up the globe” reports real native-provider configuration; native keys stay
server-managed. Administrators may select/test an OCI model from the actual
catalog without entering OCI credentials or arbitrary endpoints in the browser.
The two upstream browser-side credentials, Google Maps and Cesium, are the explicit
exception: authenticated `/api/setup/browser` loads only those two values at
startup. They require restricted provider scopes/domains; OCI, social-network and
private keys never use this endpoint. Empty values mean keyless operation, while
configuration transport/format failures produce a visible startup error.
The private Node runtime is reachable only through the authenticated, fixed-target
proxy and approved API paths; the development key writer and MCP transport are
not exposed.

## Demonstration script

1. Open source administration; show simulation versus the separately tested X connector.
2. Press “Run now” for the selected sources and open the Bogotá viewer in its new
   tab. Flooding appears in Kennedy/Bosa, followed by slope movement and vegetation
   fire. Source provenance remains available in each evidence detail.
3. Filter locality/platform and inspect the original supporting publications.
4. In AIDP Analyst, ask “What flooding reports are available in Kennedy?” and
   then ask for evidence. A local-fixture answer must remain labelled as such;
   a provider error is an unsuccessful step, not a simulated live-agent answer.
5. Focus a returned incident using its explicit action button and record a human review.
6. Pause the sources. Confirm capture stops while publications, checkpoints and reviews remain.

## Publication administration and corpus

Network tabs preserve drafts while polling operational state. Save uses an
optimistic configuration revision; it never starts capture. Test does not create
Landing data. Run starts or resumes one cycle; Pause retains its namespace and
cursor. New capture after disabling/re-enabling starts a new namespace.

The publication table uses signed keyset cursors and a fixed insertion sequence
ceiling. Late posts do not move rows between pages. Refresh deliberately opens a
new first-page snapshot; expired cursors require Reload latest. Autonomous tracks
`captured`, `ingested`, and `processed` monotonically. The local fixture marks its
synchronous local processing separately from the native deployment context.

The versioned corpus contains 120 posts, 30 per network, with 24 distinct main-case
reports per network, late posts, copies, ambiguous reports and contradictions.
Six original CC0 SVG diagrams are explicitly synthetic and nonphotographic.
They are served through authenticated administration routes with checksums and a
sandboxed response policy. The eight false-claim labels remain exclusively in
`datasets/synthetic/social-media/natural-hazards/colombia/bogota/v1/evaluation/`;
Docker images and the native bundle exclude evaluation data.

Run `python scripts/evaluate_prisma_corpus.py --predictions predictions.json` for
offline category/stance precision and recall over one run/cycle. Without actual
predictions it reports pending metrics. Textual support or contradiction does not
establish truth; the evaluator does not turn synthetic ground truth into model
input or claim automatic falsehood detection.

## Native processing evolution

Bronze/Silver/Gold expose logical `social_posts_raw`, `social_posts`, `events` and
`event_posts` views over their existing Delta data. The existing Landing prefix
and JSON/CSV checkpoints remain in place; upgrades do not relocate captured
objects or create a second Parquet copy. `source_hash_kind=batch_identity_sha256`
identifies a filename hash of the envelope plus batch identity, not a separately
verified checksum of the original platform object.

Both file streams start before either is awaited. Ingestion persists independently
of model enrichment. At most ten pending posts are enriched per tick; five failures
open an observable circuit, retain pending posts and stop inference retries until
an explicit Run or configuration change. An enrichment journal recovers an
Autonomous projection failure after a successful Silver write.

The installer accepts `streaming_mode=persistent` for a task with `isStreaming`
and concurrency one. This mode reads control configuration once per processing
iteration; normal Job parameters are not treated as live configuration. The
default remains finite until the deployment passes native persistent acceptance.
Local Spark tests alone do not certify this AIDP mode.

The model extracts up to eight quoted assertions per post. One post can relate to
multiple events without duplicating the post. Relation labels describe textual
support, contradiction or copies; legacy unclassified relations remain explicit.
The model cannot edit human reviews. Event titles and summaries are English;
original source messages retain their original language.

Spark SQL calculates distinct-content report counts and Low/Medium/High activity
within each network's configured window (defaults 30 minutes and 5/10/20).
Activity, severity, model confidence, independent corroboration and human
confirmation remain separate. Source configuration versions travel with metrics.

## Acceptance evidence

Retain test logs and DOM assertions, not screenshots. Cloud acceptance must show a
successful native Spark run, matching publication versions, synthetic VM capture through Landing, an
AIDP answer citing that evidence, conversational follow-up, session isolation, and
recovery after retry/restart. `ACTIVE`, a healthy container, or a simulated chat
alone is not evidence that the integrated deployment works.

The v2.3.8 processing baseline has positive native CSV-processing evidence.
Live AIDP chat is a separate outstanding acceptance item: the deployed agent
returned HTTP 429 even with a 2,048-token output bound and matching expected
provider/configuration values. The root cause has not been confirmed. A direct
OCI inference success does not prove the AIDP runtime path. Native overlay tests,
local fixture responses and Docker health must not be reported as live AIDP,
OpenAI voice or third-party feed acceptance.

The current bounded demo publishes at most 5,000 events per snapshot. For larger
operation replace snapshots with a paginated serving API, add individual operator
identities and retention/erasure policies appropriate to each source. Field
dispatch, autonomous critical decisions and unrestricted social-network scraping
are outside this implementation.

## Bogotá geography

Territorial Control is a layer inside the original God’s Eye View. “Filter map area” fixes the
current WGS84 view rectangle and applies it
inclusively to points, the list and agent queries. Unresolved locations stay
outside an active area filter. Clear the area to restore those reports.

Native imagery, weather, CCTV and other context layers keep their original source
labels and provider requirements; they are separate from Territorial Control
evidence. Native CCTV coverage in another city does not establish a Bogotá feed.
No publicly authorized live Bogotá camera feed has been verified for this demo.
The retained `/api/prisma/context` endpoint provides six Open-Meteo model anchors
and an official camera-reference link, but the native entry point does not render
the previous standalone weather/NASA/news widgets. Do not claim those old widgets
or their previous checks as native-layer acceptance.

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
