import asyncio
import copy
import json
from uuid import uuid4

import pytest

from app.gods_eye_view import capture, landing, x
from app.gods_eye_view.api import SourceUpdate
from app.gods_eye_view.core import default_source, simulation_events
from app.gods_eye_view.local import LocalGodsEyeViewRuntime


NOW = 1791209100.0


@pytest.mark.parametrize("query", ["a" * 500 + "\n" + "b" * 499, "😀" * 250 + "\n" + "b" * 499])
def test_search_total_accepts_exactly_1000_browser_characters(query):
    assert len(query.encode("utf-16-le")) // 2 == 1000
    assert SourceUpdate(query=query).query == query
    capture.validate_source({**default_source("x"), "query": query})
    for oversized in (query + "a", query + "\n"):
        with pytest.raises(ValueError):
            SourceUpdate(query=oversized)
        with pytest.raises(ValueError, match="1000"):
            capture.validate_source({**default_source("x"), "query": oversized})


@pytest.mark.parametrize("query", ["a" * 513, "😀" * 257, "\n".join(str(n) for n in range(11))])
def test_search_total_keeps_per_line_and_line_count_limits(query):
    with pytest.raises(ValueError, match="10 searches"):
        capture.query_lines(query)
    with pytest.raises(ValueError):
        SourceUpdate(query=query)


@pytest.mark.parametrize("mode", ["Synthetic", "simulation"])
def test_source_mode_and_legacy_configuration_are_persisted_canonically(tmp_path, mode):
    assert SourceUpdate(mode=mode).mode == "Synthetic"
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    result = asyncio.run(runtime.update_source("x", {"mode": mode, "query": "#bogota"}))
    assert result["mode"] == runtime.store.source("x")["mode"] == "Synthetic"
    runtime.credentials.put("CustomSecret", "fixture-token")
    with runtime.store.connection() as db:
        source = {**runtime.store.source("x"), "mode": "simulation", "capture_running": True,
                  "credential_configured": True, "secret_ref": "CustomSecret"}
        runtime.store._put(db, "source:x", source)
    migrated = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW).store.source("x")
    assert migrated == {**source, "mode": "Synthetic"}
    with pytest.raises(ValueError):
        SourceUpdate(mode="synthetic")


def test_csv_roundtrip_unicode_quotes_newlines_empty_results_and_provenance(tmp_path):
    event = {**simulation_events(0)[0], "text": 'Bogotá, "lluvia"\r\nsegunda línea\\n ☔'}
    key = landing.write_file(tmp_path, [event], {"platform": "x", "window": 0})
    decoded = landing.records((tmp_path / key).read_bytes())
    assert decoded[0]["text"] == event["text"] and decoded[0]["is_simulated"] is True
    empty = landing.write_file(tmp_path, [], {"platform": "x", "window": 1})
    assert empty != key and (tmp_path / empty).read_text() == "id,payload\n"
    assert landing.records((tmp_path / empty).read_bytes()) == []
    assert landing.write_file(tmp_path, [], {"platform": "x", "window": 1}) == empty
    assert landing.write_file(tmp_path, [], {"platform": "x", "window": 2}) != empty
    legacy = json.dumps({"id": "x:" + event["source_id"], "payload": json.dumps(event)}).encode()
    assert landing.records(legacy, ".ndjson") == decoded
    with pytest.raises(ValueError, match="provenance"):
        landing.page([{**event, "is_simulated": False}])
    with pytest.raises(ValueError, match="header"):
        landing.records(b"payload,id\n")


def test_multiline_searches_are_independent_and_union_deduplicates():
    source = {**default_source("x"), "query": "  #bogota #inundacion\n\n#desastre\n@IDIGER  "}
    events, _ = capture.batch(source, {"status": "running", "run_id": "one", "anchor_at": NOW, "elapsed_seconds": 0}, {}, NOW)
    assert len(events) == 1
    assert events[0]["raw_metadata"]["matched_queries"] == ["#bogota #inundacion", "#desastre"]
    assert capture.matches("Aviso @IDIGER en Bogotá", capture.search_terms("@IDIGER #Bogota"))
    assert not capture.matches("Aviso IDIGER en Bogotá", capture.search_terms("@IDIGER"))
    assert not capture.matches("Aviso @IDIGERfalso", capture.search_terms("@IDIGER"))
    with pytest.raises(ValueError):
        capture.query_lines("\n".join(str(value) for value in range(11)))


