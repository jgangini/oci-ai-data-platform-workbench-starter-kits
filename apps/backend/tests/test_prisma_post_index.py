import json
from types import SimpleNamespace

import pytest

from app.prisma import database


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
    assert result == {**document, "items": [{"id": "x:1", "text": "original", "capture_seq": 9, "analysis_status": "processed"}], "next_seq": 9}
    for arguments in (("x' OR 1=1", 10, None, None), ("x", True, None, None), ("x", 101, None, None), ("x", 10, -1, 12)):
        with pytest.raises(ValueError):
            database.query_posts(connection, *arguments)
    assert len(calls) == 1
    document["items"] = document["items"][:1]
    assert database.query_posts(connection, "x", 1, before_seq=10, max_seq=12)["next_seq"] is None
