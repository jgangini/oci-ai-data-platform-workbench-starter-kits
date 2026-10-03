"""Administrative post projection and bounded, tamper-evident keyset cursors."""
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException


def cursor_values(value, platform, key, now=None):
    if not value:
        return None, None
    try:
        token, signature = value.split(".")
        expected = hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if (data.get("platform") != platform or data.get("v") != 1
                or data["expires"] <= (time.time() if now is None else now)
                or any(type(data[name]) is not int or data[name] < minimum for name, minimum in (("maximum", 0), ("before", 1)))
                or data["before"] > data["maximum"] + 1):
            raise ValueError()
        return data["before"], data["maximum"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "This publication list cursor has expired or is invalid. Reload latest.") from None


def page_result(page, platform, key, now=None):
    cursor = None
    if page.get("next_seq") is not None:
        data = {"v": 1, "platform": platform, "maximum": page["max_seq"], "before": page["next_seq"],
                "expires": int(time.time() if now is None else now) + 86400}
        token = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).decode().rstrip("=")
        cursor = token + "." + hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
    return {"items": [post_view(item) for item in page["items"]], "next_cursor": cursor,
            "total": page["total"], "version": f"posts-v1-{page['max_seq']}"}


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
    simulated = payload.get("mode") == "simulation"
    attachments = []
    for item in payload.get("attachments", [])[:8]:
        path = str(item.get("dataset_path", ""))
        match = re.fullmatch(r"posts/(post-\d{4})/media/([A-Za-z0-9_-]+\.(?:svg|webp|png|jpg|mp4))", path)
        if simulated and match and re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
            attachments.append({**{name: item.get(name) for name in ("id", "type", "mime_type", "alt_text")},
                "url": f"/api/admin/prisma/media/{match[1]}/{match[2]}", "provenance": "synthetic"})
    if not simulated:
        from .media import photos
        attachments.extend({**item, "id": str(index), "type": "image", "mime_type": "image/jpeg"}
                           for index, item in enumerate(photos(payload.get("platform"), payload.get("media", []))))
    return attachments


def post_view(record):
    payload = record.get("payload", record)
    if isinstance(payload, str):
        payload = json.loads(payload)
    meta = payload.get("raw_metadata", {})
    simulated = payload.get("mode") == "simulation"
    return {"id": payload.get("id") or record.get("post_key"), "platform": payload.get("platform"),
        "username": payload.get("username") or meta.get("username") or "Unknown",
        "display_name": payload.get("display_name") or meta.get("display_name") or "Unknown",
        "country": payload.get("country") or "Unknown", "city": payload.get("city") or "Unknown",
        "locality": payload.get("locality") or "Unknown", "location_method": payload.get("location_method", "unresolved"),
        "text": str(payload.get("text", ""))[:12000], "published_at": payload.get("created_at"),
        "ingested_at": record.get("ingested_at") or payload.get("ingested_at"),
        "processing_status": record.get("analysis_status", payload.get("analysis_status", "captured")),
        "mode": payload.get("mode"), "url": original_url(payload.get("source_uri"), simulated), "attachments": attachments_for(payload)}
