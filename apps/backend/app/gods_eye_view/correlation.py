"""Stable event membership and report activity, independent of manual review and severity."""
from datetime import datetime
from bisect import bisect_left, bisect_right
from collections import defaultdict
import hashlib
import math

from .core import PLATFORMS, SYNTHETIC_MODES, canonical_mode, folded, image_hashes


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
    return distance_km(left, right) <= 2


def distance_km(left, right):
    lat1, lat2 = math.radians(left["lat"]), math.radians(right["lat"])
    delta = math.radians(left["lon"] - right["lon"])
    distance = math.sin((lat1 - lat2) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, max(0, distance))))


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
        if identity(members[0]) != identity(event) or not nearby(members[0], event) or not same_jurisdiction(jurisdiction([event]), jurisdiction(members)):
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
    seen, images = {}, {}
    result = []
    for item in sorted(evidence, key=lambda record: (record["created_at"], record["id"])):
        mode = canonical_mode(item["mode"])
        content = (mode, item["content_hash"])
        hashes = {(mode, digest) for digest in image_hashes(item)}
        copied_text = seen.get(content)
        copied_image = next((images[digest] for digest in sorted(hashes)
                             if digest in images and images[digest] != item["id"]), None)
        original = copied_text if copied_text and copied_text != item["id"] else copied_image
        duplicate = original is not None
        claim = item.get("claim") or {}
        result.append({"event_id": incident_id, "post_key": item["id"],
            "relation": "duplicate" if duplicate else claim.get("relation", "unclassified"),
            "claim_relation": claim.get("relation", "unclassified"),
            "duplicate_of": original,
            "explanation": ("Exact image SHA-256 matches an earlier report; caption claims remain separate" if original != copied_text else
                "Exact normalized content matches an earlier report") if duplicate else claim.get("evidence_text", "Linked by category, locality and time; claim not verified"),
            "analysis_version": item.get("prompt_version", "gods-eye-view-correlation-v1")})
        seen.setdefault(content, item["id"])
        for digest in hashes:
            images.setdefault(digest, item["id"])
    return result


def jurisdiction(evidence):
    return tuple(frozenset(folded(item.get(key) or "").strip() for item in evidence)
                 - {"", "unknown", "desconocido"} for key in ("country", "city"))


def same_jurisdiction(left, right):
    # Conflicting known jurisdictions cannot establish a geographic association.
    return all(len(a) <= 1 and len(b) <= 1 and (not a or not b or a == b) for a, b in zip(left, right))


def social_context(evidence):
    social = [item for item in evidence if item["platform"] in PLATFORMS]
    posts = {item["id"]: item for item in social}
    return {"posts": len(posts),
        **{stance: len({item["id"] for item in social if (item.get("claim") or {}).get("relation", "unclassified") == stance})
           for stance in ("supports", "contradicts", "unclassified")},
        "exact_copies": sum(item["relation"] == "duplicate" for item in relations("", posts.values())),
        "accounts_per_platform": {platform: len({str(item["raw_metadata"]["author_id"]) for item in posts.values()
            if item["platform"] == platform and item["raw_metadata"].get("author_id")}) for platform in PLATFORMS},
        "unknown_account_posts": sum(not item["raw_metadata"].get("author_id") for item in posts.values())}


def sensor_context(incident, candidates, place, now):
    result = {"status": "insufficient_location_precision", "count": 0, "samples": []}
    if any(len(part) > 1 for part in place):
        return {**result, "status": "insufficient_jurisdiction"}
    if incident.get("location_method") not in {"coordinates", "human_review"} or not all(
            type(incident.get(key)) in (int, float) and math.isfinite(incident[key]) and abs(incident[key]) <= limit
            for key, limit in (("lat", 90), ("lon", 180))):
        return result
    matches = []
    observed = timestamp(incident["last_observed_at"])
    if observed > now:
        return {**result, "status": "not_observed"}
    for sensor in candidates:
        delta = timestamp(sensor["observed_at"]) - observed
        if abs(delta) > 86400 or timestamp(sensor["observed_at"]) > now or not same_jurisdiction(
                place, jurisdiction([{**sensor, "city": sensor.get("municipality", "")}])):
            continue
        distance = distance_km(incident, sensor)
        if distance <= 2:
            matches.append((distance, abs(delta), sensor["id"], delta))
    result.update(status="observed" if matches else "not_observed", count=len(matches),
        samples=[{"id": key, "distance_km": round(distance, 3), "time_delta_minutes": round(delta / 60, 2)}
                 for distance, _gap, key, delta in sorted(matches)[:5]])
    return result


def sensor_index(sensors):
    readings = defaultdict(list)
    for sensor in sensors:
        mode = canonical_mode(sensor["mode"])
        if mode in {"Synthetic", "real"} and sensor.get("is_simulated") is (mode == "Synthetic"):
            readings[(mode, sensor["locality"])].append(sensor)
    return readings


def add_context(incidents, groups, sensors, now):
    """Publication-local associations; never change membership, confidence or human review."""
    places = {key: jurisdiction(evidence) for key, evidence in groups.items()}
    histories, readings = defaultdict(list), sensor_index(sensors)
    latest = max((timestamp(item["last_observed_at"]) for item in incidents), default=0) if now is None else now
    for incident in incidents:
        key = (incident["mode"], incident["locality"], incident["category"])
        observed = timestamp(incident["last_observed_at"])
        if incident["locality"] != "Sin localizar" and observed <= latest and any(item["platform"] in PLATFORMS for item in groups[incident["id"]]):
            histories[key].append((observed, incident["id"]))
    for values in histories.values():
        values.sort()
    for incident in incidents:
        key = (incident["mode"], incident["locality"], incident["category"])
        observed = timestamp(incident["last_observed_at"])
        candidates = histories[key]
        start, end = bisect_left(candidates, (observed - 86400, "")), bisect_right(candidates, (observed + 86400, chr(0x10ffff)))
        # ponytail: locality/time buckets bound the current publication; larger histories need a temporal SQL index.
        related = sorted((abs(at - observed), other) for at, other in candidates[start:end]
                         if other != incident["id"] and same_jurisdiction(places[incident["id"]], places[other])) if observed <= latest else []
        incident["correlation_context"] = {"rule_version": "publication_context_v1",
            "jurisdiction_status": "conflicting" if any(len(part) > 1 for part in places[incident["id"]]) else
                                   "known" if all(places[incident["id"]]) else "partial",
            "criteria": {"window_minutes": 1440, "sensor_radius_km": 2, "time_reference": "incident_last_observed_at",
                "jurisdiction": "match_when_known", "association_only": True, "scope": "current_publication",
                "historical_match": "mode_category_locality", "sensor_match": "mode_locality_precise_coordinates",
                "sensor_future_readings": "excluded",
                "social_counts_include_copies": True, "accounts_are_not_people": True},
            "social": social_context(groups[incident["id"]]),
            "historical_related": {"count": len(related), "incident_ids": [key for _gap, key in related[:5]]},
            "sensors": sensor_context(incident, readings[(incident["mode"], incident["locality"])], places[incident["id"]], latest)}
