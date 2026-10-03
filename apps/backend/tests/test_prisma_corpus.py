"""Corpus integrity, evaluation isolation and durable continuous-capture compatibility."""
import json
from pathlib import Path
import shutil

import pytest

from app.prisma import capture, corpus, landing
from app.prisma.core import PLATFORMS, corroboration, default_source

NOW = 1791209100.0


def runtime_copy(tmp_path):
    original = corpus.dataset_root()
    shutil.copy(original / "manifest.json", tmp_path / "manifest.json")
    shutil.copytree(original / "posts", tmp_path / "posts")
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
        assert event["is_simulated"] and event["mode"] == "simulation" and event["source_uri"] == ""
        assert event["username"].startswith("sim_bogota_") and event["display_name"].startswith("Fictional ")
        assert event["country"] == "Colombia" and event["city"] == "Bogotá"
        assert event["location_precision"] in {"locality_anchor", "unresolved"}
        assert event["location_method"] == ("synthetic_locality_anchor" if event["locality"] else "unresolved")
        assert not forbidden.intersection(event) and not forbidden.intersection(event["raw_metadata"])
    _, encoded = landing.page(events)
    assert "scenario_assertion" not in encoded.decode() and "expected_category" not in encoded.decode()
    assert len(landing.records(encoded)) == 120


def test_bundled_svg_is_allowlisted_and_hash_checked_even_after_cached_load(tmp_path, monkeypatch):
    root = runtime_copy(tmp_path)
    monkeypatch.setenv("GODS_EYE_DATASET_ROOT", str(root))
    path, metadata = corpus.media_file("post-0001", "image-01.svg")
    assert path.is_relative_to(root) and metadata["origin"] == "synthetic_diagram"
    assert metadata["license"] == "CC0-1.0" and metadata["mime_type"] == "image/svg+xml"
    svg = path.read_text(encoding="utf-8")
    assert "NOT A PHOTOGRAPH" in svg and "<script" not in svg and "foreignObject" not in svg
    with pytest.raises(ValueError):
        corpus.media_file("../post-0001", "image-01.svg")
    with pytest.raises(FileNotFoundError):
        corpus.media_file("post-0002", "image-01.svg")
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        corpus.media_file("post-0001", "image-01.svg")


def test_tampered_post_is_rejected_before_generation(tmp_path):
    root = runtime_copy(tmp_path)
    path = root / "posts/post-0001/post.json"
    path.write_text(path.read_text(encoding="utf-8").replace("Kennedy", "Other place"), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        corpus.load(root)


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
    matched, _ = capture.continuous_batch({**source, "query": "@sim_bogota_0001"}, control, {}, NOW + 300)
    assert [item["raw_metadata"]["fixture_id"] for item in matched] == ["post-0001"]
    assert capture.continuous_batch({**source, "query": "#not-present"}, control, {}, NOW + 300)[0] == []
    legacy = {"run_id": "new", "query": source["query"], "elapsed": 0, "interval_minutes": 5, "next_due": NOW + 300}
    old_events, old_cursor = capture.continuous_batch(source, control, legacy, NOW + 600)
    assert any(item["source_id"].endswith(":lluvia-1") for item in old_events)
    assert all("dataset_version" not in item["raw_metadata"] for item in old_events)
    assert "dataset_version" not in old_cursor and "dataset_version" not in old_cursor["batch_key"]
    assert not capture.matches("RT @sim_bogota_0001: inundacion", capture.search_terms("inundacion -is:retweet"))


def test_runtime_docker_copies_only_posts_and_manifest():
    dockerfile = Path(__file__).resolve().parents[3] / "docker/Dockerfile"
    copies = [line for line in dockerfile.read_text().splitlines() if line.startswith("COPY datasets/")]
    assert len(copies) == 2
    assert any("/posts " in line for line in copies) and any("/manifest.json " in line for line in copies)
    assert all("evaluation" not in line for line in copies)


def test_explicitly_paused_source_never_falls_back_to_legacy_replay():
    source = {**default_source("x"), "capture_paused": True}
    inputs = list(capture.inputs([source], {}, {"run_id": "legacy", "status": "running"}))
    assert {item[0]["platform"] for item in inputs} == {"sensor", "sire", "linea123"}
