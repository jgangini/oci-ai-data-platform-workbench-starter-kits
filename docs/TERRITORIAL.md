# Territorial Control · God’s Eye View Bogotá

Territorial Control is a shared operational project in Starter Kits.
`/admin/gods-eye-view` configures sources inside Settings → Application;
`/gods-eye-view/` opens the private God’s Eye viewer through the
same authenticated nginx ingress. It does not provision a project per student.
Legacy public URLs redirect to these routes. New code uses the `territorial`
package, `/api/territorial` and `/api/admin/territorial`; the older API routes
remain authenticated compatibility aliases. Database and storage identities
remain stable so the rename does not move or erase existing data.

During rolling upgrades, nginx accepts both viewer API prefixes and rewrites
`/api/territorial/*` to the legacy `/api/prisma/*` wire path, including voice.
The request stays in the same authenticated location with its original body
limit and query string. Both identity headers are overwritten by authentication;
this lets a new administration VM use an older viewer until its update finishes.

## Naming compatibility

Terraform uses `territorial` resource addresses with explicit `moved` blocks for
the previous addresses. `enable_territorial_viewer` takes precedence when set;
the legacy `enable_prisma_viewer` input and `prisma_viewer_*` outputs remain
available beside the canonical outputs. Existing OCI display names, principals,
policies and their restricted object paths stay unchanged. No migration APPLY
is performed by the source rename.
The Deploy Studio form retains the serialized key `enable_prisma_viewer` with
its neutral label. This prevents a newly defaulted canonical field from
overriding saved deployments that already enable the viewer. Direct Terraform
callers may use `enable_territorial_viewer`; an explicit value still takes
precedence over the legacy input.

Persisted compatibility identities include `PRISMA_*` database objects and
writer/reader credentials, `prisma_ingest`, the existing Delta tables and
Object Storage prefixes, `prisma_bogota` agent memory, the `prisma_tick` task,
and its `prisma-run` idempotency namespace. Local `prisma.sqlite3`,
`prisma-secrets`, `prisma-landing`, `.local/prisma` and the existing cache volume
are retained. New signed sessions retain the `local-prisma` subject for rollback
compatibility. Readers accept both `local-prisma` and transitional
`local-territorial` subjects; both authenticated identity headers normalize to
the legacy wire subject so an older viewer preserves its access restrictions.

New workflow bundles contain `territorial/*` and a small `prisma` import shim
for already deployed notebooks. New releases publish canonical territorial
viewer assets plus byte-identical archives under the legacy asset names;
immutable historical releases continue to load through the compatibility path.
The VM container names, `/opt/prisma`, update lock and installed update command
remain stable across rollback. Cloud-init writes both `TERRITORIAL_*` variables
and their `PRISMA_*` aliases with equal values so an older image can start after
a rollback; new images prefer the canonical variables. The development Compose service is now
`territorial-viewer`; private Compose overlays must target that name and remove
the previous orphan service before starting the replacement. Keep its existing
`prisma-native-cache-dev` volume.

The viewer now boots the original pinned God’s Eye View application and its
native Data Layers, globe controls, scenes and voice UI. Territorial Control is
an additional catalog layer with floating evidence and text-assistant panels.
The earlier standalone shell is retained in source for compatibility, not used
as the native image's entry point. The [viewer integration README](../apps/territorial-viewer/README.md)
documents the exact source/archive checksums, six verified patches, exclusions
and upstream-update procedure. `.upstream/` is generated and never committed.
The noncommercial Nepal event and cable datasets are excluded; all other native
layer controls remain, subject to provider access and geographic coverage.

## Viewer identity and administration

Settings → Application → God's Eye View includes **Viewer identity** directly
below the viewer URL. Its name (80 characters) and description (200 characters)
are persisted with the application settings. Empty fields restore `God's Eye View`
and `NO PLACE LEFT BEHIND`. The authenticated HTML response applies escaped text
to the browser title, loading title and viewer header before scripts start;
default branding retains the upstream markup. HTML is not cached across changes.

Social and sensor schedules use the same heading spacing, with Save beside the
capture interval on desktop and mobile. Publication loading is independent of
source configuration loading. The Users table displays its spinner while a
request is pending, then the actual results, empty state or error.

Synthetic cleanup uses the standard modal progress indicator, reports historical
publication progress, and closes only after the same operation and scope have
confirmed completion. Interrupted requests resume the existing reset; status
polling and source polling cannot overwrite a completed operation with stale data.
Global module installation is available in Settings → Application; administrator
rows in Users no longer expose that global action.

## Local acceptance

