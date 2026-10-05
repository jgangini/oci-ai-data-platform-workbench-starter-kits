"""Versioned synthetic posts for VM capture; evaluation files are never opened."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re

from .core import SYNTHETIC_MODES, PLATFORMS, utc_text

VERSION = "bogota-v1"
VERSIONS = {"bogota-v1": "v1", "bogota-v2": "v2"}
DIRECTORY = "datasets/synthetic/social-media/natural-hazards/colombia/bogota/v1"
MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def dataset_root(version=VERSION):
    if not isinstance(version, str) or version not in VERSIONS:
        raise ValueError("Unsupported synthetic dataset version")
    configured = os.getenv("GODS_EYE_DATASET_ROOT")
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[4] / DIRECTORY
    return root if version == VERSION else root.parent / VERSIONS[version]


def checked_file(root, relative, digest):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
        raise ValueError("Invalid synthetic dataset path or checksum")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Synthetic dataset checksum mismatch")
    return path, data


def _load_post(root, entry, media):
    identifier = entry["fixture_id"]
    if not re.fullmatch(r"post-\d{4}", identifier) or entry["path"] != f"posts/{identifier}/post.json":
        raise ValueError("Invalid synthetic fixture identifier")
    _, data = checked_file(root, entry["path"], entry["sha256"])
    post = json.loads(data)
    required = {"fixture_id", "platform", "author", "message", "country", "city", "locality", "location",
                "published_offset_seconds", "available_offset_seconds", "attachments"}
    if set(post) != required or post["fixture_id"] != identifier or post["platform"] not in PLATFORMS:
        raise ValueError("Invalid synthetic post contract")
    published, available = post["published_offset_seconds"], post["available_offset_seconds"]
    if any(type(value) is not int for value in (published, available)) or not 0 <= published <= available < 600:
        raise ValueError("Invalid synthetic post chronology")
    for attachment in post["attachments"]:
        extension = str(attachment.get("dataset_path", "")).rpartition(".")[2]
        shared = re.fullmatch(r"media/[a-z0-9_-]+\.(?:png|jpe?g|webp)", attachment.get("dataset_path", ""))
        bundled = re.fullmatch(rf"posts/{identifier}/media/image-\d{{2}}\.(?:svg|png|jpe?g|webp)", attachment.get("dataset_path", ""))
        if (attachment not in media or not (shared or bundled)
                or not re.fullmatch(rf"{identifier}-image-\d{{2}}", attachment.get("id", ""))
                or attachment.get("type") != "image" or attachment.get("is_simulated") is not True
                or attachment.get("mime_type") != MEDIA_TYPES.get(extension)
                or attachment.get("origin") != ("synthetic_diagram" if extension == "svg" else "ai_generated")):
            raise ValueError("Synthetic attachment is not in the manifest")
        checked_file(root, attachment["dataset_path"], attachment["sha256"])
    return post


@lru_cache(maxsize=2)
def load(root, version=VERSION):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(version, str) or version not in VERSIONS or manifest.get("version") != version or manifest.get("schema_version") != 1 or manifest.get("duration_seconds") != 600:
        raise ValueError("Unsupported synthetic dataset version")
    posts, identifiers = [], set()
    for entry in manifest["posts"]:
        post = _load_post(root, entry, manifest["media"])
        identifier = post["fixture_id"]
        if identifier in identifiers:
            raise ValueError("Invalid synthetic fixture identifier")
        identifiers.add(identifier)
        posts.append(post)
    # The original 100-post minimum is a v1 fixture contract, not a capture requirement.
    if type(manifest["post_count"]) is not int or len(posts) != manifest["post_count"] or len(posts) < (100 if version == VERSION else 1):
        raise ValueError("Synthetic manifest post count mismatch")
    originals = {}
    for item in manifest["media"]:
        original = originals.get(item["sha256"])
        if original:
            if version == VERSION or item.get("reused_from") != original["id"] or item["dataset_path"] != original["dataset_path"]:
                raise ValueError("Synthetic publications must identify reused images")
        elif item.get("reused_from"):
            raise ValueError("Synthetic image reuse must reference its original")
        else:
            originals[item["sha256"]] = item
    return tuple(posts)


def media_file(fixture_id, filename, version=VERSION):
    """Resolve the fixture allowlist, including old SVG/PNG URLs for captured records."""
    if not re.fullmatch(r"post-\d{4}", fixture_id) or not re.fullmatch(r"image-\d{2}\.(?:svg|png|jpe?g|webp)", filename):
        raise ValueError("Invalid synthetic media identifier")
    root = dataset_root(version)
    posts = load(root, version)
    matches = [item for post in posts if post["fixture_id"] == fixture_id for item in post["attachments"]
               if item["id"] == f"{fixture_id}-{filename.rpartition('.')[0]}"
               and (filename.endswith((".svg", ".png")) or filename.rpartition(".")[2] == item["dataset_path"].rpartition(".")[2])]
    if len(matches) != 1:
        raise FileNotFoundError("Synthetic media is not in the bundled allowlist")
    path, _ = checked_file(root, matches[0]["dataset_path"], matches[0]["sha256"])
    return path, dict(matches[0])


def presentation(payload):
    """Refresh a verified fictional fixture's display without changing the captured record."""
    meta = payload.get("raw_metadata") or {}
    if (payload.get("mode") not in SYNTHETIC_MODES or meta.get("synthetic") is not True
            or meta.get("dataset_version") != VERSION):
        return {}
    post = next((post for post in load(dataset_root()) if post["fixture_id"] == meta.get("fixture_id")), None)
    if post is None or post["platform"] != payload.get("platform") or post["author"]["id"] != meta.get("author_id"):
        return {}
    attachments = [dict(item) for item in post["attachments"]]
    current_ids = {item["id"] for item in attachments}
    attachments.extend(dict(item) for item in payload.get("attachments", []) if item.get("id") not in current_ids)
    return {"text": post["message"], "username": post["author"]["username"],
            "display_name": post["author"]["display_name"], "attachments": attachments}


