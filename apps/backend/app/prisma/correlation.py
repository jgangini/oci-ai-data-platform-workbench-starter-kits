"""Stable event membership and report activity, independent of manual review and severity."""
from datetime import datetime
import hashlib
import math

from .core import SYNTHETIC_MODES


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def identity(event):
    meta = event.get("raw_metadata", {})
    scenario = meta.get("scenario_run_id", "") if event["mode"] in SYNTHETIC_MODES and not meta.get("capture_run_id") else ""
    # Identity keeps its original token so existing reviews and memberships survive.
    mode = "simulation" if event["mode"] in SYNTHETIC_MODES else event["mode"]
    return mode, event["category"], event["locality"], scenario


def nearby(left, right):
    # Locality anchors express an area, never a measured distance between incidents.
    if left.get("location_method") != "coordinates" or right.get("location_method") != "coordinates":
        return True
    lat1, lat2 = math.radians(left["lat"]), math.radians(right["lat"])
    delta = math.radians(left["lon"] - right["lon"])
    distance = math.sin((lat1 - lat2) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, max(0, distance)))) <= 2


def expanded_claims(events):
    eligible = {}
    for event in events:
        claims = event.get("claims")
        for index, claim in enumerate(claims if claims is not None else [None]):
            item = {**event, **({key: claim[key] for key in ("category", "locality", "severity", "confidence", "lat", "lon", "location_method") if key in claim} if claim else {}), "claim": claim}
            if item["category"] != "por_clasificar":
                eligible[(item["id"], index)] = item
    return eligible


def retained_memberships(eligible, previous):
    groups, assigned = {}, set()
    for incident in previous or []:
        keys = [key for key, item in eligible.items() if item["id"] in incident.get("evidence_ids", []) and key not in assigned
                and item["category"] == incident["category"] and item["locality"] == incident["locality"]]
        members = [eligible[key] for key in keys]
        if members:
            groups[incident["id"]] = members
            assigned.update(keys)
    return groups, assigned


def nearest_group(event, groups, window):
    candidates = []
    if event["locality"] == "Sin localizar":
        return None
    for key, members in groups.items():
        if identity(members[0]) != identity(event) or not nearby(members[0], event):
            continue
        gap = min(abs(timestamp(event["created_at"]) - timestamp(item["created_at"])) for item in members)
        if gap <= window:
            candidates.append((gap, key))
    return min(candidates)[1] if candidates else None


def group_events(events, rules, previous):
    eligible = expanded_claims(events)
    groups, assigned = retained_memberships(eligible, previous)
    for member_key, event in sorted(eligible.items(), key=lambda pair: (pair[1]["created_at"], pair[0])):
        if member_key in assigned:
            continue
        window = rules.get(event["platform"], {}).get("correlation_window_minutes", 30) * 60
        # ponytail: bounded 5,000-row demo uses an in-memory temporal scan; index space/time for larger publications.
        key = nearest_group(event, groups, window) or "incident-" + hashlib.sha256(("|".join(identity(event)) + "|" + event["id"]).encode()).hexdigest()[:16]
        groups.setdefault(key, []).append(event)
        assigned.add(member_key)
    return groups


def activity(evidence, rules, now):
    latest = max(timestamp(item["created_at"]) for item in evidence) if now is None else now
    counts, levels = {}, {}
    for platform in sorted({item["platform"] for item in evidence}):
        rule = rules.get(platform, {})
        cutoff = latest - rule.get("correlation_window_minutes", 30) * 60
        originals = {item["content_hash"] for item in evidence if item["platform"] == platform and cutoff <= timestamp(item["created_at"]) <= latest}
        counts[platform] = len(originals)
        thresholds = rule.get("report_thresholds", {"low": 5, "medium": 10, "high": 20})
        levels[platform] = next((level for level in ("high", "medium", "low") if len(originals) >= thresholds[level]), "below_threshold")
    order = {"below_threshold": 0, "low": 1, "medium": 2, "high": 3}
    return {"report_counts": counts, "report_activity_by_platform": levels,
        "report_activity": max(levels.values(), key=order.get),
        "activity_method": "distinct_content_per_platform_window_v1",
        "rule_versions": {name: rules.get(name, {}).get("config_version", 1) for name in counts}}


def relations(incident_id, evidence):
    seen = {}
    result = []
    for item in sorted(evidence, key=lambda record: (record["created_at"], record["id"])):
        duplicate = item["content_hash"] in seen and seen[item["content_hash"]] != item["id"]
        claim = item.get("claim") or {}
        result.append({"event_id": incident_id, "post_key": item["id"],
            "relation": "duplicate" if duplicate else claim.get("relation", "unclassified"),
            "explanation": "Exact normalized content matches an earlier report" if duplicate else claim.get("evidence_text", "Linked by category, locality and time; claim not verified"),
            "analysis_version": item.get("prompt_version", "territorial-correlation-v1")})
        seen[item["content_hash"]] = item["id"]
    return result
