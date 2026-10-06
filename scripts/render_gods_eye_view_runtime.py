"""Render the versioned native God's Eye View programs, or fail on generated-source drift."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "terraform/hooks"))
from gods_eye_view_bootstrap import runtime_archive
from gods_eye_sources import NOTEBOOK_ROOT, WORKFLOW_FILES, render_workflow_source
from gods_eye_agent_source import render_agent_source


def render(check=False):
    lab = NOTEBOOK_ROOT.parent
    bundle = runtime_archive()
    sources = {path: render_workflow_source(module, bundle) for module, path in WORKFLOW_FILES.items()}
    sources["40_report/ai_gods_eye_view.py"] = render_agent_source(bundle)
    metadata = json.loads((lab / "lab.json").read_text(encoding="utf-8"))
    metadata["runtime_files"] = []
    drift = []
    for relative, source in sorted(sources.items()):
        content = source.encode("utf-8")
        path = NOTEBOOK_ROOT / relative
        if check:
            if not path.is_file() or path.read_bytes() != content:
                drift.append(path.relative_to(ROOT).as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        metadata["runtime_files"].append({"file": "notebooks/" + relative,
            "sha256": hashlib.sha256(content).hexdigest(), "workspace_path": relative})
    metadata["source_manifests"] = []
    for relative in ("source/social_networks/v1/manifest.json", "source/social_networks/v2/manifest.json", "source/sensors/colombia/v1/manifest.json"):
        path = lab / relative
        current = path.read_bytes()
        # Git stores these UTF-8 manifests with LF on every platform; hash those exact bytes.
        content = current.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        if current != content:
            if check:
                drift.append(path.relative_to(ROOT).as_posix())
            else:
                path.write_bytes(content)
        metadata["source_manifests"].append({"file": relative, "sha256": hashlib.sha256(content).hexdigest()})
    unsigned = {key: value for key, value in metadata.items() if key != "pack_sha256"}
    metadata["pack_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    content = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if check:
        if (lab / "lab.json").read_bytes() != content:
            drift.append((lab / "lab.json").relative_to(ROOT).as_posix())
    else:
        (lab / "lab.json").write_bytes(content)
    if drift:
        raise SystemExit("Regenerate God's Eye View runtime artifacts: " + ", ".join(drift))
    print("God's Eye View runtime artifacts " + ("verified" if check else "rendered") + ": 3 programs, 3 source manifests.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check committed bytes and hashes without changing files")
    render(parser.parse_args().check)
