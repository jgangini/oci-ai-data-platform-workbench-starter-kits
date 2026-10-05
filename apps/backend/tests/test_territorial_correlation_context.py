import copy
import json
from types import SimpleNamespace

import pytest

from app.territorial import correlation, pipeline, sensor_capture, synthetic_reset
from app.territorial.core import build_snapshot, corroboration, default_source, normalize_event, utc_text
from app.territorial.store import TerritorialStore
from test_territorial_pipeline import CONFIG, NOW, runtime


def report(index, offset=0, stance="supports", **changes):
    event = normalize_event({"platform": "x", "source_id": str(index), "text": f"Inundación Kennedy reporte {index}",
        "mode": "Synthetic", "is_simulated": True, "created_at": utc_text(NOW + offset),
        "country": "Colombia", "city": "Bogotá", "locality": "Kennedy", "category": "inundacion",
        "lat": 4.627, "lon": -74.155, "location_method": "coordinates", "confidence": .7,
        "raw_metadata": {"author_id": str(index)}, **changes})
    event["claims"] = [{"category": event["category"], "locality": event["locality"],
        "relation": stance, "evidence_text": event["text"]}]
    return event


def reading(index, offset=0, **changes):
    return {"id": f"reading-{index}", "sensor_id": f"station-{index}", "sensor_type": "rainfall",
        "observed_at": utc_text(NOW + offset), "lat": 4.627, "lon": -74.155,
        "mode": "Synthetic", "is_simulated": True, "locality": "Kennedy", "municipality": "Bogotá",
        "country": "Colombia", "value": 1, "unit": "mm/h", "status": "normal", **changes}


def snapshot(events, **kwargs):
    return build_snapshot(events, {}, "test", "", rules={"x": default_source("x")}, now=NOW, **kwargs)


def context_for(result, evidence_id):
    return next(item["correlation_context"] for item in result["incidents"] if evidence_id in item["evidence_ids"])


def test_social_stances_keep_copy_links_and_count_accounts_not_people_or_other_channels():
    events = [report(1, -10, text="Inundación Kennedy"),
        report(2, -9, platform="facebook", text="  INUNDACIÓN Kennedy  "),
        report(3, -8, stance="contradicts", text="No hay inundación Kennedy"),
        report(4, -7, raw_metadata={"author_id": "1"}),
        report(5, -6, platform="instagram", raw_metadata={}),
        report(6, -5, stance="unclassified", platform="tiktok"),
        report(7, -4, platform="sensor"), report(8, -3, platform="linea123")]
    result = snapshot(events)
    social = result["incidents"][0]["correlation_context"]["social"]
    assert social == {"posts": 6, "supports": 4, "contradicts": 1, "unclassified": 1, "exact_copies": 1,
        "accounts_per_platform": {"x": 2, "facebook": 1, "instagram": 0, "tiktok": 1}, "unknown_account_posts": 1}
    link = next(item for item in result["event_posts"] if item["post_key"] == "facebook:2")
    assert link["relation"] == "duplicate" and link["claim_relation"] == "supports" and link["duplicate_of"] == "x:1"
    assert next(item for item in result["event_posts"] if item["post_key"] == "x:3")["claim_relation"] == "contradicts"
    assert len(result["evidence"]) == 8  # Existing institutional evidence is retained, not counted as social.


def test_repeated_claims_do_not_become_copies_and_copy_preserves_its_contradiction():
    original = report(1, -1)
    original["claims"].append({**original["claims"][0], "relation": "contradicts"})
    duplicate = report(2, text=original["text"], stance="contradicts")
    links = snapshot([original, duplicate])["event_posts"]
    assert [item["relation"] for item in links] == ["supports", "contradicts", "duplicate"]
    assert links[-1]["claim_relation"] == "contradicts" and links[-1]["duplicate_of"] == "x:1"
    assert all(item["duplicate_of"] is None for item in links[:2])