Use `docker/docker-compose.dev.yml` and a private `.env.dev` with
`LOCAL_DEVELOPMENT_MODE=true`. It starts the administration and viewer as separate
containers. Open `http://localhost:18081`; the development profile binds only to
loopback, uses the existing local nginx configuration and requires no certificate
installation. Public OCI deployments retain their separate TLS configuration.
Configure a new fixture account in that profile; never replace production user
passwords. Keep the test account JSON in the ignored `.tmp/prisma-local-access.json`.

Run `python scripts/smoke_territorial_local.py` to check authentication, four source
controls, replay, the nginx viewer boundary, evidence references, stale versions
and pause. The runner is restricted to loopback and labels chat as a local fixture.
Local results do not certify Spark, Oracle memory, IAM or a deployed AIDP agent.

## Deployment

Deploy an immutable Starter Kits release with `enable_territorial_viewer=true` into a
new tenancy/stack. This toggle splits the new network into public/private subnets;
it is not a migration of an existing stack. VM1 exposes HTTPS. VM2 accepts only
VM1 on its viewer port. The reverse bridge to VM1 accepts only VM2 and still
requires the original administrator session.

The post-apply hook first installs the existing encrypted operator runtime and
then creates the Territorial database package, writer/reader identities, AIDP credentials,
versioned code agent, and permanent Social/Sensors Jobs. The agent reader has SELECT on
curated Territorial views; it cannot change control documents or issue arbitrary SQL.
The workflow and its source bundle are versioned independently of captured data.

X requires a separate API Bearer credential and sufficient X access/credits. Add
it through the source form; the credential is write-only. Only X supports real
mode. Facebook, Instagram and TikTok use Synthetic mode. The collector never treats
HTTP errors as empty searches, and commits its cursor only after durable Landing persistence.

The canonical source and event `mode` value for new writes is `Synthetic`;
`real` remains the credential-backed value. Legacy `simulation` input remains
compatible and is normalized during processing; the existing source migration
also updates legacy source configuration. Cleanup recognizes both values, so
previously captured Synthetic data can still be reset. This changes the mode
value, not the legacy replay API or its `simulation` control document, and does
not require deleting existing data to upgrade. Existing Delta and Autonomous
history is not retroactively rewritten by this code change; cloud installations
need the updated native workflow before producing canonical new publications.
Synthetic reset requires contract version 2 from both the database package
(`RESET_VERSION()`) and the native Job (`runtime.synthetic_reset_version`).
The Job verifies the database contract before advertising support; the API
rejects reset until that support is present, preventing older workers or
packages from reporting success while leaving canonical Synthetic records.