def events(platform, run_id, cycle, anchor_at, start, end, seed=0, version=VERSION):
    """Jitter stays below each causal gap; late availability never rewrites publication time."""
    if not isinstance(version, str) or version not in VERSIONS or type(seed) is not int or not 0 <= seed < 2 ** 31:
        raise ValueError("Unsupported synthetic dataset version or seed")
    result = []
    for post in load(dataset_root(version), version):
        if post["platform"] != platform:
            continue
        identity = f"{version}:{seed}:{run_id}:{post['fixture_id']}"
        jitter = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % 4 if post["published_offset_seconds"] else 0
        published = post["published_offset_seconds"] + jitter
        available = post["available_offset_seconds"] + jitter
        if not start < available <= end:
            continue
        location = post["location"]
        result.append({"platform": platform, "source_id": f"{run_id}:{cycle}:{version}:{post['fixture_id']}",
            "text": post["message"], "username": post["author"]["username"], "display_name": post["author"]["display_name"],
            "country": post["country"], "city": post["city"], "locality": post["locality"], "lat": location["lat"], "lon": location["lon"],
            "location_method": "synthetic_locality_anchor" if post["locality"] else "unresolved",
            "location_precision": location["precision"], "location_provenance": location["provenance"],
            "created_at": utc_text(anchor_at + published), "source_uri": "", "mode": "Synthetic", "is_simulated": True,
            "attachments": [dict(item) for item in post["attachments"]],
            "raw_metadata": {"fixture_id": post["fixture_id"], "author_id": post["author"]["id"], "dataset_version": version,
                "dataset_seed": seed, "scenario_run_id": f"{run_id}:{cycle}", "capture_run_id": run_id,
                "offset_seconds": published, "available_offset_seconds": available, "synthetic": True, "producer": "vm_search"}})
    return sorted(result, key=lambda event: (event["raw_metadata"]["available_offset_seconds"], event["source_id"]))
