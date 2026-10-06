# Synthetic source data

- `social_networks/v1` contains 120 fictional posts and 72 images; `v2` contains 16 posts using four images. Keep their existing immutable IDs, manifests, image provenance and offline evaluation files.
- `sensors/colombia/v1` contains 4,000 readings in five TXT files. Each file is newline-delimited JSON, not a CSV.
- `sensors/generate.py` recreates the fixed example from the shared sensor generator; the VM producer creates current batches independently.
- `social_networks/refresh.py` validates the v1 image provenance and refreshes its post/media hashes. It does not generate new posts or images.

Run offline evaluation with `scripts/evaluate_gods_eye_view_corpus.py`. Evaluation labels are excluded from the runtime image and never sent to classification. The synthetic inputs are not evidence of actual incidents.
