"""Bogotá scope and an opt-in, read-only check against official IDECA polygons."""
import json
import os
import urllib.parse
import urllib.request

import pytest

from app.territorial.core import LOCALITIES, build_snapshot, folded, normalize_event


def event(text, **values):
    return {"platform": "x", "source_id": "geo-1", "mode": "simulation", "text": text,
            "created_at": "2026-10-05T14:00:00Z", **values}


@pytest.mark.parametrize("locality", LOCALITIES)
def test_simulated_locality_is_approximate_anchor_not_exact_incident_position(locality):
    result = normalize_event(event("Incendio en " + folded(locality)))
    assert result["locality"] == locality
    assert (result["lat"], result["lon"]) == LOCALITIES[locality]
    assert result["location_method"] == "text_locality_anchor"


@pytest.mark.parametrize("text", [
    "Incendio entre Kennedy y Bosa, lugar sin confirmar",
    "Incendio en Subachoque",
    "Inundación en Bogotá, ubicación sin confirmar",
])
def test_ambiguous_and_partial_names_keep_risk_for_review_without_marker(text):
    result = normalize_event(event(text))
    assert result["locality"] == "Sin localizar"
    assert result["lat"] is None and result["lon"] is None
    snapshot = build_snapshot([result], {}, "v1", result["created_at"])
    assert len(snapshot["incidents"]) == 1
    assert snapshot["incidents"][0]["lat"] is None


@pytest.mark.parametrize("text", [
    "Incendio en Kennedy, Quito, Ecuador",
    "Inundación en Ciudad Bolívar, Venezuela",
    "Alerta para que no suba el nivel del río en Bogotá",
    "Incendio en Kennedy",
])
def test_real_names_do_not_assign_bogota_without_classified_jurisdiction(text):
    result = normalize_event(event(text, mode="real", lat=8.12, lon=-63.55))
    assert result["locality"] == "Sin localizar"
    assert result["lat"] is None and result["lon"] is None
    assert result["location_method"] == "unresolved"


def test_unresolved_classification_clears_old_anchor_and_stays_unmapped():
    result = normalize_event(event("Inundación en Bogotá", locality="Sin localizar", category="inundacion",
        lat=4.627, lon=-74.155, location_method="text_locality_centroid"))
    assert result["category"] == "inundacion"
    assert result["lat"] is None and result["lon"] is None


def test_reclassified_locality_replaces_previous_approximate_anchor():
    result = normalize_event(event("Incendio en Bosa", mode="real", locality="Bosa",
        lat=4.627, lon=-74.155, location_method="text_locality_centroid"))
    assert (result["lat"], result["lon"]) == LOCALITIES["Bosa"]


@pytest.mark.skipif(os.environ.get("PRISMA_VERIFY_OFFICIAL_GEOGRAPHY") != "1",
                    reason="Explicit opt-in required for the public official GIS service")
def test_six_anchors_intersect_their_official_locality_polygons():
    # CAR publishes this layer from the IDECA reference map (copyright IDECA).
    # SDP/IDECA source: https://www.ideca.gov.co/recursos/mapas/localidad-bogota-dc
    service = "https://sig.car.gov.co/arcgis/rest/services/visor/Division_Territorial/MapServer/5/query"
    codes = {"Suba": "11", "Chapinero": "02", "Ciudad Bolívar": "19", "Usaquén": "01", "Kennedy": "08", "Bosa": "07"}
    for name, (lat, lon) in LOCALITIES.items():
        query = urllib.parse.urlencode({"f": "json", "geometry": f"{lon},{lat}", "inSR": 4326,
            "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "LocNombre,LocCodigo", "returnGeometry": "false"})
        with urllib.request.urlopen(service + "?" + query, timeout=15) as response:
            data = json.load(response)
        assert "error" not in data, data
        matches = [feature["attributes"] for feature in data["features"]]
        assert len(matches) == 1, (name, matches)
        assert (folded(matches[0]["LocNombre"]), matches[0]["LocCodigo"]) == (folded(name), codes[name])
