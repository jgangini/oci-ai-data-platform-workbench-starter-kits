# Bogotá synthetic social corpus v2

This version contains 16 fictional Spanish posts: four each for X, Facebook,
Instagram and TikTok. Four original AI-generated images appear in 12 attachments
(75% of posts). Each image has an original report and two explicitly attributed
republications. A fourth, text-only post adds a limited observation, clarification
or correction. Different captions on the same image are not independent visual
evidence.

The scenes cover shallow street flooding, cooking smoke initially mistaken for a
possible fire, a small roadside soil slip, and a passable wet street whose extent
and location are misreported in forwarded captions. Posts retain uncertainty,
emotion, limited viewpoints and publication times; one delayed report explicitly
describes an earlier observation. This is a 600-second cycle, not a simulation of
a 24-hour delay or proof of any real incident.

All names and accounts are invented. Coincidences are accidental. Capture records
retain `mode: Synthetic`, `is_simulated: true` and versioned provenance metadata;
attachments identify `origin: ai_generated`. Messages themselves read like human
reports without instructions or evaluation labels. Location coordinates are
representative locality anchors, not measured incident locations. The images are
attachments and support duplicate detection; the current classifier processes
text only and does not inspect their pixels.

## Immutable versions and activation

`bogota-v1` remains the default, with all its files unchanged. Selecting v2 requires
an explicit new capture run with `dataset_version: bogota-v2`; changing the version
on an existing run does not reinterpret its stored cursor or evidence IDs. The v1
minimum of 100 posts is that fixture's contract, not a general runtime requirement.
This separate 16-post version adds no copies or rewrites of v1 assets.

Captured v2 text is displayed unchanged. Image URLs include the dataset version,
so an identical fixture number in v1 and v2 cannot select the wrong photo. The
manifest pins every post and PNG with SHA-256. Reposts have distinct attachment
IDs, the same original image hash/path, and `reused_from` naming the original
attachment. Generated PNG files are stored byte-for-byte without re-encoding.

## Generation and evaluation

The four images were generated with OpenAI's built-in `image_gen` on 2026-10-05.
[`media/generation.json`](media/generation.json) records the exact generation
prompts, image dimensions, original hashes and synthetic provenance. The manifest
references that record. The prompts are generation documentation, never model
input during classification. Dataset license: CC0-1.0.

[`evaluation/ground-truth.json`](evaluation/ground-truth.json) describes the
fictional scenarios, copied reports and corrections for offline evaluation.
Evaluation files are excluded from the runtime Docker image and are not read by
capture or correlation. A report's synthetic origin is separate from whether its
claims are supported, contradicted or uncertain within the fictional scenario.
