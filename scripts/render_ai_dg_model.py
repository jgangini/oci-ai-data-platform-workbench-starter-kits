"""Render the reviewed additive Delta bootstrap for AIDP; never execute it."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))

from app.ai_data_governance.delta import bootstrap_source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage-root", required=True,
                        help="Verified oci://<selected-artifacts-bucket>@<namespace>/oci_artifacts")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = bootstrap_source(args.storage_root)
    compile(content, "create_model.py", "exec")
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != content:
            raise SystemExit("Governance bootstrap differs from the current approved source")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(content)
    print(f"AI_DG bootstrap SHA-256: {hashlib.sha256(content).hexdigest()}")


if __name__ == "__main__":
    main()