def test_per_source_run_survives_restart_and_stops_after_one_finite_scenario(tmp_path):
    now = [NOW]
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: now[0])
    asyncio.run(runtime.update_source("x", {"synthetic_batch_max": 100}))
    asyncio.run(runtime.tick())
    assert runtime.store.snapshot()["evidence"] == []  # Saving enabled is not Run.
    asyncio.run(runtime.test_source("x"))
    assert not runtime.store.source("x")["capture_running"]
    first = asyncio.run(runtime.run_source("x"))
    assert first["source"]["capture_running"] and first["source"]["last_received_count"] == 1
    assert any(item["locality"] == "Kennedy" for item in runtime.store.snapshot()["incidents"])
    original_ids = {item["id"] for item in runtime.store.snapshot()["evidence"]}
    assert not runtime.store.source("facebook")["capture_running"]
    now[0] += 600
    restarted = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: now[0])
    asyncio.run(restarted.tick())
    events = restarted.store.snapshot()["evidence"]
    assert {"sensor", "sire", "linea123"} <= {item["platform"] for item in events}
    assert len([item for item in events if item["platform"] == "x" and item["source_id"].endswith(":post-0001")]) == 1
    assert original_ids <= {item["id"] for item in events}
    assert restarted.store.source("x")["capture_running"] is False
    assert restarted.store.source("x")["next_due"] is None
    finished = copy.deepcopy(events)
    asyncio.run(restarted.update_source("x", {"interval_minutes": 2}))
    now[0] += 60
    asyncio.run(restarted.tick())
    assert restarted.store.source("x")["next_due"] is None
    assert restarted.store.snapshot()["evidence"] == finished
    before = restarted.store.snapshot()["evidence"]
    file_count = len(list((tmp_path / "prisma-landing").glob("*.csv")))
    stopped = asyncio.run(restarted.update_source("x", {"enabled": False}))
    assert stopped["capture_running"] is False
    now[0] += 600
    asyncio.run(restarted.tick())
    assert restarted.store.snapshot()["evidence"] == before
    assert len(list((tmp_path / "prisma-landing").glob("*.csv"))) == file_count


def test_overdue_synthetic_run_catches_up_once_without_republishing_a_days_cycles():
    source = {**default_source("x"), "synthetic_batch_max": 100}
    control = {"run_id": "continuous", "anchor_at": NOW}
    first, cursor = capture.continuous_batch(source, control, {}, NOW)
    ids = {item["source_id"] for item in first}
    events, cursor = capture.continuous_batch(source, control, cursor, NOW + 86400, True)
    assert not ids.intersection(item["source_id"] for item in events)
    ids.update(item["source_id"] for item in events)
    assert cursor["elapsed"] == 600 and cursor["next_due"] is None
    assert len([identifier for identifier in ids if identifier.endswith(":post-0001")]) == 1
    assert all(identifier.startswith("continuous:0:") for identifier in ids)
    assert capture.continuous_batch(source, control, cursor, NOW + 172800, True) is None


def test_existing_unversioned_continuous_cursor_keeps_legacy_generator():
    source = default_source("x")
    control = {"run_id": "existing", "anchor_at": NOW}
    cursor = {"run_id": "existing", "query": source["query"], "elapsed": 0,
              "interval_minutes": 5, "next_due": NOW + 300}
    events, resumed = capture.continuous_batch(source, control, cursor, NOW + 600)
    assert events and all(event["source_id"].startswith("existing:0:") for event in events)
    assert all("dataset_version" not in event["raw_metadata"] for event in events)
    assert "dataset_version" not in resumed and "dataset_version" not in resumed["batch_key"]
    assert resumed["elapsed"] == 600 and resumed["next_due"] is None


@pytest.mark.parametrize("elapsed", [600, 86400])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("changed_query", [False, True])
def test_exhausted_run_cannot_make_new_ids_or_dates_even_after_query_change_or_force(elapsed, force, changed_query):
    source = default_source("x")
    control = {"run_id": "finished", "anchor_at": NOW}
    cursor = {"run_id": "finished", "query": source["query"], "elapsed": elapsed,
        "interval_minutes": 5, "next_due": NOW + 300, "dataset_version": "bogota-v1", "dataset_seed": 0}
    before = copy.deepcopy(cursor)
    if changed_query:
        source["query"] = "Kennedy"
    assert capture.is_complete(control, cursor)
    assert capture.continuous_batch(source, control, cursor, NOW + 172800, force) is None
    assert cursor == before


def test_explicit_new_run_uses_selected_version_new_anchor_and_new_ids():
    source = {**default_source("x"), "query": ""}
    previous = {"run_id": str(uuid4()), "anchor_at": NOW, "dataset_version": "bogota-v1", "seed": 17}
    old_events, cursor = capture.continuous_batch(source, previous, {}, NOW + 600)
    control = {"run_id": str(uuid4()), "anchor_at": NOW + 3600, "dataset_version": "bogota-v2", "seed": 23}
    assert not capture.is_complete(control, cursor)
    new_events, restarted = capture.continuous_batch(source, control, cursor, control["anchor_at"])
    assert new_events and not {event["source_id"] for event in new_events} & {event["source_id"] for event in old_events}
    assert all(event["source_id"].startswith(control["run_id"] + ":0:bogota-v2:") for event in new_events)
    assert all(event["raw_metadata"]["dataset_version"] == "bogota-v2" and event["raw_metadata"]["dataset_seed"] == 23 for event in new_events)
    assert restarted["batch_key"]["from_seconds"] == -1 and restarted["elapsed"] == 0
    assert restarted["dataset_version"] == "bogota-v2" and restarted["run_id"] == control["run_id"]


