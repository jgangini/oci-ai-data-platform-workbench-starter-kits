"""Refresh verified synthetic media; --optimize-media encodes original PNGs as WebP using Pillow."""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import struct


ROOT = Path(__file__).resolve().parent / "v1"
SCENES = {
    "flood-kennedy-active": "localized flooding on a residential street in Kennedy",
    "flood-kennedy-receding": "receding floodwater and an emerging curb in Kennedy",
    "flood-bosa-active": "localized flooding around residential entrances in Bosa",
    "flood-bosa-receding": "receding floodwater and muddy residential entrances in Bosa",
    "fire-chapinero-active": "small flames and smoke over burning vegetation in Chapinero",
    "fire-chapinero-smoldering": "smoldering blackened vegetation with light smoke and no visible flames in Chapinero",
    "landslide-ciudad-bolivar": "wet earth and stones from a small landslide beside a hillside path in Ciudad Bolívar",
    "rain-suba": "heavy rain and reduced visibility on a residential street in Suba",
}
LOCALITY_SCENES = {"Kennedy": "flood-kennedy", "Bosa": "flood-bosa", "Chapinero": "fire-chapinero",
                   "Ciudad Bolívar": "landslide-ciudad-bolivar", "Suba": "rain-suba"}
# These fixture IDs describe recession or residual smoke; their chronology and text are immutable.
RECEDING = {"Kennedy": {17, 19}, "Bosa": {40, 41, 43, 44}, "Chapinero": {58, 62, 64, 65, 67, 68}}


def scene(post):
    name = LOCALITY_SCENES[post["locality"]]
    number = int(post["fixture_id"].split("-")[1])
    if post["locality"] in RECEDING:
        name += ("-smoldering" if post["locality"] == "Chapinero" else "-receding") if number in RECEDING[post["locality"]] else "-active"
    return name


def checked_file(root, relative, digest):
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
        raise ValueError("Invalid synthetic fixture path or checksum")
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Synthetic fixture checksum mismatch; refresh did not modify the corpus")
    return data


def generated_assets(root):
    provenance = json.loads((root / "media/generation.json").read_text(encoding="utf-8"))
    if provenance.get("generator") != "OpenAI image_gen" or not provenance.get("generated_at"):
        raise ValueError("Generated media requires its image-generation provenance")
    assets = provenance.get("assets", [])
    indexed = {item["fixture_id"]: item for item in assets}
    if len(indexed) != len(assets) or len({item["sha256"] for item in assets}) != len(assets):
        raise ValueError("Each synthetic publication requires its own unique image")
    for asset in indexed.values():
        relative = asset["dataset_path"]
        if not re.fullmatch(r"media/[a-z0-9_-]+\.(?:png|webp)", relative):
            raise ValueError("Invalid generated media assignment")
        if any(not isinstance(asset.get(key), str) or not asset[key].strip() for key in ("prompt", "alt_text")):
            raise ValueError("Generated media requires its complete prompt and description")
        data = checked_file(root, relative, asset["sha256"])
        dimensions = media_dimensions(data)
        expected_mime = "image/webp" if relative.endswith(".webp") else "image/png"
        if (dimensions != (asset["width"], asset["height"]) or min(dimensions) < 512
                or asset["mime_type"] != expected_mime or relative.endswith(".png") != data.startswith(b"\x89PNG")):
            raise ValueError("Generated media must retain its full resolution and format")
    return indexed


def media_dimensions(data):
    """Read only the PNG/VP8 formats emitted by this corpus; runtime needs no image library."""
    if len(data) >= 24 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        return struct.unpack(">II", data[16:24])
    if (len(data) >= 30 and data[:4] == b"RIFF" and data[8:16] == b"WEBPVP8 "
            and data[23:26] == b"\x9d\x01\x2a" and struct.unpack("<I", data[4:8])[0] == len(data) - 8):
        return tuple(value & 0x3FFF for value in struct.unpack("<HH", data[26:30]))
    raise ValueError("Unsupported or truncated generated media format")


