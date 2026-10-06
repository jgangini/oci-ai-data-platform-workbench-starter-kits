"""Fictional Colombian telemetry: reproducible readings, never observations of real sensors."""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from datetime import datetime, timezone

from .core import folded, utc_text

# Approximate urban anchors for fictional stations, not a surveyed sensor inventory.
CENTRES = (
    ("Amazonas", "Leticia", "Leticia", -4.215, -69.941, 27),
    ("Antioquia", "Medellín", "Medellín", 6.244, -75.581, 23),
    ("Arauca", "Arauca", "Arauca", 7.084, -70.759, 28),
    ("Atlántico", "Barranquilla", "Barranquilla", 10.984, -74.801, 29),
    ("Bogotá D.C.", "Bogotá", "Kennedy", 4.627, -74.155, 15),
    ("Bolívar", "Cartagena", "Cartagena", 10.400, -75.500, 29),
    ("Boyacá", "Tunja", "Tunja", 5.535, -73.367, 14),
    ("Caldas", "Manizales", "Manizales", 5.070, -75.514, 18),
    ("Caquetá", "Florencia", "Florencia", 1.614, -75.606, 26),
    ("Casanare", "Yopal", "Yopal", 5.337, -72.395, 27),
    ("Cauca", "Popayán", "Popayán", 2.445, -76.614, 20),
    ("Cesar", "Valledupar", "Valledupar", 10.463, -73.253, 30),
    ("Chocó", "Quibdó", "Quibdó", 5.694, -76.661, 27),
    ("Córdoba", "Montería", "Montería", 8.748, -75.882, 29),
    ("Cundinamarca", "Soacha", "Soacha", 4.579, -74.216, 16),
    ("Guainía", "Inírida", "Inírida", 3.866, -67.923, 27),
    ("Guaviare", "San José del Guaviare", "San José del Guaviare", 2.568, -72.639, 27),
    ("Huila", "Neiva", "Neiva", 2.928, -75.281, 29),
    ("La Guajira", "Riohacha", "Riohacha", 11.534, -72.912, 30),
    ("Magdalena", "Santa Marta", "Santa Marta", 11.230, -74.180, 29),
    ("Meta", "Villavicencio", "Villavicencio", 4.143, -73.626, 27),
    ("Nariño", "Pasto", "Pasto", 1.214, -77.281, 14),
    ("Norte de Santander", "Cúcuta", "Cúcuta", 7.894, -72.508, 28),
    ("Putumayo", "Mocoa", "Mocoa", 1.152, -76.647, 24),
    ("Quindío", "Armenia", "Armenia", 4.535, -75.675, 22),
    ("Risaralda", "Pereira", "Pereira", 4.813, -75.696, 23),
    ("San Andrés y Providencia", "San Andrés", "San Andrés", 12.574, -81.705, 28),
    ("Santander", "Bucaramanga", "Bucaramanga", 7.119, -73.122, 24),
    ("Sucre", "Sincelejo", "Sincelejo", 9.304, -75.397, 28),
    ("Tolima", "Ibagué", "Ibagué", 4.439, -75.232, 23),
    ("Valle del Cauca", "Cali", "Cali", 3.452, -76.532, 26),
    ("Vaupés", "Mitú", "Mitú", 1.253, -70.235, 27),
    ("Vichada", "Puerto Carreño", "Puerto Carreño", 6.190, -67.486, 28),
    ("Bogotá D.C.", "Bogotá", "Suba", 4.741, -74.084, 15),
    ("Bogotá D.C.", "Bogotá", "Bosa", 4.609, -74.184, 15),
    ("Bogotá D.C.", "Bogotá", "Ciudad Bolívar", 4.565, -74.164, 14),
    ("Bogotá D.C.", "Bogotá", "Chapinero", 4.648, -74.063, 15),
    ("Bogotá D.C.", "Bogotá", "Usaquén", 4.701, -74.031, 15),
    ("Bogotá D.C.", "Bogotá", "Engativá", 4.705, -74.112, 15),
    ("Bogotá D.C.", "Bogotá", "Fontibón", 4.678, -74.146, 15),
)

