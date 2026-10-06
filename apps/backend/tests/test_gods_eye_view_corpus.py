"""Corpus integrity, evaluation isolation and durable continuous-capture compatibility."""
import json
import hashlib
from pathlib import Path
import re
import shutil

import pytest

from app.gods_eye_view import capture, corpus, landing
from app.gods_eye_view.core import CATEGORIES, PLATFORMS, corroboration, default_source
from app.gods_eye_view.posts import attachments_for, post_view

NOW = 1791209100.0


def runtime_copy(tmp_path):
    original = corpus.dataset_root()
    shutil.copy(original / "manifest.json", tmp_path / "manifest.json")
    shutil.copytree(original / "posts", tmp_path / "posts")
    if (original / "media").exists():
        shutil.copytree(original / "media", tmp_path / "media")
    return tmp_path


def test_corpus_runtime_excludes_evaluation_and_preserves_original_author_location(tmp_path, monkeypatch):
    truth = json.loads((corpus.dataset_root() / "evaluation/ground-truth.json").read_text(encoding="utf-8"))["posts"]
    assert sum(item["scenario_assertion"] == "false" for item in truth) >= 6
    assert {item["evidence_role"] for item in truth} >= {"supports", "contradicts", "duplicate"}
    assert any(item["is_late"] for item in truth)
    root = runtime_copy(tmp_path)
    monkeypatch.setenv("GODS_EYE_DATASET_ROOT", str(root))
    posts = corpus.load(root)
    assert len(posts) == 120 and len({post["fixture_id"] for post in posts}) == 120
    assert all(sum(post["platform"] == platform for post in posts) == 30 for platform in PLATFORMS)
    assert not (root / "evaluation").exists()
    events = [event for platform in PLATFORMS for event in corpus.events(platform, "run", 0, NOW, -1, 600)]
    forbidden = {"scenario_assertion", "expected_category", "evidence_role", "duplicate_of", "contradicted_by", "event"}
    for event in events:
        assert event["is_simulated"] and event["mode"] == "Synthetic" and event["source_uri"] == ""
        assert re.fullmatch(r"[a-z0-9_]{1,15}", event["username"])
        assert "sim_bogota" not in event["username"] and "Fictional" not in event["display_name"]
        assert event["country"] == "Colombia" and event["city"] == "Bogotá"
        assert event["location_precision"] in {"locality_anchor", "unresolved"}
        assert event["location_method"] == ("synthetic_locality_anchor" if event["locality"] else "unresolved")
        assert not forbidden.intersection(event) and not forbidden.intersection(event["raw_metadata"])
    _, encoded = landing.page(events)
    assert "scenario_assertion" not in encoded.decode() and "expected_category" not in encoded.decode()
    assert len(landing.records(encoded)) == 120


def test_bundled_image_is_allowlisted_and_hash_checked_even_after_cached_load(tmp_path, monkeypatch):
    root = runtime_copy(tmp_path)
    monkeypatch.setenv("GODS_EYE_DATASET_ROOT", str(root))
    path, metadata = corpus.media_file("post-0001", "image-01.svg")
    assert path.is_relative_to(root) and metadata["origin"] == "ai_generated"
    assert metadata["mime_type"] == "image/webp" and metadata["type"] == "image"
    assert path.read_bytes()[:4] == b"RIFF" and path.read_bytes()[8:12] == b"WEBP"
    assert corpus.media_file("post-0001", "image-01.png") == (path, metadata)
    assert corpus.media_file("post-0001", "image-01.webp") == (path, metadata)
    with pytest.raises(ValueError):
        corpus.media_file("../post-0001", "image-01.svg")
    with pytest.raises(FileNotFoundError):
        corpus.media_file("post-0007", "image-01.svg")
    with pytest.raises(FileNotFoundError):
        corpus.media_file("post-0001", "image-01.jpg")
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        corpus.media_file("post-0001", "image-01.svg")


