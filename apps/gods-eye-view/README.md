# God’s Eye View application

The deployed viewer boots the original God’s Eye View application: its globe,
native controls, Data Layers catalog, scenes, provider integrations and voice UI.
`native/` adds social network and sensor catalog layers, an evidence panel, two text
assistants and OCI settings. It does not replace the native application shell.
The previous standalone page in `src/main.js` is retained for compatibility and
existing checks; it is not the native image's entry point.

## Reproducible source and attribution

Upstream: [God’s Eye View by Bilawal Sidhu](https://github.com/bilawalsidhu/gods-eye-view),
commit `aa16b7c3b0166a89d8c7a6089e0aff53a22faaee` (package version 0.2.1).
The source archive SHA-256 is
`a6020d0aa12a952585edee44a4765ab11f511e3f616927645d7ce04ce48487cb`.
[`upstream.json`](upstream.json) is the authoritative pin, exclusion list and
patch manifest. The archive and every patched source file must match their hashes;
every replacement anchor must occur exactly once. A mismatch stops preparation.

[`prepare_upstream.py`](prepare_upstream.py) generates the ignored `.upstream/`
directory from the archive. That generated tree is never vendored or committed,
and the preparer refuses to overwrite a nonempty directory. Docker independently
reproduces the tree; it does not depend on a developer's checkout.

The nine hash-checked source patches are:

| Upstream file | Integration change |
| --- | --- |
| `src/data/localGeojsonCore.js` | Exposes native terrain/stem helpers reused by sensor layers. |
| `src/main.js` | Delegates to `native/main.js`. |
| `src/standalone/application.js` | Adds `extendCatalog(catalog, {scene, signal})` before registrations are sealed. |
| `src/app/constructCatalog.js` | Removes the restricted Bhote Koshi event/locator registrations. |
| `src/scenes/recipes.js` | Removes Nepal event recipes and their restricted data imports. |
| `src/scenes/packs/defaults.js` | Removes the Nepal scene presentation adapter. |
| `src/tools/index.js` | Removes the Bhote Koshi data query from the native tool registry. |
| `src/ui/rightPanelRail.js` | Supports the expanded Agent Flow panel and shared voice-control boundary. |
| `src/ui/leftPanelRail.js` | Keeps a minimum gap and respects the shared voice/HUD floor. |

The upstream [MIT source license](https://github.com/bilawalsidhu/gods-eye-view/blob/aa16b7c3b0166a89d8c7a6089e0aff53a22faaee/LICENSE)
and original notices remain in the generated tree and image. MIT covers source,
not every bundled dataset or model. This distribution excludes TeleGeography's
CC BY-NC-SA 3.0 cable dataset and the CC BY-NC 4.0 Bhote Koshi imagery and derived
coordinates, including the dataset embedded in JavaScript. It also excludes
upstream agent configuration, GitHub workflows and documentation media.

Other native layers and their controls remain present. Excluding the cable
dataset does not provide replacement cable data; that layer is unavailable
without an appropriately licensed source. Provider coverage, credentials,
quotas and availability still determine which live layers work in Bogotá.
Do not present another city's cameras as Bogotá feeds.

Retained source/provider attribution is documented in upstream
[`DATA_SOURCES.md`](https://github.com/bilawalsidhu/gods-eye-view/blob/aa16b7c3b0166a89d8c7a6089e0aff53a22faaee/DATA_SOURCES.md).
OpenStreetMap-derived datasets retain their ODbL obligations. Retained 3D models
keep their authors, source links, CC BY 4.0 licenses and modification notices in
[`public/models/README.md`](https://github.com/bilawalsidhu/gods-eye-view/blob/aa16b7c3b0166a89d8c7a6089e0aff53a22faaee/public/models/README.md).
Provider terms apply independently of the source license. Keep those notices
when redistributing the image or public assets.

The small, previously pinned MIT session module in `vendor/gods-eye-view/` is
still used by the tested AIDP JSON adapter. Its provenance/allowlist is separate
from the full native source pin; the viewer tests verify it.

## Build and upstream updates

The native image uses Node 24.14.0. It does not change the administration build's
Node version. From the repository root:

```sh
python apps/gods-eye-view/prepare_upstream.py --output apps/gods-eye-view/.upstream
npm --prefix apps/gods-eye-view/.upstream ci --ignore-scripts
node apps/gods-eye-view/native/runtime.mjs --build
node --test apps/gods-eye-view/tests/*.test.mjs
docker build -f apps/gods-eye-view/Dockerfile -t gods-eye-view:dev .
```

Preparation requires a missing or empty output directory. `--archive` accepts a
previously downloaded archive but performs the same checksum validation. The
root viewer's old `npm run build` builds the retained standalone page; use the
native command or Docker for the deployed native application.

For an upstream update, review the desired immutable commit and its licenses,
calculate the archive and all original patched-file hashes, and review each patch against
that source before updating `upstream.json`. Materialize into a new empty
directory; never edit generated files to make a failing patch pass. Run the
preparer tests, viewer tests, native build, Docker health checks and authenticated
DOM/provider checks. Review new provider routes against the fixed proxy allowlist.
Only then publish the kit's immutable release and update the two VMs through the
existing release process. An upstream hash or anchor mismatch must fail closed.

## Runtime boundary and providers

Terraform deploys VM2 separately from the public application VM, in the same VCN.
The release build verifies the upstream archive and patches, builds/tests the
native application, and publishes an immutable image with its manifest and digest.
VM2 bootstrap verifies and installs that image; it does not download a moving
upstream branch or rebuild the viewer. Updates retain the same release checks.

VM2 supervises the original Node provider/preview runtime on
`127.0.0.1:4173` and the Python authentication bridge on port 8081.
The Node listener is private. VM1 serves `/gods-eye-view/` and authenticates the
static and API routes; it forwards only the approved native API prefixes.
The bridge uses a fixed local destination, validates the public origin, and
does not forward session cookies or OCI credentials to native providers.
It is not an arbitrary URL proxy. Never expose VM2 directly to the internet.

The native key writer and MCP transport are not enabled in this deployment.
Native `/api/setup/status` supplies provider availability; provider keys are
managed in the server environment. Missing keys remain visibly unconfigured.
OCI settings use authenticated administration endpoints: region and credentials
are read-only, and model choices come from the actual available OCI catalog.
No browser form accepts an OCI private key, database password or endpoint
override. Before starting the globe, authenticated `/api/setup/browser` returns
exactly `googleApiKey` and `cesiumToken` from the running server configuration.
Empty strings explicitly select keyless operation. A failed or malformed response
stops startup with an error; it does not silently assume credentials exist.
These two upstream-designated browser credentials are intentionally client-visible
and must use provider domain/API restrictions and minimum scopes. They are loaded
at runtime, so changing them does not require an image rebuild. No broader
environment projection is allowed. OCI, social-network and other private server
credentials remain exclusively server-side.

The globe can start with keyless sources. Optional Google photorealistic tiles,
Cesium services, native voice and other feeds require their respective provider
keys and access. Retaining a native control is not proof that its provider is
configured or geographically available.

## Assistant paths and voice providers

| Interface | Contract and scope |
| --- | --- |
| Agent Flow | `/api/gods-eye-view/chat`: one AIDP conversation with a versioned snapshot, current filters/selection, evidence references and explicit map-action buttons. Final JSON, not OpenAI Realtime or SSE. |
| OpenAI voice | Original upstream OpenAI Realtime integration and native tools, using the server OpenAI key. |
| OCI voice | An adapter for the native voice control. A bounded WAV turn goes to an audio-capable OCI conversational model; `xai.grok-tts` generates the spoken response. This is turn-based audio, not OpenAI WebRTC or full-duplex streaming. |

**Power up the globe** shows read-only provider status. Deployment supplies the
OCI models and credentials; a fresh browser automatically uses configured OCI
voice. Existing explicit browser preferences are preserved. Only one microphone
controller is installed. The OCI card uses the native provider indicator:
green means server credentials and a model are configured. Its last
inference result remains separate; a quota error does not erase configuration.
Friendly catalog names are displayed instead of model OCIDs.

With `GODS_EYE_VIEW_ENABLED=true`, a new deployment defaults to `xai.grok-4.6`
for text and `google.gemini-2.5-flash-lite` with voice `ara` for audio turns.
The administrator can override these through `GODS_EYE_OCI_TEXT_MODEL`,
`GODS_EYE_OCI_VOICE_MODEL` and `GODS_EYE_OCI_VOICE` in the VM environment.
Persisted selections take precedence. These defaults reuse the existing operator,
region and compartment and do not run inference or claim availability at startup.

When the OCI catalog returns `xai.grok-voice-agent`, the administrative API reports
it as **Realtime connection not verified**. The catalog advertises audio and
realtime capabilities, but this adapter does not yet implement a verified OCI
realtime session for that model. It remains disabled rather than being routed
through Gemini or TTS under a Grok Voice Agent label. Catalog availability,
saved configuration and successful voice inference are separate checks.

OCI voice uses the existing server operator and compartment. Neither PEM material
nor signing credentials reach the browser. The audio endpoints accept bounded PCM
WAV recordings up to 30 seconds; general API body limits remain unchanged. A voice
turn may propose only supported camera actions and data-layer visibility changes,
including Social networks. The client validates and executes these requests; the
voice reply does not confirm execution. It cannot execute code, choose a backend
URL, or confirm an incident. Published evidence remains in Agent Flow.
Cancelling stops microphone tracks and playback and discards late responses. Audio
is processed for the turn without persisting microphone recordings in the app.

The native ON/OFF control starts automatic audio turns: speech followed by a
900 ms pause sends the recording; playback finishes before listening resumes.
Silence-only 30-second windows recycle locally without inference. The amplitude
detector requires 250 ms above its threshold and can mistake sustained noise for
speech; validate microphone levels in the demonstration room. This is not
full-duplex realtime voice. OFF or any provider error stops the conversation.
Microphone acceptance must check permission, a spoken utterance, the resulting
response, and stopped input tracks. Test-file upload controls are not in the UI.
Keep that result separate from unit tests or a successful text-to-speech call.
The model contracts are documented by Oracle for
[Gemini audio input](https://docs.oracle.com/en-us/iaas/Content/generative-ai/google-gemini-2-5-flash.htm)
and [xAI speech output](https://docs.oracle.com/en-us/iaas/Content/generative-ai/xai-grok-tts.htm).

Agent Flow is a single AIDP conversation in the native right rail below Context.
Enter sends a question; Shift+Enter inserts a newline. The header's New conversation
button resets its history and counter. Cancel aborts a pending request and discards
a late response. An AIDP 409 preserves the question and refreshes the publication for
manual retry. Evidence and map-action buttons refuse to apply an answer from an
older publication. Model output is rendered as text, never executed as HTML/code.
The expanded panel respects Data Layers' height ceiling; the conversation scrolls
inside it while the composer remains visible.

`GODS_EYE_VIEW_MODE=oci` selects the real runtime independently of local development
identity. Configure it on both the backend and viewer, with the existing operator
profile and the configured module runtime on the backend. New module controls use
Object Storage, and analytical queries use AIDP Gold; Autonomous is not a
prerequisite for these paths. Existing deployments require the separate
[migration gates](../../docs/operations.md#migrate-gods-eye-view-controls).
An explicit
`GODS_EYE_VIEW_ADMIN_URL` makes the viewer obtain the authenticated backend's publication;
OCI mode rejects a fixture publication. Without that URL, the deployed viewer
continues reading Gold with its instance principal. Backend-proxy `/ready` checks
the native viewer only and reports `publication_check=authenticated_backend_request`;
module activation checks the backend deployment and snapshot separately.
Local identity with OCI mode does not start a second capture producer: the
deployed VM owns capture. Missing credentials or AIDP failures never select a
fixture answer. Omitting the override retains the existing development defaults.

The God’s Eye View layer uses one native `CustomDataSource`. Capture-status polling
follows the server schedule and refreshes completed publication revisions.
Disabling the layer stops its refresh; other layers remain intact. A fresh URL seeds a native Bogotá camera share state
before startup; an existing hash is preserved. No delayed or recurring local
camera override competes with native navigation or user-shared views.
Inclusive social periods use the linked publication's creation time;
sensor periods are explicit filters on `observed_at`. Last-known sensor readings
do not inherit the social feed's default 24-hour window. Map-area filters carry
the same bounds to AIDP. Events without
coordinates remain in the list unless a geographic area excludes them.
Activity, severity, confidence, corroboration and human confirmation are distinct.
Evidence preserves real/synthetic provenance. Only allowed real X photos whose
evidence IDs were included in the current human review can be displayed.

## Acceptance limits

Local fixture answers are explicitly identified and do not invoke an AIDP model.
Validate the installed revision's native workflows, Gold/Object publication,
agent conversation and evidence agreement independently. A successful direct OCI
inference does not establish the AIDP agent path, and a candidate conversation
does not switch the active pointer. Unit tests and container health do not
certify live voice, model inference or third-party feed availability. Keep run
identifiers and temporary diagnostics private; use the
[module acceptance guide](../../docs/modules/gods-eye-view.md#security-and-acceptance)
for the stable deployment checks.