def test_x_queries_keep_separate_cursors_stop_on_429_and_reuse_unchanged_query(monkeypatch):
    source = {**default_source("x"), "query": "#uno\n#dos", "query_version": "v1"}
    saved, pages, calls = {}, [], []
    failure = [True]
    def fetch(_client, _token, query, cursor, _now, **_):
        calls.append((query, copy.deepcopy(cursor)))
        if query == "#dos" and failure[0]:
            raise x.XFailure("rate_limited", NOW + 90)
        event = {"platform": "x", "source_id": "1", "mode": "real", "text": query}
        return [event], {"since_id": "1"}
    monkeypatch.setattr(x, "fetch_page", fetch)
    def checkpoint(value):
        saved.clear()
        saved.update(copy.deepcopy(value))
    def page(events, value):
        pages.extend(events)
        checkpoint(value)
    with pytest.raises(x.XFailure, match="rate_limited"):
        x.poll_queries(None, "token", source, {}, NOW, page, checkpoint)
    assert [query for query, _ in calls] == ["#uno", "#dos"]
    assert len(pages) == 1
    with pytest.raises(x.XFailure):
        x.poll_queries(None, "token", source, saved, NOW + 1, page, checkpoint)
    assert len(calls) == 2
    failure[0] = False
    result = x.poll_queries(None, "token", source, saved, NOW + 91, page, checkpoint)
    assert calls[2][0] == "#dos" and calls[3] == ("#uno", {"since_id": "1"})
    assert result["last_received_count"] == 1  # One original ID returned by two independent searches.
    source.update(query="#uno\n#tres", query_version="v2")
    _, state = x.query_checkpoint(source, saved)
    assert [item["cursor"] for item in state["queries"].values()] == [{"since_id": "1"}, {}]


def test_x_two_pages_per_query_and_local_landing_failure_never_advances(monkeypatch, tmp_path):
    calls = []
    def fetch(_client, _token, query, cursor, _now, **_):
        calls.append(query)
        return [{"platform": "x", "source_id": "1", "mode": "real", "text": "Bogotá", "created_at": "2026-10-05T14:05:00Z"}], {"next_token": "next"}
    monkeypatch.setattr(x, "fetch_page", fetch)
    result = x.poll_queries(None, "token", {**default_source("x"), "query": "a\nb"}, {}, NOW, lambda *_: None, lambda *_: None)
    assert calls == ["a", "a", "b", "b"] and result["status"] == "backlog"
    runtime = LocalGodsEyeViewRuntime(tmp_path, clock=lambda: NOW)
    asyncio.run(runtime.update_source("x", {"mode": "real", "query": "a", "bearer_token": "test-only"}))
    monkeypatch.setattr(landing, "write_file", lambda *_: (_ for _ in ()).throw(OSError("Injected disk failure")))
    with pytest.raises(OSError):
        asyncio.run(runtime.run_source("x"))
    assert runtime.store.checkpoint("x") == {} and runtime.store.snapshot()["evidence"] == []


def test_x_deadline_preserves_the_next_search_and_legacy_cursor(monkeypatch):
    source = {**default_source("x"), "query": "first\nsecond"}
    times = iter([0, 0, 61])
    saved, calls = {}, []
    def fetch(_client, _token, query, _cursor, _now, **_):
        calls.append(query)
        return [], {"since_id": "30"}
    monkeypatch.setattr(x, "fetch_page", fetch)
    def checkpoint(state):
        saved.update(copy.deepcopy(state))
    result = x.poll_queries(None, "token", source, {}, NOW, lambda _, state: checkpoint(state), checkpoint, clock=lambda: next(times))
    assert result["last_error"] == "capture_deadline" and calls == ["first"]
    queries, state = x.query_checkpoint(source, saved)
    assert queries[0][1] == "second" and state["queries"][queries[1][0]]["cursor"]["since_id"] == "30"
    _, migrated = x.query_checkpoint({**source, "query": "only"}, {"since_id": "legacy", "committed_at": NOW})
    assert next(iter(migrated["queries"].values()))["cursor"]["since_id"] == "legacy"


def test_connection_test_cannot_write_landing_or_cursor_even_when_rate_limited(monkeypatch):
    def persist(*_):
        raise AssertionError("A connection test must not persist pages or checkpoints")
    monkeypatch.setattr(x, "fetch_page", lambda *_args, **_kwargs: ([], {"since_id": "30"}))
    result = x.poll_queries(None, "token", default_source("x"), {}, NOW, persist, persist, test=True)
    assert result["status"] == "tested" and "last_received_count" not in result
    def limited(*_args, **_kwargs):
        raise x.XFailure("rate_limited", NOW + 90)
    monkeypatch.setattr(x, "fetch_page", limited)
    with pytest.raises(x.XFailure, match="rate_limited"):
        x.poll_queries(None, "token", default_source("x"), {}, NOW, persist, persist, test=True)