The VM captures due synthetic sources into immutable Landing CSV, with `id` and
JSON `payload` columns; commas, quotes, multiline text and photo metadata retain
their content. Social Networks has one UTC start date and capture interval for
all four sources (the form shows the browser's time zone). Sensors has a separate
shared schedule for all five families. Both default to five minutes until a
schedule is configured; saving a schedule preserves pauses and checkpoints.
Scheduled captures use the latest start-plus-interval slot and skip missed slots;
Run cannot advance a future scheduled capture. Each nonempty
query line is a separate search (up to ten, 512 characters each); query-specific
X checkpoints preserve continuation while original platform IDs deduplicate
overlapping results. A successful empty search produces a header-only CSV;
upstream failures remain errors, never fabricated empty results.

“Run now” activates capture for that source, including previously disabled
configurations. Real X capture continues until paused; a Synthetic run consumes
its 600-second corpus once, then reports completed and pauses automatically.
Each Synthetic source has a maximum per capture (default 3, range 1–100).
Each capture randomly selects up to that many eligible, unseen articles. A
durable pending batch preserves exact bytes on retries, and the emitted-ID
checkpoint prevents repeats across later intervals. Remaining records continue
over subsequent captures even after the corpus's 600-second timeline ends.
“Pause” stops capture without deleting
publications or checkpoints. The interface shows only Running or Paused;
capture errors remain visible separately. “Save” only updates configuration
and “Test” checks it without starting capture. An exhausted Synthetic cursor stays
exhausted after Run, query changes and service restarts; articles are not repeated
with newer timestamps. Existing multi-cycle cursors stop without deleting their
prior records. A separately selected new run is required for new Synthetic input.
Run can still retry unfinished processing of already captured data, including an
open enrichment circuit or the last pending upload, without regenerating articles.
The legacy explicit finite replay API remains available for reproducible tests.

The trash button beside the source toolbar opens **Delete all Synthetic data?**.
Confirmation applies to all networks, independently of publication search or
network filters. It stops Synthetic capture and removes its loaded posts,
events and associated reviews while keeping real data, searches, thresholds,
credentials and real-source checkpoints. Use **Run now** to start a fresh demo.
Opening or cancelling the dialog does not delete anything.

`POST /api/admin/territorial/synthetic/reset` requires `confirm: true` and a UUID
`operation_id`; `GET` on the same endpoint reads the durable operation status.
Retries must reuse that ID, including after a lost response. Completed IDs
cannot delete newer demo data. An unfinished reset blocks Synthetic production;
failure is shown as an error until the same operation is retried.
If a stopped process leaves the operation pending, the toolbar retry resumes
that same operation instead of creating a second deletion request.

In local fixture mode, the reset cleans SQLite and verified Synthetic Landing
CSV files. The confirmation and progress identify this local scope; a fast local
completion does not indicate that an AIDP Job ran. The administration page
shows an indeterminate progress bar and the persisted cleanup stage while the
operation is pending. After confirmed completion, the progress and message
disappear and the publications table refreshes. Verification errors remain visible.
Stages describe work being performed, not an estimated percentage or duration.
In AIDP, Python cleanup runs inside the existing native Job, after
stopping and draining file streams with their original checkpoints. It cleans
the underlying Bronze/Silver Delta tables and Autonomous post projection,
rebuilds the current publication, and replaces historical mixed publications
with new immutable versions containing their real records. It then removes the
superseded Synthetic-bearing publications from Gold, Autonomous and Object
Storage. Business `social_posts`, `events` and `event_posts` are views over
these stores; truncating only the views would not reset the demo.

Real records in mixed Landing files are retained in a replacement file before
the original is removed. Unknown or empty files without attributable Synthetic
content are retained. Dataset source fixtures and streaming checkpoints are
not deleted, and Delta retention/time-travel policy is unchanged. Completion
is reported only after all serving stores and the current publication agree.
Existing cloud installations require the updated database package and native
workflow; the API refuses the reset until the native runtime advertises support.
Local tests do not certify live Oracle/Spark permissions or execution.

Two permanent AIDP Jobs use the governed Landing and checkpoint Volumes with
separate, dedicated Spark clusters. This is Spark file streaming, not OCI
Streaming. Social preserves the legacy NDJSON checkpoint and the separate CSV
checkpoint, both writing the same Bronze MERGE. Sensors uses its own TXT stream
and checkpoint. Real cloud X capture uses the AIDP credential store and writes
the same Landing CSV envelope from AIDP. Explicit actions reuse an active
workflow; they do not queue overlapping streaming runs. Pausing a producer stops
new capture while workflows and their compute remain running. A five-minute
capture interval is not an end-to-end latency SLA. Stopping always-on compute is
a separate operational action; the VM watcher must also be disabled during
maintenance. Do not run the educational `Redeploy lab` cleanup against Territorial data.

## Publications and conversation

AIDP Bronze/Silver/Gold are the processing authority. A publication is activated
only after its Autonomous copy and private JSON snapshot exist. Both carry one
version and the same evidence IDs. The viewer rejects chat based on an outdated
version and rejects references/actions absent from that publication.

Local publications now use `local-v3-{revision}-{content digest}`. Existing event
memberships seed the registry before an upgrade; subsequent reports retain the
event ID across hour boundaries and rule changes. Human reviews remain separate.
Reviewing an event records its current evidence IDs; attachments remain visible
with their provenance independently of the human decision.

The review editor has mutually exclusive **Validated** and **Rejected** checkboxes;
neither selected means pending. **Save** opens the viewer's confirmation dialog.
Cancel or Escape leaves the event unchanged. The confirmed request
includes the event's full evidence membership; if those IDs changed, the API
returns `409` and the operator must inspect the refreshed event before retrying.
The viewer retains the note, coordinate draft and save feedback across publication refreshes.
Latitude/longitude can be typed or previewed by dragging an unvalidated marker.
Coordinates persist only after Save. A validated event must first be saved as
unvalidated before its location can change; the backend enforces the same lock.
Human coordinates are incident overrides (`location_method=human_review`), never
rewrites of a source publication's location.

In local mode, the existing Python store commits the decision to SQLite before
returning it. In AIDP, `cloud.py` saves the review in the Autonomous control
document and wakes the existing native Job. `pipeline.py` reads that document
on every tick, including ticks without new posts, and publishes the reviewed
event to Delta Gold, Autonomous and Object Storage before advancing the current
pointer. No separate update job or direct update of raw social posts is needed.
The UI distinguishes a saved decision awaiting publication from one visible in
the published snapshot, comparing status, note, coordinates and reviewed evidence IDs.
If starting publication fails after the decision was saved, the response reports
both facts and the operator can retry without losing the note.

`oci_medallion.oci_gold.events` is a versioned view over Gold publications;
older `pending` rows remain as history. To inspect current event status and
notes, filter `publication_version` to the `version` in
`04_gold/prisma/current.json` (also returned by `/api/territorial/snapshot`), and read
`review_status`, `review_note` and `reviewed_evidence_ids`. Existing AIDP
installations need the updated native code bundle and view definition to expose
the two review-detail columns. Local and mocked pipeline checks do not certify
a deployed Oracle/Spark update.

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
Use **Administration → God’s Eye View → Parameters** (next to Sensors), or
the administrator-only **Manage parameters** link in “Power up the globe”.
Provider fields never return saved keys; leaving a field blank keeps its value.
**Test** checks draft credentials without saving, while **Save** confirms the
changed field names before replacing them. Google supports separate browser and
optional server keys; OpenSky uses its client ID and secret. Probe results report
the checked scope; the OpenAI check verifies catalog access, not Realtime inference.

Provider overrides are encrypted with RSA-OAEP/AES-GCM in the private viewer’s
persistent `.gev-cache/provider-settings` directory. Save also sends an encrypted
envelope over the private VM bridge. Preserve that directory, including its private
key, when backing up or migrating the viewer; neither belongs in a release image.
The existing single bridge process owns writes and rejects stale revisions. The
supervisor reloads only the native Node worker to clear provider caches; it does
not reboot the VM or restart the administration bridge. The form distinguishes
saved settings pending reload from an applied revision. OCI model/voice selections
use the existing persistent settings and apply on the next request without reload.
OCI voice **Test** generates a short fixed speech sample and checks audio inference
and speech output, without microphone access or executing map actions.

The two upstream browser-side credentials, Google Maps and Cesium, are the explicit
exception: authenticated `/api/setup/browser` loads only those two values at
startup. They require restricted provider scopes/domains; OCI, social-network and
private keys never use this endpoint. Empty values mean keyless operation, while
configuration transport/format failures produce a visible startup error.
Reload the globe page after changing either browser key.
The private Node runtime is reachable only through the authenticated, fixed-target
proxy and approved API paths; the development key writer and MCP transport are
not exposed.

## Demonstration script

1. Open source administration; show Synthetic mode versus the separately tested X connector.
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

The default `bogota-v1` corpus contains 120 posts, 30 per network, with 24 distinct main-case
reports per network, late posts, copies, ambiguous reports and contradictions.
Messages use distinct fictional voices: worried residents, shopkeepers, commuters,
community alerts, requests to authorities and cautious updates. Length, punctuation,
emoji use and spatial references vary; contextual directions describe the fictional
scene without asserting a real household address. Copies retain the original wording,
and late observations, uncertainty, exaggerated claims and rebuttals retain their
evaluation roles. Hashtags remain searchable without a uniform template suffix.
The 72 image attachments are WebP derivatives of 72 distinct AI-generated photorealistic PNGs,
covering flooding in Kennedy and Bosa, vegetation fire in Chapinero, a landslide
in Ciudad Bolívar, and rain in Suba. Receding-water and residual-smoke reports
use corresponding recovery scenes. Each publication has its own original image,
with a viewpoint and subject matched to its message; photos are never assigned
to another fictional author. Duplicate content hashes and mismatched scenario
phases are rejected when refreshing the corpus. They depict fictional situations, not actual
incident photographs. Full generation prompts and asset checksums are recorded
in the corpus's `media/generation.json`; no third-party photographs are used.
`scripts/refresh_territorial_corpus.py` verifies the supplied media and provenance,
then updates attachment metadata without changing posts, identities or chronology.
It does not generate substitute SVG diagrams. Existing captured records remain
immutable; verified fixture rows in the admin use current wording, names and media,
including legacy SVG and PNG URLs. Original PNG checksums and WebP encoding settings
remain in the provenance record; conversion preserves image dimensions without cropping.
New captures use the revised messages; published
evidence and its literal classification quotes retain their captured wording.
Images are served through authenticated routes with
checksums and a sandboxed response policy. The eight false-claim labels remain exclusively in
`datasets/synthetic/social-media/natural-hazards/colombia/bogota/v1/evaluation/`;
Docker images and the native bundle exclude evaluation data.

The separate `bogota-v2` corpus adds 16 posts, four per network, with four new
AI-generated PNGs preserved byte-for-byte. Twelve posts carry images: each scene
has one original image and two explicitly attributed republications, plus a
text-only clarification or limited observation. Shared hashes and `reused_from`
retain provenance; different captions do not turn one image into independent
visual witnesses. Human messages include uncertainty, emotional reactions,
corrections and a delayed report without revealing evaluation labels. Synthetic
metadata remains explicit, and representative locality anchors are not precise
incident coordinates. V2 is selected explicitly for a new run; v1 files, existing
cursors and captured wording remain unchanged. Both authenticated media routes
include the v2 version in their URL. The classifier currently reads text only;
these photographs are displayed as attachments and support duplicate detection,
not image interpretation. The 600-second corpus does not simulate a 24-hour delay.

Run `python scripts/evaluate_territorial_corpus.py --predictions predictions.json` for
offline category/stance precision and recall over one run/cycle. Without actual
predictions it reports pending metrics. Textual support or contradiction does not
establish truth; the evaluator does not turn synthetic ground truth into model
input or claim automatic falsehood detection.

## Native processing evolution

Bronze retains social history in `oci_bronze.prisma_events`, exposed through
`oci_bronze.social_posts_raw`; enriched posts remain in `oci_silver.prisma_events`
and its `social_posts` view. The single publisher materializes consolidated
state in the Delta table `oci_silver.prisma_current`. Queryable Silver `events`
and `event_posts` views expose current events, review fields, activity and
post relationships. Gold is produced by reading that durable Silver state,
preserving the same version in `oci_gold.prisma_publications`, Autonomous and
the private snapshot consumed by the VM viewer. Gold `events` and `event_posts`
views retain publication history. The existing Landing prefix
and JSON/CSV checkpoints remain in place; upgrades do not relocate captured
objects or create a second Parquet copy. `source_hash_kind=batch_identity_sha256`
identifies a filename hash of the envelope plus batch identity, not a separately
verified checksum of the original platform object.

Both file streams start before either is awaited. Ingestion persists independently
of model enrichment. At most ten pending posts are enriched per tick; five failures
open an observable circuit, retain pending posts and stop inference retries until
an explicit Run or configuration change. Each validated post is journaled and
confirmed in Silver and Autonomous before the next classification. Recovery
finishes an interrupted projection and retries the remaining selected posts,
without reclassifying the completed prefix. New classifier responses contain at
most one claim per `(category, locality)`, representing the author's overall
stance on the risk's existence. Different risks and localities remain separate;
historical claims and correlations are retained. Intensity changes, precautions
and rejecting a premature all-clear do not negate the risk. Summaries attribute
allegations to their author. An irreconcilable stance can return `por_clasificar`
with `claims=[]` for human review; the original report is preserved. These prompt
instructions require native semantic acceptance and do not guarantee truth.
The model selects an `evidence_span_id` from a per-post schema enum instead of
rewriting a quotation. Short posts use the complete original text; longer posts
offer at most 23 overlapping literal fragments of up to 1,000 characters from
the bounded 12,000-character input. The selected fragment becomes `evidence_text`
and still passes the existing literal-quote validator. Unknown references and
free-form quotations are rejected, without approximate matching or lexical
classification. Selection guarantees source wording, not semantic correctness;
the full post remains available to evaluate the author's overall stance.
A repeated risk/locality allows one corrective model response for that post,
with the same schema and unique-key checks (two calls total). Other provider or
validation failures stay pending without this correction. Operational status
does not store arbitrary exception text or model output; the older
`last_error_reason=nonliteral_claim` remains recognizable during recovery.

The bootstrap sets `streaming_mode=persistent` for tasks with `isStreaming`
and concurrency one. Permanent Job requests omit `timeoutSeconds`: the native
API rejects explicit zero, while an omitted timeout defaults to unlimited
(zero or absent in the response). Bootstrap rejects a retained finite timeout,
including the previous 600-second setting. Schedule updates omit zero/null
timeouts returned by native GET and preserve positive configured values.
This mode reads control configuration once per processing
iteration; normal Job parameters are not treated as live configuration. The
finite `availableNow` entry remains available to isolated acceptance helpers,
without changing the permanent deployment default. Local Spark tests alone do
not certify native AIDP readiness.

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

On 2026-10-04, the native AIDP code agent using `google.gemini-2.5-flash-lite`
passed a sensor question and a same-locality social-corroboration question. It
cited the published sensor reading, disclosed its synthetic origin and correctly
reported that no social corroboration was available. The accepted candidate was
promoted with an ETag compare-and-swap on the agent pointer. A further question
through the normal authenticated Docker viewer route returned a newly captured
river-level reading with the exact value, unit, status, observation time, event
ID and publication version. Those expected values, timestamp and event ID were
not included in the question; the response reported runtime `aidp`.

The fresh sensor batch was verified through Landing, Bronze, Silver latest state,
Gold and Autonomous serving. An exact-byte replay advanced the stream checkpoint
without duplicate readings or changes to the original payload and first-source
provenance. Gold and serving publication versions matched.

The Social workflow also processed a newly captured Synthetic X post through
Landing CSV, Bronze and Silver Delta tables, Gold and Autonomous. The original
post recovered after a classifier response-format correction without recapture;
an exact-byte replay left Bronze and Silver at 18 distinct posts, with no pending
enrichment, orphan relations or duplicate records. Sensor history and latest
state remained unchanged, and all temporary SQL sessions were deleted.
A normal Docker chat then returned HTTP 200 with the recovered post and a
same-locality sensor citation, matching the selected publication, category,
measurement, unit, status and observation time. The gateway accepts the observed
complete JSON fence, including a standalone leading period, while rejecting
prose, ambiguous blocks and references outside the published evidence.

Native responses expose trace containers, but their tool spans and arguments
have not been verified. Governance was not deployed in OCI during this acceptance;
its Agent and catalog synchronization still require separate live validation.
These results do not certify OpenAI voice or third-party feeds, and local
fixture responses or Docker health alone remain insufficient acceptance evidence.

The current bounded publication includes at most 5,000 events per snapshot. For larger
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
The retained `/api/territorial/context` endpoint provides six Open-Meteo model anchors
and an official camera-reference link, but the native entry point does not render
the previous standalone weather/NASA/news widgets. Do not claim those old widgets
or their previous checks as native-layer acceptance.

Kennedy is locality 08 and Bosa is locality 07, as described by
[Bogotá's locality directory](https://bogota.gov.co/mi-ciudad/localidades/kennedy).
The six approximate locality anchors were checked against the official IDECA
polygons republished by [CAR](https://sig.car.gov.co/arcgis/rest/services/visor/Division_Territorial/MapServer/5).
Run `PRISMA_VERIFY_OFFICIAL_GEOGRAPHY=1 python -m pytest apps/backend/tests/test_territorial_geography.py`
to repeat the read-only geographic check (set the environment variable using your shell).
These anchors are representative points, not verified incident coordinates.
Ambiguous risk reports stay in the review list without a map point; reports
explicitly outside Bogotá are not promoted to Bogotá incidents.

## Architecture review

The Territorial change adds deliberate dependencies from administration to its control
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

### Simulated Colombia sensors

The VM sensor producer writes 4,000 explicitly `Synthetic` readings every five
minutes by default, configurable independently of social capture. The Sensors
tabs each own their station count, Save, Run now and Pause controls. One start
date and interval below Sensors applies to all families. The mode selector
allows Synthetic only; real sensor ingestion is not implemented and the server
rejects non-Synthetic modes rather than simulating real measurements.
Their last/next capture, successful record count and capture delay are reported
per family. The map's aggregate status uses the earliest next capture among
running families and the latest completed capture, including existing family
intervals before a shared start date is configured.
Run now starts only the selected family; Pause preserves its readings
and checkpoint without stopping the other families or the permanent AIDP stream.
Running families are limited to 5,000 configured stations in total. Existing
shared settings are split using their original family order (4,000 across five
families becomes 800 each), retaining station identities and pending retry bytes.
Each sensor family has one UTF-8 NDJSON `.txt` object under
`01_landing/prisma/raw/sensors/<sensor_type>/`. The five families are river level,
rainfall, temperature, soil moisture and wind speed. Station positions and warning
thresholds are fictional; they are not an official monitoring network.

An independent permanent AIDP sensor workflow reads those files through the
governed Landing volume, with its own `sensors-v1` checkpoint. Validated records
merge by immutable `event_id` into `<catalog>.oci_bronze.sensor_events`,
partitioned only by UTC `event_date`. The same microbatch then merges one row
per `sensor_id` into `<catalog>.oci_silver.sensors_current`. Observed timestamp
and event ID determine the newest row; late arrivals remain in Bronze without
replacing a newer Silver reading. The checkpoint advances only after both
layers succeed. Retries do not duplicate readings or accept conflicting values.

The previous `<catalog>.oci_silver.sensor_events` historical table and its
location are retained. On restart, the sensor workflow idempotently copies its
history into Bronze and rebuilds Silver current state before publishing its
new readiness heartbeat. It reuses the existing file checkpoint, including
already-consumed files. A reset drain receipt is issued only after this
migration; an existing receipt prevents migration during deletion. Selected
sensor Delete clears that family from all three tables so a restart cannot
restore deleted readings. The ordinary social reset preserves sensor history,
Landing files and checkpoints.

Silver retains the latest state of every sensor identity without an implicit
24-hour cutoff. Paused stations keep their last `observed_at`. Each Gold
publication reads those Silver rows, selecting at most the 5,000 most recently
observed stations when configuration changes leave more identities. Explicit
agent period filters can narrow readings; they do not delete current state.
Sensors and social events share one publication
version and the existing Delta/Autonomous/Object Storage commit sequence; the
pointer advances only after the complete snapshot is durable. The agent's
read-only `PRISMA_V_SENSOR_EVENTS` projection uses that same version. The viewer
reads the private Gold snapshot; neither browser nor agent queries Delta directly.
The Sensors panel supports filtering, searching and selection; its latitude and
longitude are read-only values from that publication.
The demo
validates at most 25,000 records per microbatch on the driver; larger ingestion
volumes require distributed validation rather than increasing this bound.

The Social and Sensors workflows use separate always-on USER Spark clusters,
`social_stream_compute` and `sensor_stream_compute`, each with a 2-OCPU/32-GB
driver and one fixed 2-OCPU/32-GB worker. The shared participant lab cluster and
the agent's AI Compute remain separate. The sensor workflow writes only its
Bronze/Silver tables and health document; the Social workflow remains the single Gold
publisher and reads the latest committed sensor Delta snapshot. Pausing VM source
capture does not stop these streams or their compute. The existing VM watcher
restarts failed workflow executions from their own checkpoints. Updates reject
changing the notebook or compute of an active workflow until it is stopped.
Bootstrap readiness requires both native executions to be RUNNING, heartbeats
from the current bundle and a matching Gold publication. This is distinct from
end-to-end acceptance with newly captured sensor files, and always-on compute
continues to incur charges while capture is paused.

This layer migration requires a coordinated rollout: stop/reconcile both
workflow notebooks to the same bundle, keep their existing checkpoint paths,
then restart them. The publisher waits for `sensor_layers_version=2` and a
matching `pipeline_revision` from the sensor heartbeat before publishing.
Local tests establish code behavior; these new tables must not be described as
live until that rollout and native data/version checks have completed.

The incident card leads with the event category. The operational view combines
all sources without an Origin filter or separate real/synthetic counters; each
evidence item retains provenance. Synthetic records are never relabelled as real.
The chat composer has a circular send control and a per-conversation submitted
question count, including failed or cancelled requests. Corroboration is a bounded heuristic based
on independent author/platform sources after near-copy suppression, not a
probability or automatic confirmation. Classification confidence remains separate.
Publication images retain their source restrictions and Synthetic provenance;
displaying an attachment does not imply human validation of the incident.
The map layers use lightweight capture status and the server's schedule instead
of fixed full-snapshot polling. Relevant content revisions distinguish social
updates from sensor updates. At a scheduled capture the viewer waits for a new
published revision, with bounded metadata retries while processing continues;
it does not treat an empty queue as proof of a particular batch's publication.
Initial/manual loading remains available. Polling never starts capture or
rewrites an article's timestamp; disabling a layer cancels its timer and request.

Each incident also publishes `correlation_context`, an explained, bounded set of
relationships over the existing evidence and sensor payloads. Report activity
continues to use each network's configured window and thresholds. Separate
historical candidates share category, locality and provenance within 24 hours
of the incident's last report, with compatible known country/city. They do not
merge incidents or change confidence, severity or human review. Account counts
are not counts of independent people; copied reports and supporting,
contradicting or unclassified claims remain distinguishable.

Sensor associations require precise incident coordinates, matching provenance
and locality, compatible jurisdiction, at most 2 km distance and a reading within
24 hours of the last report. Future readings are excluded. Missing compatible
readings mean only absence in the current publication, not absence of sensor
coverage or evidence against the social report. Context includes the criteria,
counts and up to five sample links; sensor-family deletion recomputes those
links without altering facts or analyst review. This is a logical graph over
existing records, not a new graph database or a calibrated predictive model.
Larger histories would require indexed temporal/spatial queries; a native SQL
property graph can be considered when multi-hop questions justify it.

The assistant queries the requested publication on every turn, separates
country/city/locality and maps critical incident severity to `high`. Its final
structured response is generated separately from tool calls, with short internal
citation tokens resolved back to queried source IDs. Empty or failed queries
cannot be presented as proof that no events exist in the real world. Synthetic
test provenance is preserved; the assistant does not send operational reports.

Without an explicit event or sensor selection, the model can select up to five
queried incident references for an event inventory. A selected event or sensor
keeps the explanatory format for source comparison and verification questions.
The application renders each selected row's category, locality, severity, review,
last report timestamp and provenance together with that row's citation. It labels
the subset size against the number of queried incidents, not a universal total.
Report timestamps are not verified occurrence times. This prevents combinations
of facts from different selected rows. When source evidence was queried, the
native model selects relevant citations and assessment/draft format; code renders
up to four literal source excerpts with their original authors, times, provenance
and duplicate relationships, plus cited readings. Free attributed paraphrases are
discarded because native acceptance exposed facts swapped between authors.
This deliberately limits narrative freedom in those reports; the model still
performs the native queries and source selection. Other explanations remain
model-generated and require separate semantic checks.

Evidence queries also retrieve the same publication's event relationships for
the returned posts, preserving copy origins and contradictory claims without
duplicating evidence rows. Different networks or accounts do not establish
independent witnesses; the formatter receives that limit and labels the
corroboration index as heuristic.

Architecture comparison for the query-grounding and shared capture increment (2026-10-05):
quality 6595 → 6580, coupling 0.08 unchanged, zero cycles and god files; complex
functions 44 → 58. This is an intentional gate exception, not a passing gate:
the agent retains current-turn query validation, same-version citations,
provider-compatible formatting, row-bound inventories and explicit failure handling for errors observed
in native AIDP runs. Finite capture also preserves exhausted cursors and retries
unfinished institutional/downstream processing without replaying social posts.
Shared scheduling adds UTC slot, concurrent-save and quota guards, durable
bounded-batch recovery, and per-layer publication coalescing. These guards share
the existing capture/publication boundaries; no new services or dependencies
were introduced. Source excerpts are rendered literally to prevent the observed
cross-author attribution failure. Per-post enrichment journals preserve valid
work across a later rejection. Bounded source-span selection avoids the observed
model quotation-copying failure, while one corrective response addresses repeated
risk/locality claims. Both retain the literal-quote and unique-risk validators.
Splitting these guards solely to lower the aggregate would obscure their state
and trust boundaries. The baseline was not reset.

Selecting a located event opens its consolidated summary and publication carousel
at the map point. Each distinct captured ID remains a carousel slide, even when
several posts share text. The heading shows `Publications 1/9`; floating arrows
appear only for multiple posts. Cards identify the network and username, while
source provenance remains in the payload and image metadata. The event list uses
numbered, neutral SCENES-style cards and highlights only the selection.
Events without coordinates use the sidebar. From/To share a row, initially track
the latest 24 hours, and interpret entered dates in Bogotá time without repeating
the timezone suffix on every post. Editing the range fixes it; Clear restores
the rolling window.

The viewer bridge applies this window before returning evidence and event-post
relations to the browser, including when the cloud publication has stopped
advancing. An event remains visible if it has a publication within the window,
regardless of its original creation date. Full evidence IDs remain on each
incident for review concurrency; carousel/list counts use the scoped evidence.
The consolidated summary and corroboration still describe the complete event.
The bridge still reads the existing bounded Gold artifact; scaling beyond that
artifact requires partitioned/indexed serving, rather than only a browser filter.

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

Postflight for the Synthetic reset increment (2026-10-03): quality 6683 →
6682, coupling 0.11 → 0.10, cycles 0, god files 0; complex functions 16 → 19.
This is an intentional, documented gate exception, not a passing gate. Cleanup
uses explicit preservation checks and durable recovery steps across independent
stores; the confirmation UI distinguishes rejected, accepted and interrupted
requests so a retry cannot start a second deletion. Meaningful phase extraction
reduced the initial count of 21 to 19. Further fragmentation solely to reduce the
aggregate would obscure these failure paths. The baseline is unchanged, and the
full checks passed (847 Python tests, 2 skips; 53 frontend tests and TypeScript
build). Native Oracle/Delta execution still requires deployment and live acceptance.

Postflight for neutral naming and administration (2026-10-05): the session
comparison is 6580 → 6582, coupling 0.0758 → 0.0712, zero cycles and god files.
Complex functions increase from 58 to 61. Against the retained historical
baseline, the CLI gate reports 6595 → 6582 and 44 → 61 complex functions.
This is an intentional gate exception, not a passing gate. The new administration
flows retain explicit async completion, authentication, bounded HTML handling
and cross-version compatibility checks. Reducing these branches by dropping
recovery or input checks would change required behavior; splitting them solely
to change the aggregate would obscure the contract. No baseline was reset.
Graphify's AST graph was updated; custom Sentrux rules are not configured.
Validation passed: 1329 backend tests (3 opt-in skips), 273 infrastructure Python
tests, 140 frontend tests, 114 viewer tests, 8 mock Terraform tests, Terraform
format/validate, production container builds and nginx configuration validation.
Browser DOM fixtures passed 7 groups without screenshots or real mutations;
11 authenticated/anonymous API and initial-HTML checks passed on the final local
containers. A separate real read verified the earlier cleanup completed with
204 rewritten publications, no error, no progress popup and all sources paused.
