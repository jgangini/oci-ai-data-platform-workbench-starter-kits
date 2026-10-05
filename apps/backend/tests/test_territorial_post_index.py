import json
from types import SimpleNamespace

import pytest

from app.territorial import database


def test_post_projection_deduplicates_and_retries_only_concurrent_insert_conflicts():
    calls = []
    def callproc(name, values):
        calls.append((name, values))
        if len(calls) == 1:
            raise Exception(SimpleNamespace(code=1))
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callproc=callproc), commit=lambda: calls.append("commit"))
    post = {"platform": "x", "source_id": "stable", "created_at": "2026-10-03T12:00:00Z", "text": 'line one\n"line two"'}
    database.upsert_posts(connection, [post, post], "captured", batch_key="immutable.csv")
    assert calls[0] == calls[1] and calls[-1] == "commit"
    projected = json.loads(calls[0][1][0])
    assert len(projected) == 1 and projected[0]["id"] == "x:stable"
    assert projected[0]["text"] == post["text"] and projected[0]["batch_key"] == "immutable.csv"


def test_post_page_binds_cutoff_and_keeps_analysis_progress_outside_payload():
    calls = []
    document = {"items": [{"capture_seq": 9, "analysis_status": "processed", "payload": {"id": "x:1", "text": "original"}},
                          {"capture_seq": 8, "analysis_status": "captured", "payload": {"id": "x:2"}}],
                "max_seq": 12, "total": 5}
    def callfunc(name, _kind, values):
        calls.append((name, values))
        return json.dumps(document)
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=callfunc))
    result = database.query_posts(connection, "x", 1, before_seq=10, max_seq=12)
    assert calls == [("ADMIN.PRISMA_CONTROL.LIST_POSTS", ["x", 1, 10, 12])]
    assert result == {**document, "items": [{"id": "x:1", "text": "original", "capture_seq": 9, "analysis_status": "processed", "captured_at": None}], "next_seq": 9}
    for arguments in (("x' OR 1=1", 10, None, None), ("x", True, None, None), ("x", 101, None, None), ("x", 10, -1, 12)):
        with pytest.raises(ValueError):
            database.query_posts(connection, *arguments)
    assert len(calls) == 1
    document["items"] = document["items"][:1]
    assert database.query_posts(connection, "x", 1, before_seq=10, max_seq=12)["next_seq"] is None


def test_aggregate_post_page_keeps_durable_capture_timestamp_and_status():
    calls = []
    captured = "2026-10-03T12:05:00Z"
    document = {"items": [{"capture_seq": 12, "analysis_status": "processed", "captured_at": captured,
        "payload": {"id": "instagram:1", "platform": "instagram", "ingested_at": "2026-10-03T12:10:00Z"}},
        {"capture_seq": 10, "analysis_status": "ingested", "captured_at": captured,
        "payload": {"id": "x:1", "platform": "x"}}], "max_seq": 12, "total": 2}
    def callfunc(name, _kind, values):
        calls.append((name, values))
        return json.dumps(document)
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callfunc=callfunc))
    result = database.query_posts(connection, None, 100)
    assert calls == [("ADMIN.PRISMA_CONTROL.LIST_POSTS", [None, 100, None, None])]
    assert [item["id"] for item in result["items"]] == ["instagram:1", "x:1"]
    assert [item["analysis_status"] for item in result["items"]] == ["processed", "ingested"]
    assert all(item["captured_at"] == captured for item in result["items"])
    assert result["items"][0]["ingested_at"] != captured and result["next_seq"] is None


def test_post_projection_distinguishes_capture_from_publication_and_ingestion():
    calls = []
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(callproc=lambda name, values: calls.append(values)), commit=lambda: None)
    publication, capture, ingestion = "2026-10-03T11:00:00Z", "2026-10-03T12:00:00Z", "2026-10-03T12:05:00Z"
    post = {"platform": "facebook", "source_id": "stable", "mode": "simulation", "created_at": publication}
    database.upsert_posts(connection, [post], "captured", ingested_at=capture)
    assert json.loads(calls[-1][0])[0]["captured_at"] == capture
    assert json.loads(calls[-1][0])[0]["mode"] == "Synthetic"
    database.upsert_posts(connection, [post], "ingested", ingested_at=ingestion)
    assert "captured_at" not in json.loads(calls[-1][0])[0]
    database.upsert_posts(connection, [{**post, "platform": "x", "mode": "real", "observed_at": capture}], "ingested", ingested_at=ingestion)
    projected = json.loads(calls[-1][0])[0]
    assert projected["captured_at"] == capture and projected["ingested_at"] == ingestion
    assert projected["created_at"] == publication
    assert projected["mode"] == "real" and post["mode"] == "simulation"


def test_schema_upgrade_tolerates_existing_capture_column_but_not_other_errors():
    calls = []
    def execute(statement):
        calls.append(statement)
        if statement.startswith("ALTER TABLE"):
            raise Exception(SimpleNamespace(code=1430))
    connection = SimpleNamespace(cursor=lambda: SimpleNamespace(execute=execute, fetchone=lambda: [0], fetchall=lambda: []), commit=lambda: calls.append("commit"))
    database.install_schema(connection)
    assert calls[-1] == "commit" and database.PACKAGE_BODY in calls
    def denied(statement):
        raise Exception(SimpleNamespace(code=1031))
    connection.cursor = lambda: SimpleNamespace(execute=denied)
    with pytest.raises(Exception):
        database.install_schema(connection)
