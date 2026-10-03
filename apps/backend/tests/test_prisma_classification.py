"""Rainfall is a separate review category, never sufficient evidence of flooding."""
import json
from types import SimpleNamespace

import pytest

from app.prisma.classification import classify
from app.prisma.core import build_snapshot, normalize_event


@pytest.mark.parametrize("text,category", [
    ("Lluvia intensa en Kennedy, Bogotá", "lluvia"),
    ("Está lloviendo en Bosa", "lluvia"),
    ("Aguacero en Chapinero", "lluvia"),
    ("Lluvia e inundación en Kennedy", "inundacion"),
    ("Lluvia con viviendas anegadas en Bosa", "inundacion"),
    ("Lluvia y nivel del río elevado en Kennedy", "inundacion"),
    ("Lluvia y deslizamiento en Ciudad Bolívar", "movimiento_masa"),
    ("Lluvia durante un incendio en Chapinero", "incendio"),
    ("Lluvia y árbol caído en Suba", "infraestructura"),
])
def test_rainfall_is_separate_and_explicit_hazards_take_priority(text, category):
    event = normalize_event({"platform": "x", "source_id": "risk-1", "mode": "simulation",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    assert event["category"] == category
    snapshot = build_snapshot([event], {}, "v1", event["created_at"])
    assert snapshot["incidents"][0]["category"] == category
    assert snapshot["incidents"][0]["evidence_ids"] == [event["id"]]


def test_classifier_accepts_rainfall_and_retains_the_separate_taxonomy_contract():
    event = normalize_event({"platform": "x", "source_id": "rain-1", "mode": "real",
        "text": "Lluvia intensa en Kennedy, Bogotá", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "lluvia", "locality": "Kennedy", "severity": "medium", "confidence": 0.8}

    class Model:
        def chat(self, request):
            prompt = request.chat_request.messages[0].content[0].text
            assert "no infieras inundacion solo por lluvia" in prompt
            assert "usa inundacion aunque también mencione lluvia" in prompt
            return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"items": [label]}))]))])))

    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=Model())[0]
    assert result["category"] == "lluvia"
    assert result["mode"] == "real"
    assert result["classification_method"] == "oci_genai:model"
    label["category"] = "invented_category"
    with pytest.raises(ValueError, match="Unknown classification category"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=Model())