def test_shared_image_with_different_captions_is_not_independent_and_retains_contradictions():
    image = {"type": "image", "sha256": "a" * 64}
    events = [report(1, -3, text="Agua entró por la puerta de mi casa", attachments=[image]),
        report(2, -2, platform="facebook", text="Comparto la foto de otra persona del barrio", attachments=[dict(image)]),
        report(3, -1, platform="instagram", stance="contradicts", text="Esa imagen es antigua; hoy no veo inundación", attachments=[image]),
        report(4, text="Hay otra calle con alcantarillas desbordadas", attachments=[{"type": "image", "sha256": "b" * 64}])]
    before = copy.deepcopy(events)
    result = snapshot(events)
    assert events == before
    incident = result["incidents"][0]
    assert incident["independent_source_count"] == 2 and incident["corroboration_score"] == 25
    assert incident["contradicting_report_count"] == 1 and incident["review_status"] == "pending"
    assert incident["correlation_context"]["social"]["exact_copies"] == 2
    links = {item["post_key"]: item for item in result["event_posts"]}
    for key in ("facebook:2", "instagram:3"):
        assert links[key]["relation"] == "duplicate" and links[key]["duplicate_of"] == "x:1"
        assert "image SHA-256" in links[key]["explanation"]
    assert links["instagram:3"]["claim_relation"] == "contradicts"
    assert links["x:4"]["duplicate_of"] is None


def test_shared_images_do_not_merge_incidents_or_cross_provenance_boundaries():
    image = {"type": "image", "sha256": "f" * 64}
    synthetic = report(1, -2, attachments=[image])
    real = report(2, -1, text=synthetic["text"], mode="real", is_simulated=False, attachments=[image])
    elsewhere = report(3, locality="Bosa", text="Otro lugar reporta lluvia", attachments=[image])
    assert corroboration([synthetic, real])["independent_source_count"] == 2
    assert all(item["duplicate_of"] is None for item in correlation.relations("legacy-mixed", [synthetic, real]))
    result = snapshot([synthetic, real, elsewhere])
    assert len(result["incidents"]) == 3
    assert all(item["independent_source_count"] == 1 for item in result["incidents"])


def test_copy_explanation_matches_text_origin_when_its_image_came_from_another_post():
    image = {"type": "image", "sha256": "a" * 64}
    text_origin = report(1, -2, text="Inundación junto a mi casa")
    image_origin = report(2, -1, text="Reviso otra calle del barrio", attachments=[image])
    mixed = report(3, text=text_origin["text"], attachments=[image])
    link = next(item for item in snapshot([mixed, image_origin, text_origin])["event_posts"] if item["post_key"] == "x:3")
    assert link["duplicate_of"] == "x:1" and link["relation"] == "duplicate"
    assert link["explanation"] == "Exact normalized content matches an earlier report"


@pytest.mark.parametrize("attachment", [None, {}, {"type": "image", "sha256": ""},
    {"type": "image", "sha256": "g" * 64}, {"type": "image", "sha256": "a" * 63},
    {"type": "image", "sha256": ["a" * 64]}, {"type": "video", "sha256": "a" * 64}])
def test_invalid_or_missing_image_hashes_do_not_create_copy_links(attachment):
    events = [report(1, -1, text="Agua entró por la puerta", attachments=[attachment]),
        report(2, text="Se tapó una alcantarilla", attachments=[attachment])]
    incident = snapshot(events)["incidents"][0]
    assert incident["independent_source_count"] == 2
    assert incident["correlation_context"]["social"]["exact_copies"] == 0


def test_shared_image_digest_case_and_multiple_claims_do_not_inflate_copy_count():
    original = report(1, -1, attachments=[{"type": "image", "sha256": "a" * 64}])
    original["claims"].append({**original["claims"][0], "relation": "contradicts"})
    reused = report(2, text="Reenvío la fotografía que publicó mi vecino",
        attachments=[{"type": "image", "sha256": "A" * 64}, {"type": "image", "sha256": "a" * 64}])
    result = snapshot([original, reused])
    assert result["incidents"][0]["independent_source_count"] == 1
    assert result["incidents"][0]["correlation_context"]["social"]["exact_copies"] == 1
    assert [item["relation"] for item in result["event_posts"]] == ["supports", "contradicts", "duplicate"]


