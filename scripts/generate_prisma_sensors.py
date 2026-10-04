"""Write the reproducible TXT sensor corpus; the VM producer generates fresh batches at runtime."""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "backend"))
from app.prisma.sensors import generate_batch, text_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", default="2026-10-03T18:00:00+00:00", help="UTC timestamp for this reproducible example")
    args = parser.parse_args()
    stamp = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds():
        parser.error("--at must be a UTC timestamp")
    destination = ROOT / "datasets" / "synthetic" / "sensors" / "colombia" / "v1"
    rows = generate_batch(stamp.timestamp())
    files = text_files(rows)
    manifest = {"mode": "Synthetic", "is_simulated": True, "records": len(rows),
                "interval_minutes": 5, "observed_batch_at": args.at, "files": []}
    for relative, content in sorted(files.items()):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest["files"].append({"path": relative, "records": len(content.splitlines()),
                                  "sha256": hashlib.sha256(content).hexdigest()})
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} simulated readings in {len(files)} TXT files to {destination}")


if __name__ == "__main__":
    main()
