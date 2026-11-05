"""Inclusive WGS84 view bounds shared by the viewer bridge and AIDP query."""
import math
import re


def parse_bbox(value):
    if value in (None, ""):
        return None
    try:
        parts = value.split(",")
        if len(parts) != 4 or any(not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", part.strip()) for part in parts):
            raise ValueError("Expected west,south,east,north")
        west, south, east, north = (float(part) for part in parts)
        if not all(math.isfinite(point) for point in (west, south, east, north)):
            raise ValueError("Non-finite coordinates")
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("Reversed or out-of-range bounds")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Área inválida: use oeste,sur,este,norte dentro de WGS84 sin cruzar el antimeridiano") from exc
    return west, south, east, north


def within_bbox(item, bounds):
    if bounds is None:
        return True
    lat, lon = item.get("lat"), item.get("lon")
    if type(lat) not in (int, float) or type(lon) not in (int, float):
        return False
    west, south, east, north = bounds
    return west <= lon <= east and south <= lat <= north