def test_history_links_last_day_without_merging_or_expanding_activity_window():
    events = [report(0), *[report(index, -index * 7200) for index in range(1, 8)],
        report(8, -86400), report(9, -90000, country="Perú"),
        report(10, -3600, country="Perú"), report(11, -3600, city="Medellín"),
        report(12, -3600, mode="real", is_simulated=False), report(13, 3600)]
    initial = snapshot(events)
    current = next(item for item in initial["incidents"] if "x:0" in item["evidence_ids"])
    history = current["correlation_context"]["historical_related"]
    assert history["count"] == 8 and len(history["incident_ids"]) == 5
    assert current["evidence_ids"] == ["x:0"] and current["report_counts"] == {"x": 1}
    assert current["correlation_windows_minutes"] == {"x": 30}
    assert all(next(item for item in initial["incidents"] if item["id"] == key)["mode"] == "Synthetic" for key in history["incident_ids"])
    replay = snapshot(list(reversed(events)), previous=initial["incidents"])
    assert replay["incidents"] == initial["incidents"]


@pytest.mark.parametrize("changes,expected", [({}, 1), ({"country": "", "city": ""}, 1),
    ({"country": "COLOMBIA", "city": "BOGOTA"}, 1), ({"country": "Perú"}, 0),
    ({"city": "Cali"}, 0), ({"category": "incendio"}, 0), ({"locality": "Bosa"}, 0),
    ({"locality": "Sin localizar"}, 0)])
def test_history_requires_compatible_jurisdiction_category_and_locality(changes, expected):
    result = snapshot([report(1), report(2, -3600, **changes)])
    assert context_for(result, "x:1")["historical_related"]["count"] == expected


def test_history_excludes_outside_day_even_though_existing_membership_is_retained():
    result = snapshot([report(1), report(2, -86401)])
    assert context_for(result, "x:1")["historical_related"] == {"count": 0, "incident_ids": []}
    assert len(result["incidents"]) == 2


def test_legacy_mixed_jurisdiction_membership_is_retained_but_cannot_create_associations():
    events = [report(1), report(2, -1, country="Perú"), report(3, -3600)]
    previous = [{"id": "reviewed-legacy", "evidence_ids": ["x:1", "x:2"],
        "category": "inundacion", "locality": "Kennedy", "revision": 2}]
    reviews = {"reviewed-legacy": {"status": "validated", "note": "Keep analyst decision", "evidence_ids": ["x:1", "x:2"]}}
    result = build_snapshot(events, reviews, "test", "", rules={"x": default_source("x")},
        previous=previous, now=NOW, sensors=[reading(1)])
    incident = next(item for item in result["incidents"] if item["id"] == "reviewed-legacy")
    assert incident["evidence_ids"] == ["x:1", "x:2"] and incident["review_status"] == "validated"
    context = incident["correlation_context"]
    assert context["jurisdiction_status"] == "conflicting"
    assert context["historical_related"] == {"count": 0, "incident_ids": []}
    assert context["sensors"] == {"status": "insufficient_jurisdiction", "count": 0, "samples": []}
    assert context_for(result, "x:3")["historical_related"]["count"] == 0


@pytest.mark.parametrize("changes", [{"country": "Perú"}, {"city": "Cali"}])
def test_new_reports_with_conflicting_jurisdiction_do_not_join_an_existing_group(changes):
    first = snapshot([report(1, -1)])
    result = snapshot([report(1, -1), report(2, **changes)], previous=first["incidents"])
    assert len(result["incidents"]) == 2
    original = next(item for item in result["incidents"] if item["id"] == first["incidents"][0]["id"])
    assert original["evidence_ids"] == ["x:1"]
    assert all(item["correlation_context"]["historical_related"]["count"] == 0 for item in result["incidents"])


