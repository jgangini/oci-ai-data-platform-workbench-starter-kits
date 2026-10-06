# God's Eye View

`10_bronze/social_network.py` and `10_bronze/sensor_stream.py` are independent
Python workflows containing their complete processing logic and nonsecret
configuration. Open either file to inspect its Bronze, Silver and publication
stages. AIDP credentials hold secret values; they are never embedded here.

`20_silver/` and `30_gold/` document the stages performed by those workflows.
`40_report/ai_gods_eye_view.py` is the standalone Gold-backed agent.
The installer verifies uploads against `manifest.json`; execution does not load
that manifest or other project files. Stop any workflow using these paths before
replacing its source. Regenerate these artifacts from the source repository and redeploy.
Only the empty RUNTIME_CONFIG assignment is filled with deployment identifiers;
all processing logic is identical to the versioned artifact.

From the repository root, run `python scripts/render_gods_eye_view_runtime.py`
after editing shared processing source in `apps/backend/app/gods_eye_view`.
`python scripts/render_gods_eye_view_runtime.py --check` verifies exact generated
bytes and package hashes. Do not hand-edit a generated program; deployment loads
these canonical files and never reassembles their processing logic.
