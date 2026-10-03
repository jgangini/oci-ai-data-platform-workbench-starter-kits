"""Small X API adapter; a caller persists each page before committing its cursor.

Adapted from jgangini/oracle-ai-data-platform-workbench, notebook cell 3.
Original copyright (c) 2026 Joel Gangini Garcia, MIT; see THIRD_PARTY below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import time

from .capture import query_lines
from .core import utc_text
from .media import photos


THIRD_PARTY = """Copyright (c) 2026 Joel Gangini Garcia
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


@dataclass
class XFailure(Exception):
    code: str
    retry_at: float | None = None


def retry_time(headers: dict, now: float) -> float:
    try:
        return max(now + 5, float(headers.get("x-rate-limit-reset", now + 60)))
    except (ValueError, TypeError):
        return now + 60


def fetch_page(client, token: str, query: str, checkpoint: dict, now: float, *, page_size: int = 100) -> tuple[list[dict], dict]:
    cursor = dict(checkpoint)
    previous = cursor.get("committed_at")
    if cursor.get("end_time"):
        previous = datetime.fromisoformat(cursor["end_time"].replace("Z", "+00:00")).timestamp()
    if previous and now - previous > 7 * 86400:
        raise XFailure("history_gap")
    cursor.setdefault("end_time", utc_text(now - 30))
    params = {"query": query, "max_results": page_size, "end_time": cursor["end_time"],
              "sort_order": "recency", "post.fields": "created_at,text,lang,geo,entities,attachments",
              "expansions": "author_id,attachments.media_keys", "user.fields": "username,name",
              "media.fields": "media_key,type,url,alt_text"}
    if cursor.get("since_id"):
        params["since_id"] = cursor["since_id"]
    else:
        cursor.setdefault("start_time", utc_text(now - 86400))
        params["start_time"] = cursor["start_time"]
    if cursor.get("next_token"):
        params["next_token"] = cursor["next_token"]
    response = client.get("https://api.x.com/2/tweets/search/recent", params=params,
                          headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if response.status_code == 429:
        raise XFailure("rate_limited", retry_time(response.headers, now))
    if response.status_code != 200:
        code = {401: "invalid_credential", 402: "credits_exhausted", 403: "access_denied"}.get(response.status_code, "upstream_error")
        raise XFailure(code, now + 60 if response.status_code >= 500 else None)
    try:
        payload = response.json()
        posts = payload.get("data", [])
        meta = payload.get("meta", {})
        if "meta" not in payload or not isinstance(posts, list) or not isinstance(meta, dict) or (payload.get("errors") and not posts):
            raise ValueError("Invalid page")
        media = {item["media_key"]: item for item in payload.get("includes", {}).get("media", [])}
        users = {str(item["id"]): item for item in payload.get("includes", {}).get("users", [])}
        events = [_post_event(post, now, media, users) for post in posts]
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise XFailure("invalid_response", now + 60) from exc
    if not cursor.get("pending_newest_id") and meta.get("newest_id"):
        cursor["pending_newest_id"] = str(meta["newest_id"])
    cursor["next_token"] = meta.get("next_token")
    if not cursor["next_token"]:
        cursor = {"since_id": cursor.get("pending_newest_id") or cursor.get("since_id"), "committed_at": now}
    return events, cursor


def _post_event(post: dict, now: float, media=None, users=None) -> dict:
    source_id = str(post["id"])
    if not source_id.isdigit():
        raise ValueError("Invalid post id")
    author = (users or {}).get(str(post.get("author_id")), {})
    return {"platform": "x", "source_id": source_id, "mode": "real", "text": post["text"],
            "username": str(author.get("username", ""))[:100], "display_name": str(author.get("name", ""))[:200],
            "created_at": post["created_at"], "observed_at": utc_text(now),
            "source_uri": f"https://x.com/i/web/status/{source_id}",
            "media": photos("x", [(media or {}).get(key, {}) for key in post.get("attachments", {}).get("media_keys", [])]),
            "raw_metadata": {"author_id": post.get("author_id"), "lang": post.get("lang"),
                             "geo": post.get("geo"), "entities": post.get("entities"), "source_post": post}}


def query_checkpoint(source, saved):
    queries = [(hashlib.sha256(query.encode()).hexdigest(), query) for query in query_lines(source["query"])]
    existing = saved.get("queries", {})
    state = {"query_version": source.get("query_version"), "queries": {
        key: existing.get(key, {"query": query, "cursor": {}}) for key, query in queries},
        "resume_query": saved.get("resume_query"), "retry_at": 0}
    if not existing and len(queries) == 1 and saved.get("query_version") == source.get("query_version"):
        legacy = saved.get("cursor", saved if any(key in saved for key in ("since_id", "next_token", "end_time")) else {})
        state["queries"][queries[0][0]]["cursor"] = legacy
    start = next((index for index, (key, _) in enumerate(queries) if key == state["resume_query"]), 0)
    return queries[start:] + queries[:start], state


def poll_queries(client, token, source, saved, now, on_page, on_checkpoint, *, test=False, clock=time.monotonic):
    """At most two pages per search and 60 seconds between calls; each search owns its durable cursor."""
    if saved.get("retry_at", 0) > now:
        raise XFailure("rate_limited", saved["retry_at"])
    if test:
        # Connection tests use the same reads but cannot write Landing or advance any source cursor.
        on_page = lambda *_: None
        on_checkpoint = lambda *_: None
    queries, state = query_checkpoint(source, {} if test else saved)
    deadline, received, backlog = clock() + 60, set(), False
    for key, query in queries:
        state["resume_query"] = key
        for _ in range(1 if test else 2):
            if clock() >= deadline:
                on_checkpoint(state)
                return _poll_status("backlog", source, now, received, test, "capture_deadline")
            try:
                events, cursor = fetch_page(client, token, query, state["queries"][key]["cursor"], now, page_size=10 if test else 50)
            except XFailure as exc:
                state["retry_at"] = exc.retry_at or 0
                state["queries"][key].update(last_error=exc.code, retry_at=exc.retry_at)
                on_checkpoint(state)
                raise  # In particular, a 429 stops the remaining searches rather than hammering one shared quota.
            state["queries"][key] = {"query": query, "cursor": cursor, "last_error": None, "retry_at": 0}
            for event in events:
                event["raw_metadata"] = {**event.get("raw_metadata", {}), "search_query": query}
                received.add(f"{event['platform']}:{event['source_id']}")
            on_page(events, state)
            if not cursor.get("next_token"):
                break
        backlog = backlog or bool(cursor.get("next_token"))
    state["resume_query"] = None
    on_checkpoint(state)
    return _poll_status("backlog" if backlog else "ready", source, now, received, test)


def _poll_status(status, source, now, received, test, error=None):
    if test and error is None:
        status = "tested"
    return {"status": status, "last_error": error,
            "next_due": utc_text(now + (60 if status == "backlog" else source["interval_minutes"] * 60)),
            **({"last_received_count": len(received)} if not test else {})}