def test_missing_jurisdiction_does_not_invent_a_conflict_for_existing_grouping():
    result = snapshot([report(1, -1), report(2, country="", city="")])
    assert len(result["incidents"]) == 1 and result["incidents"][0]["evidence_ids"] == ["x:1", "x:2"]


def test_sensors_filter_provenance_jurisdiction_space_and_time_with_bounded_neutral_refs():
    rows = [reading(i, -i) for i in range(8)] + [reading("old", -86401), reading("boundary", -86400),
        reading("future", 1), reading("far", lon=-74.19), reading("wrong-mode", mode="real", is_simulated=False),
        reading("wrong-provenance", is_simulated=False), reading("wrong-country", country="Perú"),
        reading("wrong-city", municipality="Medellín"), reading("wrong-locality", locality="Bosa")]
    result = snapshot([report(1)], sensors=rows)
    context = result["incidents"][0]["correlation_context"]
    assert context["sensors"]["status"] == "observed" and context["sensors"]["count"] == 9
    assert context["sensors"]["samples"] == [{"id": f"reading-{i}", "distance_km": 0, "time_delta_minutes": round(-i / 60, 2)} for i in range(5)]
    assert context["criteria"]["association_only"] is True
    assert context["criteria"]["window_minutes"] == 1440 and context["criteria"]["sensor_radius_km"] == 2
    assert result["sensors"] == rows


def test_sensor_after_incident_is_context_only_if_already_observed_at_publication():
    result = snapshot([report(1, -3600)], sensors=[reading(1), reading(2, 1)])
    assert context_for(result, "x:1")["sensors"] == {"status": "observed", "count": 1,
        "samples": [{"id": "reading-1", "distance_km": 0, "time_delta_minutes": 60}]}


@pytest.mark.parametrize("method", ["text_locality_anchor", "text_locality_centroid", "unresolved"])
def test_text_anchors_do_not_claim_measured_sensor_proximity(method):
    event = report(1)
    event["location_method"] = method
    assert context_for(snapshot([event], sensors=[reading(1)]), "x:1")["sensors"] == {
        "status": "insufficient_location_precision", "count": 0, "samples": []}


def test_human_review_coordinates_allow_context_without_changing_review_confidence_or_activity():
    events = [report(1, location_method="text_locality_anchor")]
    before = snapshot(events)
    incident = before["incidents"][0]
    reviews = {incident["id"]: {"status": "validated", "note": "Checked location", "evidence_ids": ["x:1"], "lat": 4.627, "lon": -74.155}}
    empty = build_snapshot(events, reviews, "test", "", rules={"x": default_source("x")}, now=NOW, sensors=[])
    result = build_snapshot(events, reviews, "test", "", rules={"x": default_source("x")}, now=NOW,
        sensors=[reading(1, sensor_type="temperature", value=40, unit="°C", status="critical")])
    current = result["incidents"][0]
    assert current["correlation_context"]["sensors"]["count"] == 1
    assert empty["incidents"][0]["correlation_context"]["sensors"]["status"] == "not_observed"
    assert {key: value for key, value in current.items() if key != "correlation_context"} == {
        key: value for key, value in empty["incidents"][0].items() if key != "correlation_context"}
    assert current["review_status"] == "validated" and current["confidence"] == .7
    assert current["category"] == "inundacion" and current["severity"] == "medium"


def test_four_thousand_sensors_are_bucketed_before_distance_checks(monkeypatch):
    actual_distance = correlation.distance_km
    checked = []
    monkeypatch.setattr(correlation, "distance_km", lambda incident, sensor: checked.append(sensor["id"]) or actual_distance(incident, sensor))
    rows = [reading(i, locality="Bosa") for i in range(3990)] + [reading(i) for i in range(3990, 4000)]
    context = context_for(snapshot([report(1)], sensors=rows), "x:1")
    assert len(checked) == 10 and context["sensors"]["count"] == 10 and len(context["sensors"]["samples"]) == 5
    assert len(json.dumps(context)) < 1500


