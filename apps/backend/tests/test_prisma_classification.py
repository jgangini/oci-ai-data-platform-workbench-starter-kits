"""Rainfall is a separate review category, never sufficient evidence of flooding."""
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.prisma.classification import PROMPT_VERSION, classify
from app.prisma.core import LOCALITIES, build_snapshot, default_source, normalize_event


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
    assert "claims" not in result  # Legacy labels never invent a supporting assertion.
    assert result["model_version"] == "model" and result["prompt_version"] == PROMPT_VERSION
    snapshot = build_snapshot([result], {}, "legacy", event["created_at"], rules={"x": default_source("x")})
    assert len(snapshot["incidents"]) == 1 and snapshot["event_posts"][0]["relation"] == "unclassified"
    label["category"] = "invented_category"
    with pytest.raises(ValueError, match="Unknown classification category"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=Model())


def claim(category="inundacion", locality="Kennedy", **changes):
    return {"category": category, "locality": locality, "severity": "medium", "confidence": 0.7,
            "relation": "supports", "evidence_text": "Hay inundación en Kennedy, Bogotá.",
            "summary_en": "The author reports flooding in Kennedy, Bogotá.", **changes}


class ClaimsModel:
    def __init__(self, labels):
        self.labels, self.requests = labels, []

    def chat(self, request):
        self.requests.append(request)
        text = json.dumps({"items": self.labels})
        return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=text)]))])))


def test_multiple_grounded_claims_preserve_one_post_and_ignore_model_provenance_overrides():
    event = normalize_event({"platform": "x", "source_id": "two-claims", "mode": "simulation",
        "text": "Hay inundación en Kennedy, Bogotá. No hay incendio en Bosa, Bogotá.",
        "created_at": "2026-10-05T14:00:00Z", "raw_metadata": {"evaluation_secret": "NOT-FOR-THE-MODEL"}})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
        "mode": "real", "model_version": "forged", "prompt_version": "forged", "claims": [claim(),
            claim("incendio", "Bosa", relation="contradicts", evidence_text="No hay incendio en Bosa, Bogotá.",
                  summary_en="The author denies a fire in Bosa, Bogotá.")]}
    client = ClaimsModel([labels])
    results = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(results) == 1 and results[0]["id"] == event["id"] and len(results[0]["claims"]) == 2
    assert results[0]["mode"] == "simulation" and results[0]["model_version"] == "model"
    assert results[0]["prompt_version"] == PROMPT_VERSION
    assert {item["relation"] for item in results[0]["claims"]} == {"supports", "contradicts"}
    assert [(item["lat"], item["lon"]) for item in results[0]["claims"]] == [LOCALITIES["Kennedy"], LOCALITIES["Bosa"]]
    prompt = client.requests[0].chat_request.messages[0].content[0].text
    assert "NOT-FOR-THE-MODEL" not in prompt and "nunca verdad verificada" in prompt
    assert "summary_en" in prompt and client.requests[0].chat_request.max_tokens == 2048


@pytest.mark.parametrize("claims", [
    [claim(evidence_text="Invented quotation")], [claim(evidence_text="")],
    [claim(relation="verified")], [claim(category="invented")], [claim(locality="Paris")],
    [claim(confidence=float("nan"))], [claim(summary_en="")], [claim()] * 9, {}, [],
])
def test_claims_fail_closed_on_ungrounded_or_invalid_model_output(claims):
    event = normalize_event({"platform": "x", "source_id": "one", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7, "claims": claims}
    with pytest.raises(ValueError):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=ClaimsModel([labels]))


def test_explicit_empty_claims_are_retained_for_irrelevant_posts():
    event = normalize_event({"platform": "x", "source_id": "concert", "mode": "real",
        "text": "Concierto en Bogotá", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "por_clasificar", "locality": "Sin localizar", "severity": "low", "confidence": 0.9, "claims": []}
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=ClaimsModel([label]))[0]
    assert result["claims"] == []
    assert build_snapshot([result], {}, "empty", event["created_at"], rules={"x": default_source("x")})["incidents"] == []


def test_offline_evaluation_reports_pending_and_measured_false_positives_negatives():
    path = Path(__file__).resolve().parents[3] / "scripts/evaluate_prisma_corpus.py"
    spec = importlib.util.spec_from_file_location("offline_prisma_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    truth = {"dataset_version": "test", "posts": [
        {"fixture_id": "post-0001", "expected_category": "inundacion", "evidence_role": "supports", "scenario_assertion": "false"},
        {"fixture_id": "post-0002", "expected_category": "incendio", "evidence_role": "contradicts", "scenario_assertion": "true"},
        {"fixture_id": "post-0003", "expected_category": "lluvia", "evidence_role": "supports", "scenario_assertion": "true"}]}
    pending = module.evaluate(truth)
    assert pending["status"] == "pending" and pending["category_claims"] is None and pending["pending_count"] == 3
    predictions = [{"fixture_id": "post-0001", "claims": [claim()]},
                   {"raw_metadata": {"fixture_id": "post-0002"}, "claims": [claim("lluvia")]}]
    result = module.evaluate(truth, predictions)
    assert result["status"] == "partial" and result["pending_count"] == 1
    assert result["category_claims"] == {"true_positive": 1, "false_positive": 1, "false_negative": 1}
    assert result["textual_relations"] == {"true_positive": 1, "false_positive": 1, "false_negative": 1, "evaluated_count": 2}
    assert result["scenario_false_assertions"] == {"fixture_count": 1, "with_predictions": 1}
    assert result["truth_detection"]["status"] == "not_evaluated"
    with pytest.raises(ValueError, match="once each"):
        module.evaluate(truth, predictions * 2)
