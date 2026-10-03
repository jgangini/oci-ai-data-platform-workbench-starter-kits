# Territorial Control Bogotá / God’s Eye View

This viewer reuses the original God’s Eye View application viewer, device atmosphere
compatibility, keyless terrain/retry and protocol-independent session modules. The
unmodified files are pinned in `vendor/gods-eye-view/PROVENANCE.json`, including their
SHA-256 checksums and the upstream MIT license. `npm test` checks this allowlist.
The PRISMA panels and JSON adapter are the local integration, not an upstream feature.

No upstream data, public scenes, models, TeleGeography or Bhote Koshi assets are copied.
No Google Maps, Cesium ion or OpenAI credentials are required. Cesium is Apache-2.0;
interactive OpenStreetMap tiles retain their attribution and provider usage policy.
Optional Re:Earth / Mapterhorn terrain is CC BY 4.0 and falls back to an ellipsoid.
The map requires browser WebGL; the incident list, evidence and chat remain available
if the map cannot initialize. Public tile services are an external availability dependency.
Use a licensed production tile service or self-hosted tiles before scaling traffic.

## Build

Node 24.14+ in the 24.x line (or 26.x); this does not change the kit's Node 22 build.

```sh
npm ci --ignore-scripts
npm test
npm run build
docker build -f apps/prisma-viewer/Dockerfile -t prisma-viewer:dev .
```

Run npm commands from this directory, and Docker from the repository root.
The image serves `dist` as `/app/static` using `server.py` on port 8081. The authenticated
VM1 proxy strips `/gods-eye-view/` for static files and forwards `/api/prisma/*` unchanged.
Public administration lives at `/admin/gods-eye-view`; participant access uses
`/local/gods-eye-view/login` and `/local/gods-eye-view/workspace`. Internal PRISMA data
and API names remain unchanged.
Never expose VM2 directly to the internet. Browser requests contain no upstream tokens.

The viewer polls immutable snapshots every ten seconds. Operational filters and counts
cover all ingestion modes; event details and cited evidence retain their real or simulated
provenance. A chat query carries the snapshot version, selection and filters;
responses from a different version fail visibly. A 409 refreshes the snapshot and retains
the question for manual retry. Cancel aborts the request and ignores late responses.
The question counter tracks submitted user turns, including failed or cancelled requests;
it does not count typed characters or agent replies. A new page session starts at zero.
Only explicit buttons can apply validated focus/filter suggestions; no model output is
executed as code or HTML. AIDP answers are final JSON, not an OpenAI Realtime or SSE stream.

Period controls use Bogotá time (UTC−05:00), convert to ISO UTC `date_from`/`date_to`,
and filter incident `created_at` inclusively within the published snapshot. The bridge
applies the same window and rejects citations or focus actions outside the explicit
filters and selection. Follow-up requests retain the public session and carry the current
snapshot version on every turn; this is not a claim of cloud memory acceptance.

The build exports GodEye's exact MIT license, Cesium's third-party inventory and the
installed runtime packages' license/notice texts in `THIRD-PARTY-NOTICES.txt`. Runtime
npm licenses are Apache-2.0, MIT, ISC, BSD/0BSD and Zlib; DOMPurify offers Apache-2.0 as
an alternative to MPL-2.0. Python runtime versions reuse the backend pins.

## Context layers

`/api/prisma/context` uses the authenticated viewer boundary. Open-Meteo model estimates
cover six fixed Bogotá locality anchors, with an eight-second timeout and a shared
15-minute cache. Failed refreshes are labelled stale; an initial failure returns no
weather points. These values are contextual model output, never sensor observations or
incident evidence. Current-value semantics: https://open-meteo.com/en/docs.

The free endpoint is for evaluation/prototyping. Commercial operation requires a
licensed customer endpoint: https://open-meteo.com/en/pricing and
https://open-meteo.com/en/terms. The UI retains Open-Meteo/CC BY 4.0 attribution.
The camera layer links the official Bogotá camera-location page only; no publicly
authorized live video feed has been verified or embedded.