def test_tampered_post_is_rejected_before_generation(tmp_path):
    root = runtime_copy(tmp_path)
    path = root / "posts/post-0001/post.json"
    path.write_text(path.read_text(encoding="utf-8").replace("Kennedy", "Other place"), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        corpus.load(root)


@pytest.mark.parametrize("extension,mime", [("png", "image/png"), ("jpg", "image/jpeg"),
                                           ("jpeg", "image/jpeg"), ("webp", "image/webp")])
def test_shared_raster_manifest_and_fixture_url_allowlists(tmp_path, monkeypatch, extension, mime):
    # Resolver tests check bytes and identity; image decoding belongs to the browser.
    asset = tmp_path / "media" / f"scene.{extension}"
    asset.parent.mkdir()
    asset.write_bytes(b"fixture-raster-bytes")
    post = json.loads((corpus.dataset_root() / "posts/post-0001/post.json").read_text(encoding="utf-8"))
    attachment = {**post["attachments"][0], "dataset_path": f"media/scene.{extension}",
                  "mime_type": mime, "origin": "ai_generated", "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}
    post["attachments"] = [attachment]
    record = tmp_path / "posts/post-0001/post.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(post), encoding="utf-8")
    entry = {"fixture_id": "post-0001", "path": "posts/post-0001/post.json",
             "sha256": hashlib.sha256(record.read_bytes()).hexdigest()}
    assert corpus._load_post(tmp_path, entry, [attachment]) == post
    monkeypatch.setenv("GODS_EYE_DATASET_ROOT", str(tmp_path))
    monkeypatch.setattr(corpus, "load", lambda _root, version=corpus.VERSION: [post])
    assert corpus.media_file("post-0001", "image-01.svg") == (asset, attachment)
    assert corpus.media_file("post-0001", f"image-01.{extension}") == (asset, attachment)
    view = attachments_for({"mode": "Synthetic", "attachments": [attachment]})
    assert view[0]["url"] == f"/api/admin/gods-eye-view/media/post-0001/image-01.{extension}"
    assert view[0]["mime_type"] == mime and view[0]["origin"] == "ai_generated"
    with pytest.raises(ValueError):
        corpus.media_file("post-0001", "../scene.png")
    with pytest.raises(FileNotFoundError):
        corpus.media_file("post-0002", f"image-01.{extension}")
    for change in ({"origin": "external"}, {"mime_type": "text/html"}, {"type": "video"},
                   {"dataset_path": "../scene.png"}, {"id": "post-0002-image-01"}):
        altered = {**attachment, **change}
        record.write_text(json.dumps({**post, "attachments": [altered]}), encoding="utf-8")
        entry["sha256"] = hashlib.sha256(record.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="manifest"):
            corpus._load_post(tmp_path, entry, [altered])


def test_seed_run_and_version_reproduce_time_order_retry_ids_and_late_delivery():
    first = corpus.events("x", "run", 0, NOW, -1, 600, seed=7)
    assert first == corpus.events("x", "run", 0, NOW, -1, 600, seed=7)
    changed = corpus.events("x", "run", 0, NOW, -1, 600, seed=8)
    assert [event["created_at"] for event in first] != [event["created_at"] for event in changed]
    new_run = corpus.events("x", "new-run", 0, NOW, -1, 600, seed=7)
    assert not {event["source_id"] for event in first}.intersection(event["source_id"] for event in new_run)
    main = sorted((event for event in first if int(event["raw_metadata"]["fixture_id"].split("-")[1]) <= 24), key=lambda event: event["raw_metadata"]["fixture_id"])
    assert [event["created_at"] for event in main] == sorted(event["created_at"] for event in main)
    late = [event for event in corpus.events("x", "run", 0, NOW, 500, 599, seed=7) if event["raw_metadata"]["offset_seconds"] < 300]
    assert len(late) == 2
    with pytest.raises(ValueError):
        corpus.events("x", "run", 0, NOW, -1, 600, seed=True)
    with pytest.raises(ValueError):
        corpus.events("x", "run", 0, NOW, -1, 600, version="unknown")


def test_fixture_messages_fit_runtime_and_keep_evaluation_copies_as_one_source():
    posts = {post["fixture_id"]: post for post in corpus.load(corpus.dataset_root())}
    assert all(isinstance(post["message"], str) and 0 < len(post["message"]) <= 12000 for post in posts.values())
    truth = json.loads((corpus.dataset_root() / "evaluation/ground-truth.json").read_text(encoding="utf-8"))["posts"]
    events = {event["raw_metadata"]["fixture_id"]: event
              for platform in PLATFORMS for event in corpus.events(platform, "copy-contract", 0, NOW, -1, 600)}
    duplicates = [item for item in truth if item["duplicate_of"]]
    assert len(duplicates) == 8
    for duplicate in duplicates:
        copied, original = duplicate["fixture_id"], duplicate["duplicate_of"]
        assert posts[copied]["message"].endswith(posts[original]["message"])
        assert corroboration([events[original], events[copied]])["independent_source_count"] == 1


@pytest.mark.parametrize("platform,base", [("x", 0), ("facebook", 24), ("instagram", 48), ("tiktok", 72)])
def test_each_main_scenario_crosses_five_ten_twenty_originals_in_five_minutes(platform, base):
    for end, count in ((65, 6), (130, 11), (250, 21)):
        events = corpus.events(platform, "threshold-run", 0, NOW, -1, end)
        main = [event for event in events if base < int(event["raw_metadata"]["fixture_id"].split("-")[1]) <= base + 24]
        assert len(main) >= count
        assert len({event["raw_metadata"]["author_id"] for event in main}) == len(main)
        assert corroboration(main)["independent_source_count"] == len(main)


def test_continuous_corpus_retry_filter_seed_and_legacy_checkpoint_compatibility():
    source = default_source("x")
    control = {"run_id": "new", "anchor_at": NOW, "seed": 12}
    events, cursor = capture.continuous_batch(source, control, {}, NOW)
    assert len(events) == 1 and events[0]["raw_metadata"]["fixture_id"] == "post-0001"
    assert (events, cursor) == capture.continuous_batch(source, control, {}, NOW)
    assert cursor["dataset_version"] == "bogota-v1" and cursor["dataset_seed"] == 12
    assert capture.continuous_batch(source, control, cursor, NOW + 60) is None
    more, next_cursor = capture.continuous_batch(source, {**control, "seed": 90}, cursor, NOW + 300)
    assert next_cursor["dataset_seed"] == 12
    assert not {item["source_id"] for item in events}.intersection(item["source_id"] for item in more)
    matched, _ = capture.continuous_batch({**source, "query": "@mar_rojas"}, control, {}, NOW + 300)
    assert [item["raw_metadata"]["fixture_id"] for item in matched] == ["post-0001"]
    assert capture.continuous_batch({**source, "query": "#not-present"}, control, {}, NOW + 300)[0] == []
    legacy = {"run_id": "new", "query": source["query"], "elapsed": 0, "interval_minutes": 5, "next_due": NOW + 300}
    old_events, old_cursor = capture.continuous_batch(source, control, legacy, NOW + 600)
    assert any(item["source_id"] == "new:0:ubicacion-1" for item in old_events)
    assert not any(item["source_id"].endswith(":lluvia-1") for item in old_events)
    assert all("dataset_version" not in item["raw_metadata"] for item in old_events)
    assert "dataset_version" not in old_cursor and "dataset_version" not in old_cursor["batch_key"]
    assert not capture.matches("RT @mar_rojas: inundacion", capture.search_terms("inundacion -is:retweet"))


def test_fictional_identities_and_contextual_media_cover_every_platform():
    posts = corpus.load(corpus.dataset_root())
    images = [image for post in posts for image in post["attachments"]]
    assert len(images) == len({image["sha256"] for image in images}) == len({image["dataset_path"] for image in images}) == 72
    assert len({post["author"]["username"] for post in posts}) == len(posts)
    assert len({post["author"]["display_name"] for post in posts}) == len(posts)
    for platform in PLATFORMS:
        selected = [post for post in posts if post["platform"] == platform]
        assert sum(bool(post["attachments"]) for post in selected) == 18
    assert all("sim_bogota_" not in post["message"] for post in posts)
    for fixture in ("post-0001", "post-0025", "post-0049", "post-0073", "post-0097"):
        path, attachment = corpus.media_file(fixture, "image-01.svg")
        assert "AI-generated" in attachment["alt_text"] and attachment["origin"] == "ai_generated"
        assert path.suffix == ".webp" and attachment["is_simulated"] is True
    # Do not invent visual support for the scenario's exaggerated or contradicting claims.
    excluded = {f"post-{base + offset:04d}" for base in (0, 24, 48, 72) for offset in (7, 18)}
    excluded.update(("post-0102", "post-0108", "post-0114", "post-0120"))
    assert all(not post["attachments"] for post in posts if post["fixture_id"] in excluded)


def test_refresh_preserves_scenario_and_is_reproducible(tmp_path):
    from app.labs.gods_eye_view.source.social_networks.refresh import refresh, optimize_media
    root = runtime_copy(tmp_path)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    refresh(root)
    optimize_media(root)  # Already encoded assets do not require Pillow or a second lossy pass.
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert not (root / "evaluation").exists()


def test_encoded_media_preserves_generation_provenance_resolution_and_release_size():
    from app.labs.gods_eye_view.source.social_networks.refresh import generated_assets, media_dimensions
    root = corpus.dataset_root()
    assets = generated_assets(root)
    assert len(assets) == 72 and len({asset["source_png"]["sha256"] for asset in assets.values()}) == 72
    assert sum((root / asset["dataset_path"]).stat().st_size for asset in assets.values()) < 80 * 1024 * 1024
    for asset in assets.values():
        assert asset["mime_type"] == "image/webp" and asset["dataset_path"].endswith(".webp")
        assert re.fullmatch(r"[a-f0-9]{64}", asset["source_png"]["sha256"])
        assert asset["source_png"]["dataset_path"].endswith(".png")
        assert not (root / asset["source_png"]["dataset_path"]).exists()
        assert asset["encoding"]["quality"] == 90 and asset["encoding"]["method"] == 6
        assert asset["encoding"]["resized"] is False and asset["encoding"]["cropped"] is False
        data = (root / asset["dataset_path"]).read_bytes()
        assert media_dimensions(data) == (asset["width"], asset["height"])
        with pytest.raises(ValueError, match="format"):
            media_dimensions(data[:-1])


def test_refresh_refuses_tampered_input_before_rewriting_any_files(tmp_path):
    from app.labs.gods_eye_view.source.social_networks.refresh import refresh
    root = runtime_copy(tmp_path)
    target = root / "posts/post-0120/post.json"
    target.write_bytes(target.read_bytes() + b" ")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="checksum"):
        refresh(root)
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_duplicate_photo_assignment_is_rejected_before_refresh_or_capture(tmp_path):
    from app.labs.gods_eye_view.source.social_networks.refresh import refresh
    root = runtime_copy(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    first, second = manifest["posts"][:2]
    original = json.loads((root / first["path"]).read_text(encoding="utf-8"))["attachments"][0]
    post = json.loads((root / second["path"]).read_text(encoding="utf-8"))
    previous = post["attachments"][0]
    replacement = {**original, "id": previous["id"]}
    post["attachments"] = [replacement]
    data = json.dumps(post, ensure_ascii=False).encode("utf-8")
    (root / second["path"]).write_bytes(data)
    second["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["media"][manifest["media"].index(previous)] = replacement
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="reuse"):
        corpus.load(root)
    provenance_path = root / "media/generation.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assets = {asset["fixture_id"]: asset for asset in provenance["assets"]}
    assets["post-0002"].update({key: assets["post-0001"][key] for key in ("dataset_path", "sha256")})
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    before = {path: path.read_bytes() for path in [root / "manifest.json", *root.glob("posts/*/post.json")]}
    with pytest.raises(ValueError, match="unique image"):
        refresh(root)
    assert {path: path.read_bytes() for path in before} == before


def test_presentation_refreshes_only_verified_simulated_fixtures_without_mutating_capture():
    event = corpus.events("x", "run", 0, NOW, -1, 0)[0]
    current_message = event["text"]
    legacy = {**event["attachments"][0], "dataset_path": "posts/post-0001/media/image-01.svg",
              "mime_type": "image/svg+xml", "origin": "synthetic_diagram"}
    event.update(text="Old captured fixture wording", content_hash="original-hash",
                 claims=[{"evidence_text": "Old captured fixture wording"}],
                 username="sim_bogota_0001", display_name="Fictional Bogotá Reporter 001", attachments=[legacy])
    original = json.loads(json.dumps(event))
    patch = corpus.presentation(event)
    assert patch["username"] == "mar_rojas" and patch["display_name"] == "Mariana Rojas"
    assert patch["text"] == post_view(event)["text"] == current_message
    assert len(patch["attachments"]) == 1
    assert patch["attachments"][0]["id"] == legacy["id"]
    assert patch["attachments"][0]["origin"] == "ai_generated"
    rendered = post_view(event)["attachments"]
    assert len(rendered) == 1 and rendered[0]["url"].endswith("/post-0001/image-01.webp")
    assert set(patch) == {"text", "username", "display_name", "attachments"} and event == original
    for change in ({"mode": "real"}, {"platform": "facebook"}, {"raw_metadata": {}},
                   {"raw_metadata": {**event["raw_metadata"], "dataset_version": "other"}},
                   {"raw_metadata": {**event["raw_metadata"], "author_id": "another-author"}},
                   {"raw_metadata": {**event["raw_metadata"], "fixture_id": "post-9999"}}):
        unverified = {**event, **change}
        assert corpus.presentation(unverified) == {}
        assert post_view(unverified)["text"] == original["text"]
    patch["attachments"][0]["alt_text"] = "caller mutation"
    assert "caller mutation" not in corpus.presentation(event)["attachments"][0]["alt_text"]


def test_runtime_docker_excludes_offline_evaluation_from_the_canonical_corpus():
    root = Path(__file__).resolve().parents[3]
    dockerfile = (root / "docker/Dockerfile").read_text()
    ignored = set((root / ".dockerignore").read_text().splitlines())
    assert "COPY apps/backend/app ./app" in dockerfile
    assert "COPY datasets/" not in dockerfile
    assert "ENV GODS_EYE_DATASET_ROOT=/opt/aidp-lab/app/labs/gods_eye_view/source/social_networks/v1" in dockerfile
    assert "apps/backend/app/labs/gods_eye_view/source/**/evaluation/" in ignored
    for version in ("v1", "v2"):
        path = root / "apps/backend/app/labs/gods_eye_view/source/social_networks" / version
        assert (path / "evaluation/ground-truth.json").is_file()
        assert (path / "manifest.json").is_file() and (path / "posts").is_dir() and (path / "media").is_dir()


def test_v2_has_twelve_attributed_images_and_keeps_evaluation_out_of_capture():
    version = "bogota-v2"
    posts = corpus.load(corpus.dataset_root(version), version)
    truth = json.loads((corpus.dataset_root(version) / "evaluation/ground-truth.json").read_text(encoding="utf-8"))["posts"]
    assert {item["fixture_id"] for item in truth} == {post["fixture_id"] for post in posts}
    assert {item["expected_category"] for item in truth} <= set(CATEGORIES)
    assert len(posts) == 16 and all(sum(post["platform"] == platform for post in posts) == 4 for platform in PLATFORMS)
    images = [item for post in posts for item in post["attachments"]]
    assert len(images) == 12 and len({item["sha256"] for item in images}) == 4
    assert sum(bool(item.get("reused_from")) for item in images) == 8
    events = {event["raw_metadata"]["fixture_id"]: event for platform in PLATFORMS
              for event in corpus.events(platform, "human-v2", 0, NOW, -1, 600, version=version)}
    for base in (1, 5, 9, 13):
        original, *copies = [events[f"post-{number:04d}"] for number in range(base, base + 3)]
        assert all(copy["attachments"][0]["reused_from"] == original["attachments"][0]["id"] for copy in copies)
        assert corroboration([original, *copies])["independent_source_count"] == 1
    for event in events.values():
        assert event["mode"] == "Synthetic" and event["is_simulated"] is True
        assert event["raw_metadata"]["dataset_version"] == version and event["raw_metadata"]["synthetic"] is True
        assert not re.search(r"\b(?:prueba|simulad[oa]|sint[eé]tic[oa]|demo)\b", event["text"], re.I)
        assert not {"expected_category", "scenario_assertion", "duplicate_of", "evidence_role"} & event.keys()
        assert not {"expected_category", "scenario_assertion", "duplicate_of", "evidence_role"} & event["raw_metadata"].keys()
    assert len(landing.records(landing.page(list(events.values()))[1])) == 16


def test_v2_selection_keeps_an_existing_v1_cursor_and_never_refreshes_captured_v2_text():
    source = {**default_source("x"), "query": ""}
    control = {"run_id": "existing-v1", "anchor_at": NOW, "seed": 12}
    _, cursor = capture.continuous_batch(source, control, {}, NOW)
    retained, updated = capture.continuous_batch(source, {**control, "dataset_version": "bogota-v2"}, cursor, NOW + 300)
    assert updated["dataset_version"] == "bogota-v1"
    assert all(event["raw_metadata"]["dataset_version"] == "bogota-v1" for event in retained)
    selected = {**control, "run_id": "new-v2", "dataset_version": "bogota-v2"}
    events, next_cursor = capture.continuous_batch(source, selected, {}, NOW + 300)
    assert next_cursor["dataset_version"] == "bogota-v2"
    assert (events, next_cursor) == capture.continuous_batch(source, selected, {}, NOW + 300)
    assert not {event["source_id"] for event in events} & {event["source_id"] for event in retained}
    event = {**events[0], "text": "Original captured wording", "content_hash": "original-capture-hash"}
    assert corpus.presentation(event) == {}
    assert post_view(event)["text"] == "Original captured wording"
    assert event["content_hash"] == "original-capture-hash"


def test_v2_media_urls_are_versioned_and_use_the_same_authenticated_handler(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import LOCAL_COOKIE_NAME, create_app
    from app.security import issue_session

    event = corpus.events("x", "media-v2", 0, NOW, -1, 600, version="bogota-v2")[0]
    url = post_view(event)["attachments"][0]["url"]
    assert url == "/api/admin/gods-eye-view/media/post-0001/image-01.png?dataset_version=bogota-v2"
    settings = Settings(local_development_mode=True, cookie_secure=False,
        aidp_settings_file=str(tmp_path / "settings.json"), session_secret_file=str(tmp_path / "session.key"))
    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 401
        client.cookies.set(LOCAL_COOKIE_NAME, issue_session(client.app.state.session_key, "admin"))
        new_image = client.get(url)
        old_image = client.get("/api/admin/gods-eye-view/media/post-0001/image-01.webp")
        assert new_image.status_code == old_image.status_code == 200
        assert new_image.headers["content-type"] == "image/png"
        assert old_image.headers["content-type"] == "image/webp"
        assert new_image.content != old_image.content
        assert hashlib.sha256(new_image.content).hexdigest() == event["attachments"][0]["sha256"]
        assert client.get(url.replace("bogota-v2", "bogota-v3")).status_code == 404
        assert client.get(url.replace("bogota-v2", "../v1")).status_code == 404
    for version in ("bogota-v3", {}, "../v1"):
        assert attachments_for({**event, "raw_metadata": {**event["raw_metadata"], "dataset_version": version}}) == []


def test_v2_reuse_requires_the_exact_original_and_v1_cannot_load_a_v2_manifest(tmp_path):
    source = corpus.dataset_root("bogota-v2")
    shutil.copytree(source, tmp_path / "v2")
    root = tmp_path / "v2"
    with pytest.raises(ValueError, match="version"):
        corpus.load(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["posts"][1]
    post = json.loads((root / entry["path"]).read_text(encoding="utf-8"))
    previous = post["attachments"][0]
    for reused_from in (None, "post-0005-image-01"):
        attachment = {**previous, "reused_from": reused_from}
        post["attachments"] = [attachment]
        data = (json.dumps(post, ensure_ascii=False) + "\n").encode()
        (root / entry["path"]).write_bytes(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        manifest["media"][1] = attachment
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="reused images"):
            corpus.load(root, "bogota-v2")


def test_explicitly_paused_source_never_falls_back_to_legacy_replay():
    source = {**default_source("x"), "capture_paused": True}
    inputs = list(capture.inputs([source], {}, {"run_id": "legacy", "status": "running"}))
    assert {item[0]["platform"] for item in inputs} == {"sensor", "sire", "linea123"}


def test_packaged_sensor_corpus_is_the_exact_reproducible_generator_output():
    from datetime import datetime
    from app.gods_eye_view.sensors import generate_batch, text_files

    root = Path(corpus.__file__).parents[1] / "labs/gods_eye_view/source/sensors/colombia/v1"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    generated = text_files(generate_batch(datetime.fromisoformat(manifest["observed_batch_at"]).timestamp()))
    assert set(generated) == {entry["path"] for entry in manifest["files"]}
    assert manifest["records"] == sum(entry["records"] for entry in manifest["files"]) == 4000
    for entry in manifest["files"]:
        content = (root / entry["path"]).read_bytes()
        assert content == generated[entry["path"]]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert len(content.splitlines()) == entry["records"]