# These are simulation thresholds, not official disaster warning thresholds.
SENSOR_TYPES = {
    "river_level": {"unit": "m", "minimum": 0, "maximum": 20, "warning": 3, "critical": 5},
    "rainfall": {"unit": "mm/h", "minimum": 0, "maximum": 250, "warning": 15, "critical": 35},
    "temperature": {"unit": "°C", "minimum": -20, "maximum": 60, "warning": 35, "critical": 40},
    "soil_moisture": {"unit": "%", "minimum": 0, "maximum": 100, "warning": 75, "critical": 90},
    "wind_speed": {"unit": "km/h", "minimum": 0, "maximum": 300, "warning": 40, "critical": 65},
}


def readings_page(snapshot, *, family=None, query="", status=None, order="desc", page=1, limit=20):
    """Page only the sensor fields needed by the administration table."""
    if (family is not None and family not in SENSOR_TYPES or status not in (None, "normal", "warning", "critical")
            or order not in ("asc", "desc") or type(page) is not int or page < 1
            or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(query, str) or len(query) > 200):
        raise ValueError("Invalid sensor reading filters")
    def observed(reading):
        try:
            value = datetime.fromisoformat(reading.get("observed_at", "").replace("Z", "+00:00"))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 0
    latest = {}
    for reading in snapshot.get("sensors", []):
        if not reading.get("sensor_id") or not reading.get("id"):
            continue
        candidate = (observed(reading), reading)
        previous = latest.get(reading["sensor_id"])
        if previous is None or candidate[0] > previous[0] or candidate[0] == previous[0] and reading["id"] < previous[1]["id"]:
            latest[reading["sensor_id"]] = candidate
    match = folded(query.strip())
    rows = [(stamp, row) for stamp, row in latest.values()
            if (not family or row.get("sensor_type") == family) and (not status or row.get("status") == status)
            and match in folded(" ".join(str(row.get(key) or "") for key in
                ("sensor_id", "sensor_type", "department", "municipality", "locality", "status"))
                + " " + str(row.get("sensor_type", "")).replace("_", " "))]
    rows.sort(key=lambda item: (item[0] * (-1 if order == "desc" else 1), item[1]["id"]))
    page = min(page, max(1, math.ceil(len(rows) / limit)))
    fields = ("id", "sensor_id", "sensor_type", "observed_at", "department", "municipality", "locality", "value", "unit", "status")
    return {"items": [{key: row.get(key) for key in fields} for _, row in rows[(page - 1) * limit:page * limit]],
            "total": len(rows), "page": page, "version": snapshot.get("version")}


def configuration(document=None, status=None):
    return {"config_version": 1, "mode": "Synthetic", "is_simulated": True, "interval_minutes": 5,
            "sensor_count": 4000, "families": list(SENSOR_TYPES), "capture_running": False,
            "last_run_at": None, "next_due": None, "last_received_count": 0, "last_error": None,
            **(document or {}), **(status or {})}


