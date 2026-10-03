"""Pure, deterministic transformations; no credentials, network or database access."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from .media import photos


PLATFORMS = ("x", "facebook", "instagram", "tiktok")
SOURCE_FIELDS = {"enabled", "mode", "query", "interval_minutes", "secret_ref", "credential_configured", "capture_running",
                 "status", "last_run_at", "next_due", "last_error", "last_received_count", "capture_paused",
                 "correlation_window_minutes", "report_thresholds", "config_version"}
# Representative anchors, not incident coordinates or mathematical centroids. All six
# verified inside SDP/IDECA locality polygons on 2026-10-02 (EPSG:4326 point intersects).
# https://www.ideca.gov.co/recursos/mapas/localidad-bogota-dc
# Official CAR mirror: https://sig.car.gov.co/arcgis/rest/services/visor/Division_Territorial/MapServer/5
LOCALITIES = {
    "Suba": (4.741, -74.084),
    "Chapinero": (4.649, -74.063),
    "Ciudad Bolívar": (4.506, -74.148),
    "Usaquén": (4.695, -74.031),
    "Kennedy": (4.627, -74.155),
    "Bosa": (4.609, -74.184),
}
CATEGORIES = {
    "inundacion": ("inund", "aneg", "nivel del rio"),
    "incendio": ("incend", "humo", "fuego"),
    "movimiento_masa": ("desliz", "derrum", "ladera"),
    "infraestructura": ("poste", "cable", "arbol caido"),
    "lluvia": ("lluvia", "llov", "aguacero"),
}
SEVERITIES = {"low": 0, "medium": 1, "high": 2}
CATEGORY_NAMES = {"inundacion": "Flooding", "incendio": "Fire", "movimiento_masa": "Landslide",
                  "infraestructura": "Infrastructure damage", "lluvia": "Heavy rain", "por_clasificar": "Unclassified report"}


def default_source(platform: str) -> dict:
    query = "#bogota #inundacion\n#colombia #incendio\n#desastre"
    return {"platform": platform, "enabled": True, "capture_running": False, "mode": "simulation", "query": query,
            "interval_minutes": 5, "secret_ref": f"gods-eye-view-{platform}", "credential_configured": False,
            "correlation_window_minutes": 30, "report_thresholds": {"low": 5, "medium": 10, "high": 20}, "config_version": 1,
            "status": "simulation", "last_run_at": None, "next_due": None, "last_error": None, "last_received_count": None}


def aidp_credential_name(platform: str, reference: str) -> str:
    """Map the public source alias to an AIDP identifier; preserve valid legacy names."""
    if platform not in PLATFORMS or not isinstance(reference, str):
        raise ValueError("Invalid source credential reference")
    if reference == f"gods-eye-view-{platform}":
        return f"gods_eye_view_{platform}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", reference):
        raise ValueError("Invalid AIDP credential name")
    return reference


def source_migration(platform: str, source: dict) -> dict:
    """Upgrade unchanged defaults while keeping custom queries and live secret references."""
    defaults, changes = default_source(platform), {}
    previous_query = "(Bogotá OR Bogota OR #Bogota) (inundación OR inundacion OR incendio OR deslizamiento OR derrumbe OR lluvia) -is:retweet" if platform == "x" else ""
    if source.get("query") == previous_query:
        changes["query"] = defaults["query"]
    if not source.get("credential_configured") and source.get("secret_ref") in {f"prisma-{platform}", f"PrismaSource_{platform}"}:
        changes["secret_ref"] = defaults["secret_ref"]
    return changes


def utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def simulation_state(document, now):
    elapsed = float(document.get("elapsed_seconds", 0))
    if document.get("status") == "running":
        elapsed += max(0, now - float(document.get("started_at", now)))
    elapsed = min(600, elapsed)
    return {"status": "completed" if elapsed >= 600 else document.get("status", "idle"),
            "elapsed_seconds": elapsed, "duration_seconds": 600, "run_id": document.get("run_id"),
            "anchor_at": document.get("anchor_at"), "capture_complete": document.get("capture_complete", False)}


def folded(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _event_location(event: dict, normalized: str) -> tuple:
    locality = event.get("locality")
    # ponytail: lexical inference is only for our Bogotá simulation; real reports
    # require the classifier's jurisdiction decision, not a matching place name.
    if not locality and event["mode"] == "simulation":
        matches = [name for name in LOCALITIES if re.search(r"\b" + re.escape(folded(name)) + r"\b", normalized)]
        locality = matches[0] if len(matches) == 1 else None
    if locality not in LOCALITIES:
        return "Sin localizar", None, None, "unresolved"
    lat, lon = event.get("lat"), event.get("lon")
    method = event.get("location_method", "unresolved")
    if lat is None or lon is None or method in {"text_locality_centroid", "text_locality_anchor"}:
        return locality, *LOCALITIES[locality], "text_locality_anchor"
    return locality, lat, lon, method


def normalize_event(event: dict) -> dict:
    platform, source_id = str(event["platform"]), str(event["source_id"])
    if event.get("mode") not in {"simulation", "real"} or not source_id:
        raise ValueError("Each event needs an explicit mode and source identifier")
    simulated = event["mode"] == "simulation"
    if "is_simulated" in event and (type(event["is_simulated"]) is not bool or event["is_simulated"] != simulated):
        raise ValueError("Simulation provenance must agree with the event mode")
    text = str(event.get("text", ""))[:12000]
    normalized = folded(text)
    locality, lat, lon, method = _event_location(event, normalized)
    category = event.get("category") or next((name for name, words in CATEGORIES.items() if any(word in normalized for word in words)), "por_clasificar")
    timestamp = datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Event timestamps require a timezone")
    created_at = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    claims = []
    for claim in event.get("claims", []):
        claim_locality, claim_lat, claim_lon, claim_method = _event_location(
            {**event, "locality": claim["locality"], "lat": None, "lon": None}, folded(claim.get("evidence_text", "")))
        claims.append({**claim, "locality": claim_locality, "lat": claim_lat, "lon": claim_lon, "location_method": claim_method})
    return {
        "id": f"{platform}:{source_id}", "platform": platform, "source_id": source_id,
        "text": text, "created_at": created_at, "observed_at": event.get("observed_at", created_at),
        "source_uri": event.get("source_uri", ""), "mode": event["mode"], "is_simulated": simulated,
        "category": category, "locality": locality, "lat": lat, "lon": lon,
        "location_method": method, "severity": event.get("severity", "medium"),
        "classification_method": event.get("classification_method", "provided" if event.get("category") else "keyword_rules"),
        "confidence": float(event.get("confidence", 0.55)),
        "raw_metadata": event.get("raw_metadata", {}),
        "username": str(event.get("username", ""))[:100], "display_name": str(event.get("display_name", ""))[:200],
        "country": str(event.get("country", ""))[:100], "city": str(event.get("city", ""))[:100],
        "attachments": event.get("attachments", [])[:8],
        **{key: event[key] for key in ("ingested_at", "source_object", "source_hash", "source_hash_kind", "schema_version", "location_precision", "location_provenance", "model_version", "prompt_version") if key in event},
        **({"claims": claims} if "claims" in event else {}),
        "media": photos(platform, event.get("media", [])),
        "content_hash": hashlib.sha256(" ".join(normalized.split()).encode()).hexdigest(),
    }


def corroboration(evidence):
    sources, distinct = set(), []
    contradictions = set()
    for event in evidence:
        if (event.get("claim") or {}).get("relation") == "contradicts":
            contradictions.add(event["id"])
            continue
        source = (event["platform"], event["raw_metadata"].get("author_id") or "unknown")
        if source in sources:
            continue
        words = set(re.findall(r"\w+", folded(event["text"])))
        # ponytail: pairwise copy detection is bounded by the 5,000-event demo;
        # larger publications need an indexed similarity search before correlation.
        if any(words and len(words & prior) / len(words | prior) >= 0.8 for prior in distinct):
            continue
        distinct.append(words)
        # Missing author IDs never turn two posts on one platform into two witnesses.
        sources.add(source)
    count = len(sources)
    return {"independent_source_count": count, "contradicting_report_count": len(contradictions), "corroboration_score": min(100, max(0, count - 1) * 25),
            "corroboration_status": {0: "no_supporting_sources", 1: "single_source"}.get(count, "multiple_sources"),
            "corroboration_method": "independent_sources_text_similarity_v1"}


def legacy_groups(events):
    groups: dict[str, list[dict]] = {}
    for event in events:
        if event["category"] == "por_clasificar":
            continue
        # ponytail: hourly locality buckets can split boundary events; production needs sliding spatial/time clustering.
        metadata = event["raw_metadata"]
        # Continuous sources share locality/time correlation; bounded replays retain scenario isolation.
        scenario = metadata.get("scenario_run_id", "") if event["mode"] == "simulation" and not metadata.get("capture_run_id") else ""
        key = "|".join((event["mode"], event["category"], event["locality"], event["created_at"][:13], scenario))
        incident_id = "incident-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        groups.setdefault(incident_id, []).append(event)
    return groups


def incident_summary(first, evidence):
    location = first["locality"] if first["locality"] != "Sin localizar" else "an unresolved location"
    category = CATEGORY_NAMES.get(first["category"], "risk").lower()
    count = len({item["id"] for item in evidence})
    return (first.get("claim") or {}).get("summary_en") or f"{count} reports of {category} in {location}. Human review required."


def build_snapshot(events: list[dict], reviews: dict, version: str, published_at: str, *, rules=None, previous=None, now=None) -> dict:
    groups = legacy_groups(events)
    if rules is not None:
        from .correlation import group_events
        groups = group_events(events, rules, previous)
    incidents, event_posts = [], []
    for incident_id, evidence in sorted(groups.items()):
        first = min(evidence, key=lambda item: item["created_at"])
        review = reviews.get(incident_id, {})
        incidents.append({
            "id": incident_id, "title": f"{CATEGORY_NAMES.get(first['category'], 'Report')} · {first['locality'] if first['locality'] != 'Sin localizar' else 'Location unresolved'}",
            "summary": incident_summary(first, evidence),
            "created_at": first["created_at"], "last_observed_at": max(item["created_at"] for item in evidence),
            "category": first["category"], "locality": first["locality"],
            "severity": max((item["severity"] for item in evidence), key=lambda item: SEVERITIES.get(item, 0)),
            "confidence": max(item["confidence"] for item in evidence),
            "lat": first["lat"], "lon": first["lon"], "location_method": first["location_method"],
            "mode": first["mode"], "is_simulated": first["mode"] == "simulation", "evidence_ids": sorted({item["id"] for item in evidence}),
            "review_status": review.get("status", "pending"), "review_note": review.get("note", ""),
            "reviewed_evidence_ids": review.get("evidence_ids", []),
            **corroboration(evidence),
        })
        from .correlation import activity, relations
        event_posts.extend(relations(incident_id, evidence))
        if rules is not None:
            incidents[-1].update(activity(evidence, rules, now))
            incidents[-1]["correlation_windows_minutes"] = {name: rules.get(name, {}).get("correlation_window_minutes", 30) for name in incidents[-1]["report_counts"]}
        old = next((item for item in previous or [] if item["id"] == incident_id), {})
        changed = any(old.get(key) != value for key, value in incidents[-1].items())
        incidents[-1].update(revision=old.get("revision", 0) + int(changed),
            updated_at=utc_text(now) if changed and now is not None else old.get("updated_at", published_at))
    return {"version": version, "published_at": published_at, "incidents": incidents, "event_posts": event_posts,
            "evidence": sorted(events, key=lambda item: (item["created_at"], item["id"]))}


def simulation_events(elapsed_seconds: float, anchor_at: float | None = None) -> list[dict]:
    """Fixed relative scenario; a persisted run anchor keeps pause/resume timestamps stable."""
    timeline = (
        (0, "x", "lluvia-1", "Reporte simulado: lluvia intensa e inundación en Kennedy", "medium"),
        (0, "x", "lluvia-1", "Reporte simulado: lluvia intensa e inundación en Kennedy", "medium"),
        (45, "sensor", "nivel-1", "Sensor sintético: nivel del río elevado en Kennedy", "high"),
        (60, "facebook", "reenvio-1", "Reporte simulado: lluvia intensa e inundación en Kennedy", "medium"),
        (90, "facebook", "lluvia-2", "Reporte simulado: viviendas anegadas en Bosa", "high"),
        (150, "instagram", "humo-1", "Reporte simulado: humo e incendio en Chapinero", "medium"),
        (210, "sire", "humo-2", "Entrada SIRE simulada: incendio reportado en Chapinero", "high"),
        (270, "tiktok", "ladera-1", "Video simulado: deslizamiento en Ciudad Bolívar", "medium"),
        (330, "sensor", "ladera-2", "Sensor sintético: movimiento de ladera en Ciudad Bolívar", "high"),
        (390, "x", "ruido-1", "Publicación simulada: actividad cultural en Bogotá", "low"),
        (420, "x", "ubicacion-1", "Reporte simulado: inundación en Bogotá, ubicación sin confirmar", "medium"),
        (450, "linea123", "lluvia-3", "Llamada 123 simulada: inundación en Kennedy", "high"),
        (510, "facebook", "humo-3", "Reporte simulado: humo disminuye en Chapinero", "medium"),
        (600, "sire", "ladera-3", "Entrada SIRE simulada: revisar deslizamiento en Ciudad Bolívar", "high"),
    )
    anchor = datetime.fromtimestamp(anchor_at, timezone.utc) if anchor_at is not None else datetime(2026, 10, 5, 14, tzinfo=timezone.utc)
    return [
        {"platform": platform, "source_id": source_id, "text": text + " · Bogotá #Bogota #Colombia"
          + (" #desastre #inundacion" if any(word in folded(text) for word in ("inund", "aneg", "rio")) else
             " #desastre #lluvia" if "lluvia" in folded(text) else
             " #desastre #incendio" if any(word in folded(text) for word in ("incend", "humo")) else
             " #desastre" if "cultura" not in folded(text) else ""), "mode": "simulation", "is_simulated": True,
         "created_at": (anchor + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
         "source_uri": "", "severity": severity, "confidence": 0.8,
         "raw_metadata": {"scenario": "bogota-10min-v1", "offset_seconds": offset, "synthetic": True}}
        for offset, platform, source_id, text, severity in timeline if offset <= elapsed_seconds
    ]