def refresh(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    posts, obsolete = [], set()
    for entry in manifest["posts"]:
        if not re.fullmatch(r"post-\d{4}", entry["fixture_id"]) or entry["path"] != f"posts/{entry['fixture_id']}/post.json":
            raise ValueError("Invalid synthetic fixture path")
        post = json.loads(checked_file(root, entry["path"], entry["sha256"]))
        if post["fixture_id"] != entry["fixture_id"]:
            raise ValueError("Synthetic fixture identity mismatch")
        if len(post["attachments"]) > 1:
            raise ValueError("Expected one image per selected synthetic fixture")
        for attachment in post["attachments"]:
            if attachment not in manifest["media"] or attachment["id"] != post["fixture_id"] + "-image-01":
                raise ValueError("Invalid synthetic fixture attachment")
            checked_file(root, attachment["dataset_path"], attachment["sha256"])
            if attachment["dataset_path"] == f"posts/{post['fixture_id']}/media/image-01.svg":
                obsolete.add(root / attachment["dataset_path"])
        posts.append(post)
    counts = Counter(post["platform"] for post in posts if post["attachments"])
    if len(posts) != 120 or counts != Counter({name: 18 for name in ("x", "facebook", "instagram", "tiktok")}):
        raise ValueError("Expected 120 posts and 18 media posts per network")
    assets = generated_assets(root)
    selected = [post for post in posts if post["attachments"]]
    if set(assets) != {post["fixture_id"] for post in selected} or any(assets[post["fixture_id"]]["scene"] != scene(post) for post in selected):
        raise ValueError("Generated images must match each publication and its scenario phase")
    media = []
    for entry, post in zip(manifest["posts"], posts, strict=True):
        if post["attachments"]:
            asset = assets[post["fixture_id"]]
            relative = asset["dataset_path"]
            item = {"id": post["fixture_id"] + "-image-01", "type": "image", "mime_type": asset["mime_type"], "dataset_path": relative,
                    "sha256": asset["sha256"], "license": "CC0-1.0", "origin": "ai_generated",
                    "alt_text": asset["alt_text"], "is_simulated": True}
            post["attachments"] = [item]
            media.append(item)
        data = (json.dumps(post, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (root / entry["path"]).write_bytes(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["media"] = media
    manifest["presentation_revision"] = max(manifest.get("presentation_revision", 0), 5)
    manifest["provenance"] = "Invented personal names, fictional posts and AI-generated photorealistic scenes for this demonstration; no real accounts or photographs of actual incidents. Name coincidences are accidental."
    manifest["media_provenance"] = "media/generation.json"
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for target in obsolete:
        target.unlink()


def optimize_media(root):
    root = Path(root).resolve()
    refresh(root)  # Validate every original post and attachment before changing an asset.
    provenance_path = root / "media/generation.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    originals = []
    for asset in provenance["assets"]:
        if asset["dataset_path"].endswith(".webp"):
            continue
        from PIL import Image, __version__ as pillow_version
        data = checked_file(root, asset["dataset_path"], asset["sha256"])
        output = io.BytesIO()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError("Optimization requires the original RGB PNG; no color or alpha conversion is allowed")
            image.save(output, format="WEBP", quality=90, method=6)
        encoded = output.getvalue()
        if media_dimensions(encoded) != (asset["width"], asset["height"]):
            raise ValueError("Optimization changed the image dimensions")
        original = root / asset["dataset_path"]
        target = original.with_suffix(".webp")
        target.write_bytes(encoded)
        asset["source_png"] = {"dataset_path": asset["dataset_path"], "sha256": asset["sha256"]}
        asset.update(dataset_path=target.relative_to(root).as_posix(), mime_type="image/webp",
                     sha256=hashlib.sha256(encoded).hexdigest(),
                     encoding={"format": "WebP", "quality": 90, "method": 6, "pillow_version": pillow_version,
                               "resized": False, "cropped": False})
        originals.append(original)
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    refresh(root)
    for original in originals:
        original.unlink()  # Remove only validated originals after the new manifest is complete.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--optimize-media", action="store_true", help="Preserve resolution while encoding RGB PNGs as WebP quality 90 (requires Pillow)")
    arguments = parser.parse_args()
    (optimize_media if arguments.optimize_media else refresh)(arguments.root)
