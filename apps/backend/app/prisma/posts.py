"""Administrative post projection and bounded, tamper-evident keyset cursors."""
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from .corpus import presentation

from .core import SYNTHETIC_MODES, canonical_mode


def _query_key(q):
    normalized = q.strip().casefold()
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


def _published_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds") if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _valid_position(position, sort, order, maximum):
    if not isinstance(position, dict) or set(position) != {"key", "digest"} or not re.fullmatch(r"[a-f0-9]{64}", str(position["digest"])):
        return False
    anchor = position["key"]
    if not isinstance(anchor, list) or len(anchor) != 2 or not isinstance(anchor[1], str) or not 1 <= len(anchor[1]) <= 200:
        return False
    return _valid_anchor(anchor[0], sort, order, maximum)


def _valid_anchor(value, sort, order, maximum):
    if sort == "captured_at":
        return type(value) is int and 1 <= value <= maximum
    return isinstance(value, str) and (value == ("" if order == "desc" else "\uffff") or _published_time(value) == value)


def cursor_values(value, platform, key, now=None, *, q="", sort="captured_at", order="desc"):
    if not value:
        return None, None
    try:
        if len(value) > 2048:
            raise ValueError()
        token, signature = value.split(".")
        expected = hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if (not isinstance(data, dict) or data.get("platform") != platform or data.get("v") not in (1, 2)
                or data.get("query", "") != _query_key(q)
                or data.get("sort", "captured_at") != sort or data.get("order", "desc") != order
                or type(data.get("expires")) is not int
                or data["expires"] <= (time.time() if now is None else now)
                or type(data.get("maximum")) is not int or data["maximum"] < 0):
            raise ValueError()
        if data["v"] == 1:
            if sort != "captured_at" or order != "desc" or type(data.get("before")) is not int or not 1 <= data["before"] <= data["maximum"] + 1:
                raise ValueError()
        elif not _valid_position(data.get("before"), sort, order, data["maximum"]):
            raise ValueError()
        return data["before"], data["maximum"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "This publication list cursor has expired or is invalid. Reload latest.") from None


def page_result(page, platform, key, now=None, *, q="", sort="captured_at", order="desc"):
    cursor = None
    if page.get("next_seq") is not None:
        data = {"v": 2 if page.get("sort_digest") else 1, "platform": platform, "query": _query_key(q),
                "sort": sort, "order": order, "maximum": page["max_seq"], "before": page["next_seq"],
                "expires": int(time.time() if now is None else now) + 86400}
        token = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).decode().rstrip("=")
        cursor = token + "." + hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
    return {"items": [post_view(item) for item in page["items"]], "next_cursor": cursor,
            "total": page["total"], "version": f"posts-v2-{page['max_seq']}-{page['sort_digest']}" if page.get("sort_digest") else f"posts-v1-{page['max_seq']}"}


async def _native_pages(read_page, maximum):
    before = None
    while True:
        page = await read_page(100, before, maximum)
        maximum = page["max_seq"]
        yield page
        following = page["next_seq"]
        if following is None:
            return
        if before is not None and following >= before:
            raise RuntimeError("Publication pagination did not advance")
        before = following


def _matching_posts(items, query):
    for item in items:
        view = post_view(item)
        if not query or any(query in str(view[name] or "").casefold()
                            for name in ("text", "username", "display_name", "platform", "country")):
            yield item, view


def _past_anchor(key, anchor, order="desc"):
    return anchor is None or (key < anchor if order == "desc" else key > anchor)


def _order_key(item, view, sort, order):
    if sort == "captured_at":
        return item["capture_seq"], view["id"]
    date = _published_time(view["published_at"]) or ("" if order == "desc" else "\uffff")
    return date, view["id"]


async def search_page(read_page, limit, before_seq=None, max_seq=None, *, q="", sort="captured_at", order="desc"):
    if sort not in {"captured_at", "published_at"} or order not in {"asc", "desc"}:
        raise ValueError("Unsupported publication order")
    if sort != "captured_at" or order != "desc":
        return await _ordered_page(read_page, limit, before_seq, max_seq, q=q, sort=sort, order=order)
    query = q.strip().casefold()
    if not query:
        return await read_page(limit, before_seq, max_seq)
    # ponytail: O(n) scans preserve hydrated names and Unicode parity across SQLite/Oracle.
    # At higher volume, replace with a normalized search index; retain this cutoff/cursor contract.
    items, total = [], 0
    async for page in _native_pages(read_page, max_seq):
        max_seq = page["max_seq"]
        for item, _ in _matching_posts(page["items"], query):
            total += 1
            if len(items) <= limit and _past_anchor(item["capture_seq"], before_seq):
                items.append(item)
    return {"items": items[:limit], "max_seq": max_seq, "total": total,
            "next_seq": items[limit - 1]["capture_seq"] if len(items) > limit else None}


