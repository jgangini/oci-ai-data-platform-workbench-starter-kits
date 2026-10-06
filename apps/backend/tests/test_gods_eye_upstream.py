import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


MODULE_PATH = Path(__file__).parents[2] / "gods-eye-view" / "prepare_upstream.py"
SPEC = importlib.util.spec_from_file_location("gods_eye_prepare_upstream", MODULE_PATH)
upstream = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upstream)
PREFIX = "gods-eye-view-test-commit"


def archive_with(tmp_path, entries):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            if isinstance(content, bytes):
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            else:
                member.type = content[0]
                member.linkname = "../../outside"
                archive.addfile(member)
    manifest = {"commit": "test-commit", "archive_sha256": upstream.digest(path), "excluded": [], "patches": []}
    return path, manifest


def test_checksum_failure_precedes_archive_read_or_output_creation(tmp_path, monkeypatch):
    archive, manifest = archive_with(tmp_path, [(f"{PREFIX}/source.js", b"unchanged")])
    manifest["archive_sha256"] = "0" * 64
    monkeypatch.setattr(upstream.tarfile, "open", lambda *args, **kwargs: pytest.fail("Invalid archive must not be opened"))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="checksum mismatch"):
        upstream.prepare(archive, output, manifest)
    assert not output.exists()


@pytest.mark.parametrize("name", [f"{PREFIX}/../outside", f"{PREFIX}/src/../../outside", "/absolute/path",
    "other-root/source.js", f"{PREFIX}/src/./source.js", f"{PREFIX}/..\\outside",
    f"{PREFIX}/C:\\outside", f"{PREFIX}/C:/outside", f"{PREFIX}/source.js:stream"])
def test_archive_paths_cannot_escape_or_use_windows_path_semantics(tmp_path, name):
    archive, manifest = archive_with(tmp_path, [(name, b"untrusted")])
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Invalid upstream archive path"):
        upstream.prepare(archive, output, manifest)
    assert not any(output.rglob("*"))
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_links_and_special_entries_are_rejected(tmp_path, kind):
    archive, manifest = archive_with(tmp_path, [(f"{PREFIX}/source.js", (kind,))])
    with pytest.raises(ValueError, match="Unsupported upstream archive entry"):
        upstream.prepare(archive, tmp_path / "output", manifest)
    assert not any((tmp_path / "output").rglob("*"))


@pytest.mark.parametrize("content,sha,anchor,expected", [
    (b"anchor", "0" * 64, "anchor", "integration contract changed"),
    (b"missing", None, "anchor", "patch anchor changed"),
    (b"anchor anchor", None, "anchor", "patch anchor changed"),
])
def test_patch_requires_exact_file_hash_and_unique_anchor(tmp_path, content, sha, anchor, expected):
    archive, manifest = archive_with(tmp_path, [(f"{PREFIX}/source.js", content)])
    manifest["patches"] = [{"path": "source.js", "sha256": sha or hashlib.sha256(content).hexdigest(),
        "replacements": [{"before": anchor, "after": "replacement"}]}]
    with pytest.raises(ValueError, match=expected):
        upstream.prepare(archive, tmp_path / "output", manifest)
    assert (tmp_path / "output" / "source.js").read_bytes() == content
    assert not (tmp_path / "output" / "GODS_EYE_VIEW_UPSTREAM.json").exists()


@pytest.mark.parametrize("content", [b"missing", b"BOUNDARY\nBOUNDARY"])
def test_scene_boundary_must_be_unique_before_trimming(tmp_path, content):
    archive, manifest = archive_with(tmp_path, [(f"{PREFIX}/source.js", content)])
    manifest["patches"] = [{"path": "source.js", "sha256": hashlib.sha256(content).hexdigest(),
        "keep_from": "BOUNDARY", "prepend": "safe prefix\n", "replacements": []}]
    with pytest.raises(ValueError, match="scene boundary changed"):
        upstream.prepare(archive, tmp_path / "output", manifest)
    assert (tmp_path / "output" / "source.js").read_bytes() == content


def test_exclusions_patch_and_clean_output_contract(tmp_path):
    source = b"restricted scene\nconst PUBLIC = [1];\n"
    archive, manifest = archive_with(tmp_path, [
        (PREFIX + "/", (tarfile.DIRTYPE,)), (f"{PREFIX}/src/main.js", source),
        (f"{PREFIX}/docs/media/image.png", b"excluded"), (f"{PREFIX}/src/restricted.js", b"excluded"),
        (f"{PREFIX}/docs/media-other/NOTICE", b"retained license"), (f"{PREFIX}/LICENSE", b"MIT"),
    ])
    manifest["excluded"] = ["docs/media", "src/restricted.js"]
    manifest["patches"] = [{"path": "src/main.js", "sha256": hashlib.sha256(source).hexdigest(),
        "keep_from": "const PUBLIC", "prepend": "// safe\n", "replacements": [{"before": "[1]", "after": "[2]"}]}]
    output = tmp_path / "output"
    upstream.prepare(archive, output, manifest)
    assert (output / "src/main.js").read_bytes() == b"// safe\nconst PUBLIC = [2];\n"
    assert not (output / "docs/media").exists() and not (output / "src/restricted.js").exists()
    assert (output / "docs/media-other/NOTICE").read_bytes() == b"retained license"
    assert (output / "LICENSE").read_bytes() == b"MIT"
    assert json.loads((output / "GODS_EYE_VIEW_UPSTREAM.json").read_text()) == {
        "commit": "test-commit", "archive_sha256": manifest["archive_sha256"],
        "patched": ["src/main.js"], "excluded": manifest["excluded"]}
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="never overwrite a checkout"):
        upstream.prepare(archive, output, manifest)
    assert before == {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