def test_context_and_revision_do_not_tick_with_clock_and_legacy_sensor_shape_is_preserved():
    events, rows = [report(1)], [reading(1, -60)]
    first = snapshot(events, sensors=rows)
    replay = build_snapshot(events, {}, "test", "", rules={"x": default_source("x")},
        now=NOW + 1, sensors=rows, previous=first["incidents"])
    assert replay == first
    assert "sensors" not in snapshot(events)
    assert snapshot([], sensors=[])["sensors"] == []


def test_pipeline_applies_overrides_before_context_and_publishes_same_gold_adb_snapshot(runtime):
    _log, docs, publications, lake, objects = runtime
    rows = [reading(1, lon=-74.25)]
    lake.sensors = SimpleNamespace(latest=lambda _now: copy.deepcopy(rows))
    docs["reviews"] = {"sensor_locations": {rows[0]["sensor_id"]: {"lat": 4.627, "lon": -74.155}}}
    events = [report(1)]
    first = pipeline.publish_snapshot(object(), objects, lake, CONFIG, events, {}, {}, NOW, rules={"x": default_source("x")})
    assert first["incidents"][0]["correlation_context"]["sensors"]["count"] == 1
    assert rows[0]["lon"] == -74.25
    assert publications[first["version"]] == lake.current == first
    assert json.loads(objects.data[f"04_gold/prisma/snapshots/{first['version']}.json"]) == first
    second = pipeline.publish_snapshot(object(), objects, lake, CONFIG, events, {}, {}, NOW + 1, rules={"x": default_source("x")})
    assert second == first


def test_local_snapshot_supplies_readings_before_context_and_retains_replay(tmp_path, monkeypatch):
    store = TerritorialStore(tmp_path / "territorial.db", clock=lambda: NOW)
    raw = report(1)
    raw.pop("claims")
    store.persist_page("x", [raw], {})
    monkeypatch.setattr(sensor_capture, "local_latest", lambda _store: [reading(1)])
    first = store.snapshot()
    assert first["incidents"][0]["correlation_context"]["sensors"]["count"] == 1
    assert store.snapshot() == first


def test_sensor_family_pruning_updates_only_sensor_context_using_original_publication_time():
    events = [report(1), report(2, -3600)]
    rows = [reading("rain"), reading("retained", -10, sensor_type="temperature"),
        reading("future", 1, sensor_type="temperature"), reading("wrong-mode", mode="real", is_simulated=False, sensor_type="temperature")]
    source = snapshot(events, sensors=rows)
    source.update(published_at=utc_text(NOW))
    source["incidents"][0].update(review_status="validated", review_note="Keep decision")
    untouched = copy.deepcopy(source)
    cleaned = synthetic_reset.prune_publication(source, "rainfall")
    assert source == untouched and cleaned["sensors"] == rows[1:]
    assert cleaned["reset_of"] == source["version"] and cleaned["version"] != source["version"]
    assert cleaned["published_at"] == source["published_at"]
    assert cleaned["evidence"] == source["evidence"] and cleaned["event_posts"] == source["event_posts"]
    for before, after in zip(source["incidents"], cleaned["incidents"]):
        assert {key: value for key, value in after.items() if key != "correlation_context"} == {
            key: value for key, value in before.items() if key != "correlation_context"}
        assert {key: value for key, value in after["correlation_context"].items() if key != "sensors"} == {
            key: value for key, value in before["correlation_context"].items() if key != "sensors"}
        sensors = after["correlation_context"]["sensors"]
        assert sensors["count"] == 1 and [item["id"] for item in sensors["samples"]] == ["reading-retained"]
    assert synthetic_reset.prune_publication(source, "rainfall") == cleaned
    assert synthetic_reset.prune_publication(cleaned, "rainfall") is None
