"""Fixed Bogotá model-weather context. This data never enters incident evidence."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

import httpx


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE_URL = "https://open-meteo.com/en/docs"
# Same six representative locality anchors verified against IDECA for the demo.
POINTS = (("Suba", 4.741, -74.084), ("Chapinero", 4.649, -74.063),
          ("Ciudad Bolívar", 4.506, -74.148), ("Usaquén", 4.695, -74.031),
          ("Kennedy", 4.627, -74.155), ("Bosa", 4.609, -74.184))
FIELDS = {"temperature_2m": ("temperature_c", "°C", -60, 60),
          "precipitation": ("precipitation_mm", "mm", 0, 1000),
          "wind_speed_10m": ("wind_kmh", "km/h", 0, 500),
          "weather_code": ("weather_code", "wmo code", 0, 99)}
CAMERAS = {
    "status": "links_only", "mode": "official_reference", "verified_at": "2026-10-03",
    "message": "No hay transmisiones públicas verificadas para integrar. El enlace oficial informa ubicaciones, no video en vivo.",
    "items": [{"name": "Cámaras Salvavidas · Secretaría Distrital de Movilidad",
               "url": "https://www.movilidadbogota.gov.co/sitio/camaras-salvavidas-bogota-ubicacion-e-infracciones",
               "status": "reference_only"}],
}
_cache = None
_retry_at = 0.0
_lock = asyncio.Lock()


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # Upstream request explicitly asks for UTC.
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_weather(payload, now):
    if not isinstance(payload, list) or len(payload) != len(POINTS):
        raise ValueError("Unexpected weather locations")
    points = []
    for (name, lat, lon), item in zip(POINTS, payload):
        if not isinstance(item, dict) or item.get("utc_offset_seconds") != 0:
            raise ValueError("Invalid weather timezone")
        # The API returns a model grid cell, which need not equal the requested locality anchor.
        grid_lat, grid_lon = float(item["latitude"]), float(item["longitude"])
        if not math.isfinite(grid_lat) or not math.isfinite(grid_lon) or abs(grid_lat - lat) > 0.15 or abs(grid_lon - lon) > 0.15:
            raise ValueError("Weather grid is outside the Bogotá request")
        current, units = item["current"], item["current_units"]
        point = {"name": name, "lat": lat, "lon": lon, "valid_at": timestamp(current["time"]), "observed_at": None}
        for field, (output, unit, low, high) in FIELDS.items():
            value = current[field]
            if units.get(field) != unit or type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid model weather value or unit")
            point[output] = value
        points.append(point)
    stale = any(abs(datetime.fromisoformat(point["valid_at"].replace("Z", "+00:00")).timestamp() - now) > 10800 for point in points)
    return {"status": "stale" if stale else "available", "mode": "real_context", "data_type": "model_estimate", "points": points,
            "source": "Open-Meteo", "source_url": SOURCE_URL, "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": ("Modelo con más de tres horas de diferencia; consulte la hora de validez. " if stale else "")
                       + "Estimación de modelo meteorológico · contexto real, no evidencia de incidentes ni medición de sensores."}


async def fetch_weather():
    params = {"latitude": ",".join(str(point[1]) for point in POINTS),
              "longitude": ",".join(str(point[2]) for point in POINTS),
              "current": ",".join(FIELDS), "timezone": "UTC", "forecast_days": 1}
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), follow_redirects=False) as client:
        response = await client.get(WEATHER_URL, params=params)
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError("Weather payload exceeds the bounded six-location request")
        return parse_weather(response.json(), time.time())


async def context_layers():
    global _cache, _retry_at
    # ponytail: one viewer process shares a six-point cache; use a shared cache before scaling workers.
    async with _lock:
        if time.monotonic() >= _retry_at:
            try:
                _cache = await fetch_weather()
                _retry_at = time.monotonic() + 900
            except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
                previous = _cache if _cache and _cache.get("points") else {}
                _cache = {**previous, "status": "stale" if previous else "unavailable", "mode": "real_context",
                          "source": "Open-Meteo", "source_url": SOURCE_URL,
                          "message": "Clima no actualizado; última respuesta en caché." if previous else "Clima no disponible; no se sustituyó por datos simulados.",
                          "points": previous.get("points", [])}
                _retry_at = time.monotonic() + 60
        return {"weather": _cache, "cameras": CAMERAS}