def update_configuration(current, values):
    from .source_rules import check_revision
    check_revision(current, values.get("expected_revision"))
    if set(values) - {"expected_revision", "interval_minutes", "sensor_count", "families"}:
        raise ValueError("Unsupported sensor configuration")
    result = {**current, **{key: value for key, value in values.items() if key != "expected_revision"}}
    for key, minimum, maximum in (("interval_minutes", 1, 60), ("sensor_count", 100, 5000)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
    families = result["families"]
    if not isinstance(families, list) or not families or any(not isinstance(item, str) or item not in SENSOR_TYPES for item in families) or len(set(families)) != len(families):
        raise ValueError("Select distinct supported sensor types")
    result.update(config_version=current["config_version"] + 1)
    return result


def allocation(total, kinds):
    """Match the legacy round-robin distribution, including its original order."""
    quotient, remainder = divmod(total, len(kinds))
    return {kind: quotient + (index < remainder) for index, kind in enumerate(kinds)}


def family_configs(document=None, status=None):
    legacy, status = configuration(document), status or {}
    counts = allocation(legacy["sensor_count"], legacy["families"])
    received = allocation(status.get("last_received_count") or 0, legacy["families"])
    result = {}
    for kind in SENSOR_TYPES:
        config = {"sensor_type": kind, "config_version": legacy["config_version"], "mode": "Synthetic", "is_simulated": True,
                  "interval_minutes": legacy["interval_minutes"], "sensor_count": counts.get(kind, 800),
                  "capture_running": legacy["capture_running"] and kind in counts,
                  **legacy.get("by_type", {}).get(kind, {})}
        fallback = {key: status.get(key) for key in ("last_run_at", "next_due", "last_error")} if kind in counts else {}
        state = {**fallback, "last_received_count": received.get(kind, 0)
                 if fallback.get("last_run_at") and status.get("last_received_count") is not None else None}
        if "by_type" in legacy:
            state = status.get("by_type", {}).get(kind, state)
        result[kind] = {"last_run_at": None, "next_due": None, "last_received_count": 0, "last_error": None, **config, **state}
        if not config["capture_running"]:
            result[kind]["next_due"] = None
    return result


def family_document(current, configs):
    if sum(config["sensor_count"] for config in configs.values() if config["capture_running"]) > 5000:
        raise ValueError("Running sensor types may contain at most 5000 sensors in total")
    return {**current, "by_type": configs, "capture_running": any(config["capture_running"] for config in configs.values())}


def update_family(current, kind, values):
    from .source_rules import check_revision
    if kind not in SENSOR_TYPES:
        raise ValueError("Unknown sensor type")
    if set(values) - {"expected_revision", "interval_minutes", "sensor_count"}:
        raise ValueError("Unsupported sensor configuration")
    configs = family_configs(current)
    previous = configs[kind]
    check_revision(previous, values.get("expected_revision"))
    changed = {**previous, **{key: value for key, value in values.items() if key != "expected_revision"}}
    for key, maximum in (("interval_minutes", 60), ("sensor_count", 5000)):
        if type(changed[key]) is not int or not 1 <= changed[key] <= maximum:
            raise ValueError(f"{key} must be between 1 and {maximum}")
    configs[kind] = {**changed, "config_version": previous["config_version"] + 1}
    return family_document({**current, "config_version": current["config_version"] + 1}, configs)


def control_family(current, kind, running):
    if kind not in SENSOR_TYPES:
        raise ValueError("Unknown sensor type")
    configs = family_configs(current)
    previous = configs[kind]
    changed = previous["capture_running"] != running
    configs[kind] = {**previous, "capture_running": running, "config_version": previous["config_version"] + int(changed)}
    return family_document({**current, "config_version": current["config_version"] + int(changed)}, configs)


def validate_record(record):
    if not isinstance(record, dict) or record.get("is_simulated") is not True or record.get("mode") != "Synthetic" or record.get("country") != "Colombia":
        raise ValueError("Sensor readings must be explicitly simulated in Colombia")
    kind = record.get("sensor_type")
    rules = SENSOR_TYPES.get(kind) if isinstance(kind, str) else None
    if not rules or record.get("metric") != kind or record.get("unit") != rules["unit"]:
        raise ValueError("Invalid sensor metric or unit")
    for key in ("event_id", "sensor_id", "batch_id"):
        if not isinstance(record.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record[key]):
            raise ValueError("Invalid sensor identifier")
    for key in ("locality", "municipality", "department"):
        if not isinstance(record.get(key), str) or not 1 <= len(record[key]) <= 100:
            raise ValueError("Invalid sensor location")
    for key, low, high in (("lat", -4.3, 13.6), ("lon", -81.8, -66.7), ("value", rules["minimum"], rules["maximum"])):
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid sensor coordinate or measurement")
    stamp = datetime.fromisoformat(str(record.get("observed_at", "")).replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0 or record.get("event_date") != stamp.date().isoformat():
        raise ValueError("Sensor time must be UTC with a matching event_date")
    expected = "critical" if record["value"] >= rules["critical"] else "warning" if record["value"] >= rules["warning"] else "normal"
    if record.get("status") != expected:
        raise ValueError("Sensor status does not match its simulation threshold")
    fields = ("event_id", "sensor_id", "sensor_type", "observed_at", "event_date", "lat", "lon", "locality", "municipality", "department", "country", "metric", "value", "unit", "status", "mode", "is_simulated", "batch_id")
    return {**{name: record[name] for name in fields}, "observed_at": stamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            **{name: float(record[name]) for name in ("lat", "lon", "value")}}


def apply_locations(rows, locations):
    return [{**row, **{key: locations[row["sensor_id"]][key] for key in ("lat", "lon")}}
            if row["sensor_id"] in locations else row for row in rows]


def update_location(sensor, locations, values, now):
    for key, low, high in (("lat", -4.3, 13.6), ("lon", -81.8, -66.7)):
        for field in (key, "expected_" + key):
            value = values.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Sensor coordinates must be finite numbers within Colombia")
    current = locations.get(sensor["sensor_id"], sensor)
    if any(current[key] != values["expected_" + key] for key in ("lat", "lon")):
        from fastapi import HTTPException
        raise HTTPException(409, "Sensor coordinates changed. Refresh the sensor before saving again.")
    return {**locations, sensor["sensor_id"]: {"lat": values["lat"], "lon": values["lon"], "updated_at": utc_text(now)}}


def generate_batch(now, *, sensor_count=4000, families=None, seed=0):
    if type(sensor_count) is not int or not 1 <= sensor_count <= 5000:
        raise ValueError("sensor_count must be between 1 and 5000")
    config = update_configuration(configuration(), {"families": list(SENSOR_TYPES) if families is None else families})
    kinds = config["families"]
    batch_time = int(now)
    batch_id = f"sim-{batch_time}-{seed}"
    rows = []
    # Stable station locations and smooth regional weather persist between randomly ordered batches.
    for index in range(sensor_count):
        site = index // len(kinds)
        place = site % len(CENTRES)
        department, municipality, locality, lat, lon, baseline = CENTRES[place]
        kind = kinds[index % len(kinds)]
        sensor_id = f"CO-{place:02d}-{site // len(CENTRES):03d}-{kind}"
        location = random.Random(sensor_id)
        rng = random.Random(f"{seed}:{sensor_id}:{batch_time // 300}")
        storm = max(0, math.sin(batch_time / 2400 + place * .47))
        # Bogotá's fictional flood scenario is deliberate and must remain labelled Synthetic.
        storm = max(storm, .75) if municipality == "Bogotá" and kind != "temperature" else storm
        values = {"river_level": 1.2 + 4.5 * storm, "rainfall": 45 * storm,
                  "temperature": baseline + 4 * math.sin(batch_time / 13751 + place),
                  "soil_moisture": 40 + 53 * storm, "wind_speed": 7 + 65 * storm}
        rules = SENSOR_TYPES[kind]
        value = round(max(rules["minimum"], values[kind] + rng.uniform(-1, 1)), 2)
        observed = utc_text(batch_time - rng.randint(0, 120))
        event_id = hashlib.sha256(f"{batch_id}:{sensor_id}".encode()).hexdigest()[:32]
        rows.append(validate_record({"event_id": event_id, "sensor_id": sensor_id, "sensor_type": kind,
            "observed_at": observed, "event_date": observed[:10], "lat": round(lat + location.uniform(-.003, .003), 6),
            "lon": round(lon + location.uniform(-.003, .003), 6), "locality": locality, "municipality": municipality,
            "department": department, "country": "Colombia", "metric": kind, "value": value, "unit": rules["unit"],
            "status": "critical" if value >= rules["critical"] else "warning" if value >= rules["warning"] else "normal",
            "mode": "Synthetic", "is_simulated": True, "batch_id": batch_id}))
    random.Random(batch_id).shuffle(rows)
    return rows


def text_files(rows):
    grouped = {}
    for record in rows:
        row = validate_record(record)
        key = f"sensors/{row['sensor_type']}/{row['batch_id']}.txt"
        grouped.setdefault(key, []).append(json.dumps(row, ensure_ascii=False, allow_nan=False))
    return {name: ("\n".join(lines) + "\n").encode("utf-8") for name, lines in grouped.items()}
