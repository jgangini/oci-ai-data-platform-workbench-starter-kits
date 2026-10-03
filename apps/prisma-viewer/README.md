# God’s Eye View native application with Territorial Control

The deployed viewer boots the original God’s Eye View application: its globe,
native controls, Data Layers catalog, scenes, provider integrations and voice UI.
`native/` adds a Territorial Control catalog layer, evidence panel, two text
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

The six hash-checked source patches are:

| Upstream file | Integration change |
| --- | --- |
| `src/main.js` | Delegates to `native/main.js`. |
| `src/standalone/application.js` | Adds `extendCatalog(catalog, {scene, signal})` before registrations are sealed. |
| `src/app/constructCatalog.js` | Removes the restricted Bhote Koshi event/locator registrations. |
| `src/scenes/recipes.js` | Removes Nepal event recipes and their restricted data imports. |
| `src/scenes/packs/defaults.js` | Removes the Nepal scene presentation adapter. |
| `src/tools/index.js` | Removes the Bhote Koshi data query from the native tool registry. |

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
python apps/prisma-viewer/prepare_upstream.py --output apps/prisma-viewer/.upstream
npm --prefix apps/prisma-viewer/.upstream ci --ignore-scripts
node apps/prisma-viewer/native/runtime.mjs --build
node --test --test-isolation=none apps/prisma-viewer/tests/*.test.mjs
docker build -f apps/prisma-viewer/Dockerfile -t prisma-viewer:dev .
```

Preparation requires a missing or empty output directory. `--archive` accepts a
previously downloaded archive but performs the same checksum validation. The
root viewer's old `npm run build` builds the retained standalone page; use the
native command or Docker for the deployed native application.

For an upstream update, review the desired immutable commit and its licenses,
calculate the archive and six original file hashes, and review each patch against
that source before updating `upstream.json`. Materialize into a new empty
directory; never edit generated files to make a failing patch pass. Run the
preparer tests, viewer tests, native build, Docker health checks and authenticated
DOM/provider checks. Review new provider routes against the fixed proxy allowlist.
Only then publish the kit's immutable release and update the two VMs through the
existing release process. An upstream hash or anchor mismatch must fail closed.

## Runtime boundary and providers

VM2 supervises the original Node provider/preview runtime on
`127.0.0.1:4173` and the Python authentication/PRISMA bridge on port 8081.
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

## Three separate assistant paths

| Interface | Contract and scope |
| --- | --- |
| AIDP Analyst | `/api/prisma/chat`: versioned snapshot, current filters/selection, evidence references and explicit map-action buttons. Final JSON, not OpenAI Realtime or SSE. |
| OCI Assistant | `/api/prisma/oci-chat`: general text inference using the saved server-side OCI model. Separate bounded conversation history; answers are not verified incident evidence. |
| Original native voice | Upstream OpenAI Realtime integration and native tools. It retains its own provider configuration and session; OCI text does not implement or replace this protocol. |

Text conversations have separate histories and counters. Cancel aborts a pending
request and discards a late response. New conversation resets only the selected
assistant. An AIDP 409 preserves the question and refreshes the publication for
manual retry. Evidence and map-action buttons refuse to apply an answer from an
older publication. Model output is rendered as text, never executed as HTML/code.

The Territorial Control layer uses one native `CustomDataSource` and the native
layer manager's ten-second refresh lifecycle. Disabling it stops its refresh;
other layers remain intact. A fresh URL seeds a native Bogotá camera share state
before startup; an existing hash is preserved. No delayed or recurring local
camera override competes with native navigation or user-shared views.
Inclusive periods use incident creation time in
Bogotá (UTC−05:00); map-area filters carry the same bounds to AIDP. Events without
coordinates remain in the list unless a geographic area excludes them.
Activity, severity, confidence, corroboration and human confirmation are distinct.
Evidence preserves real/synthetic provenance. Only allowed real X photos whose
evidence IDs were included in the current human review can be displayed.

## Acceptance limits

Local fixture answers are explicitly identified and do not invoke an AIDP model.
Positive native CSV-processing evidence from the v2.3.8 processing baseline does
not establish live conversational acceptance. The separately tested deployed
AIDP agent returned HTTP 429 even with an explicit 2,048-token output bound;
its provider/configuration comparison matched expected values, but the cause
remains unconfirmed. A successful direct OCI inference does not resolve or prove
the AIDP agent path. Keep those acceptance items separate and repeat them after
the applicable provider/runtime change. Unit tests and container health do not
certify live voice, model inference or third-party feed availability.
