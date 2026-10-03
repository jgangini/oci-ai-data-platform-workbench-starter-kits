"""Small X API adapter; a caller persists each page before committing its cursor.

Adapted from jgangini/oracle-ai-data-platform-workbench, notebook cell 3.
Original copyright (c) 2026 Joel Gangini Garcia, MIT; see THIRD_PARTY below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .core import utc_text


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
              "sort_order": "recency", "post.fields": "created_at,text,lang,geo,entities",
              "expansions": "author_id", "user.fields": "username,name"}
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
        events = [_post_event(post, now) for post in posts]
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise XFailure("invalid_response", now + 60) from exc
    if not cursor.get("pending_newest_id") and meta.get("newest_id"):
        cursor["pending_newest_id"] = str(meta["newest_id"])
    cursor["next_token"] = meta.get("next_token")
    if not cursor["next_token"]:
        cursor = {"since_id": cursor.get("pending_newest_id") or cursor.get("since_id"), "committed_at": now}
    return events, cursor


def _post_event(post: dict, now: float) -> dict:
    source_id = str(post["id"])
    if not source_id.isdigit():
        raise ValueError("Invalid post id")
    return {"platform": "x", "source_id": source_id, "mode": "real", "text": post["text"],
            "created_at": post["created_at"], "observed_at": utc_text(now),
            "source_uri": f"https://x.com/i/web/status/{source_id}",
            "raw_metadata": {"author_id": post.get("author_id"), "lang": post.get("lang"),
                             "geo": post.get("geo"), "entities": post.get("entities"), "source_post": post}}