async def _ordered_page(read_page, limit, position, maximum, *, q, sort, order):
    # ponytail: scan existing SQLite/Oracle pages, retaining at most limit+101 candidates.
    # Move this shared Unicode search/sort to native indexes when the demo outgrows an O(n) scan.
    items, total = [], 0
    digest, query = hashlib.sha256(), q.strip().casefold()
    anchor = tuple(position["key"]) if position else None
    async for page in _native_pages(read_page, maximum):
        maximum = page["max_seq"]
        for item, view in _matching_posts(page["items"], query):
            key = _order_key(item, view, sort, order)
            total += 1
            digest.update(json.dumps(key, ensure_ascii=False).encode() + b"\n")
            if _past_anchor(key, anchor, order):
                items.append((key, item))
        items = sorted(items, key=lambda pair: pair[0], reverse=order == "desc")[:limit + 1]
    fingerprint = digest.hexdigest()
    if position and position["digest"] != fingerprint:
        raise HTTPException(409, "This publication list changed while paging. Reload latest.")
    return {"items": [item for _, item in items[:limit]], "total": total, "max_seq": maximum, "sort_digest": fingerprint,
            "next_seq": {"key": list(items[limit - 1][0]), "digest": fingerprint} if len(items) > limit else None}


def original_url(value, simulated):
    if simulated or not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        uri = urlsplit(value)
        if (uri.scheme == "https" and not uri.username and not uri.password and uri.port in (None, 443)
                and uri.hostname in {"x.com", "twitter.com", "www.facebook.com", "www.instagram.com", "www.tiktok.com"}):
            return value
    except ValueError:
        pass
    return ""


def attachments_for(payload):
    """Expose only authenticated corpus objects or validated platform media."""
    simulated = payload.get("mode") in SYNTHETIC_MODES
    attachments = []
    for item in payload.get("attachments", [])[:8]:
        path = str(item.get("dataset_path", ""))
        match = re.fullmatch(r"posts/(post-\d{4})/media/(image-\d{2}\.(?:svg|webp|png|jpe?g))", path)
        shared = re.fullmatch(r"media/[a-z0-9_-]+\.(png|jpe?g|webp)", path)
        if shared:
            match = re.fullmatch(r"(post-\d{4})-(image-\d{2})", str(item.get("id", "")))
        if simulated and match and re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
            filename = match[2] + "." + shared[1] if shared else match[2]
            attachments.append({**{name: item.get(name) for name in ("id", "type", "mime_type", "alt_text", "origin")},
                "url": f"/api/admin/prisma/media/{match[1]}/{filename}", "provenance": "synthetic"})
    if not simulated:
        from .media import photos
        attachments.extend({**item, "id": str(index), "type": "image", "mime_type": "image/jpeg"}
                           for index, item in enumerate(photos(payload.get("platform"), payload.get("media", []))))
    return attachments


def post_view(record):
    payload = record.get("payload", record)
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = {**payload, **presentation(payload)}
    meta = payload.get("raw_metadata", {})
    simulated = payload.get("mode") in SYNTHETIC_MODES
    published_at = payload.get("created_at")
    if _published_time(published_at) is None:
        published_at = None
    return {"id": payload.get("id") or record.get("post_key"), "platform": payload.get("platform"),
        "username": payload.get("username") or meta.get("username") or "Unknown",
        "display_name": payload.get("display_name") or meta.get("display_name") or "Unknown",
        "country": payload.get("country") or "Unknown", "city": payload.get("city") or "Unknown",
        "locality": payload.get("locality") or "Unknown", "location_method": payload.get("location_method", "unresolved"),
        "text": str(payload.get("text", ""))[:12000], "published_at": published_at,
        "captured_at": record.get("captured_at") or payload.get("captured_at"),
        "ingested_at": record.get("ingested_at") or payload.get("ingested_at"),
        "processing_status": record.get("analysis_status", payload.get("analysis_status", "captured")),
        "mode": canonical_mode(payload.get("mode")), "url": original_url(payload.get("source_uri"), simulated), "attachments": attachments_for(payload)}
