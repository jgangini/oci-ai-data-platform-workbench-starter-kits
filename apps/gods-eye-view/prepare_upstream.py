"""Materialize a pinned upstream distribution with small, fail-closed integration patches."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import urllib.request


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def patch_file(output, patch):
    path = output / patch["path"]
    if digest(path) != patch["sha256"]:
        raise ValueError("Upstream integration contract changed: " + patch["path"])
    text = path.read_text(encoding="utf-8")
    if "keep_from" in patch:
        if text.count(patch["keep_from"]) != 1:
            raise ValueError("Upstream scene boundary changed")
        text = patch["prepend"] + patch["keep_from"] + text.split(patch["keep_from"], 1)[1]
    for change in patch["replacements"]:
        if text.count(change["before"]) != 1:
            raise ValueError("Upstream patch anchor changed: " + patch["path"])
        text = text.replace(change["before"], change["after"], 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def archive_parts(name, prefix):
    parts = PurePosixPath(name).parts
    # TAR paths use POSIX separators; reject Windows separators, drives and alternate data streams too.
    if (not parts or parts[0] != prefix or "\\" in name or ":" in name
            or any(part in {"..", "."} for part in name.split("/"))):
        raise ValueError("Invalid upstream archive path")
    return parts


def prepare(archive, output, manifest):
    if digest(archive) != manifest["archive_sha256"]:
        raise ValueError("Upstream archive checksum mismatch")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty generated upstream directory; never overwrite a checkout")
    output.mkdir(parents=True, exist_ok=True)
    prefix = "gods-eye-view-" + manifest["commit"]
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            parts = archive_parts(member.name, prefix)
            relative = "/".join(parts[1:])
            if any(relative == path or relative.startswith(path + "/") for path in manifest["excluded"]):
                continue
            if member.isdir():
                continue
            if not member.isfile() or member.size > 20_000_000:
                raise ValueError("Unsupported upstream archive entry")
            path = output.joinpath(*parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as content:
                path.write_bytes(content.read())
    for patch in manifest["patches"]:
        patch_file(output, patch)
    (output / "GODS_EYE_VIEW_UPSTREAM.json").write_text(json.dumps({
        "commit": manifest["commit"], "archive_sha256": manifest["archive_sha256"],
        "patched": [patch["path"] for patch in manifest["patches"]], "excluded": manifest["excluded"],
    }, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("upstream.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="gods-eye-source-") as temporary:
        archive = args.archive or Path(temporary) / "source.tar.gz"
        if args.archive is None:
            with urllib.request.urlopen(manifest["archive_url"], timeout=120) as response, archive.open("xb") as target:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    target.write(block)
        prepare(archive, args.output, manifest)
    print(json.dumps({"commit": manifest["commit"], "status": "prepared"}))


if __name__ == "__main__":
    main()
