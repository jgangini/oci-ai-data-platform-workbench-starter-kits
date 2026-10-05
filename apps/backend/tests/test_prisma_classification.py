"""Rainfall is a separate review category, never sufficient evidence of flooding."""
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.prisma.classification import PROMPT_VERSION, classify
from app.prisma.core import CATEGORIES, LOCALITIES, SEVERITIES, build_snapshot, default_source, normalize_event


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
    def __init__(self, labels, response_text=None):
        self.labels, self.requests = labels, []
        self.response_text = response_text

    def chat(self, request):
        self.requests.append(request)
        text = json.dumps({"items": self.labels}) if self.response_text is None else self.response_text
        return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=text)]))])))


@pytest.mark.parametrize("template,trim,accepted", [
    ("<json>", 0, True), ("```json\n<json>\n```", 0, True),
    (" \n```JSON \r\n<json>\r\n```\n", 0, True), ("```\n<json>\n```", 0, True),
    ("Here is the result:\n```json\n<json>\n```", 0, False),
    ("```json\n<json>\n```\nExplanation", 0, False), ("```json\n<json>", 0, False),
    ("```yaml\n<json>\n```", 0, False), ("```json\n<json>\n```", 1, False),
    ("```json\n<json>\n```\n```json\n<json>\n```", 0, False), ("<json><json>", 0, False),
    ("```json\n<json>\n]\n}\n```", 0, False),
])
def test_classifier_accepts_only_plain_json_or_one_complete_json_fence(template, trim, accepted):
    event = normalize_event({"platform": "x", "source_id": "fenced", "mode": "simulation",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
             "confidence": 0.7, "claims": [claim()]}
    payload = json.dumps({"items": [label]})
    client = ClaimsModel([label], template.replace("<json>", payload[:-trim] if trim else payload))
    if not accepted:
        with pytest.raises(json.JSONDecodeError):
            classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
        return
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    assert result["id"] == event["id"] and result["mode"] == "Synthetic"
    assert result["claims"][0]["evidence_text"] == event["text"]
    label["claims"][0]["evidence_text"] = "Invented quotation"
    client.response_text = template.replace("<json>", json.dumps({"items": [label]}))
    with pytest.raises(ValueError, match="quote the original"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)


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
    assert results[0]["mode"] == "Synthetic" and results[0]["model_version"] == "model"
    assert results[0]["prompt_version"] == PROMPT_VERSION
    assert {item["relation"] for item in results[0]["claims"]} == {"supports", "contradicts"}
    assert [(item["lat"], item["lon"]) for item in results[0]["claims"]] == [LOCALITIES["Kennedy"], LOCALITIES["Bosa"]]
    prompt = client.requests[0].chat_request.messages[0].content[0].text
    assert "NOT-FOR-THE-MODEL" not in prompt and "nunca verdad verificada" in prompt
    assert "summary_en" in prompt and client.requests[0].chat_request.max_tokens == 2048


def test_classifier_requests_a_strict_schema_for_one_post_and_grounded_claims():
    event = normalize_event({"platform": "x", "source_id": "schema", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
             "confidence": 0.7, "claims": [claim()]}
    client = ClaimsModel([label])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    request = client.requests[0].chat_request
    assert request.max_tokens == 2048 and request.temperature == 0 and len(client.requests) == 1
    assert request.response_format.type == "JSON_SCHEMA"
    contract = request.response_format.json_schema
    assert contract.is_strict is True and contract.name == "territorial_classification"
    schema = json.loads(json.dumps(contract.schema))
    posts = schema["properties"]["items"]
    assert posts["minItems"] == posts["maxItems"] == 1
    post = posts["items"]
    claims = post["properties"]["claims"]
    assert claims["maxItems"] == 8
    for item in (schema, post, claims["items"]):
        assert item["additionalProperties"] is False and set(item["required"]) == set(item["properties"])
    for item in (post, claims["items"]):
        properties = item["properties"]
        assert properties["category"]["enum"] == [*CATEGORIES, "por_clasificar"]
        assert properties["locality"]["enum"] == [*LOCALITIES, "Sin localizar"]
        assert properties["severity"]["enum"] == list(SEVERITIES)
        assert properties["confidence"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert claims["items"]["properties"]["relation"]["enum"] == ["supports", "contradicts"]
    assert result["prompt_version"] == "territorial-control-claims-v5"


def test_stance_request_distinguishes_risk_existence_from_intensity_and_attributes_summaries():
    text = ("La columna de humo está más bajita en Chapinero. "
            "Sería apresurado decir que el incendio terminó solo por eso. Bogotá, Colombia.")
    event = normalize_event({"platform": "instagram", "source_id": "stance-regression", "mode": "simulation",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "incendio", "locality": "Chapinero", "severity": "medium", "confidence": 0.7,
        "claims": [claim("incendio", "Chapinero", evidence_text=text,
            summary_en="The author reports less smoke in Chapinero but warns that this does not establish that the fire has ended.")]}
    client = ClaimsModel([label])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    request = client.requests[0].chat_request
    prompt = request.messages[0].content[0].text
    for instruction in ("existencia del riesgo definido por category y locality", "resuelve la doble negación",
                        "no generes supports y contradicts artificiales", "No atribuyas contradicción sólo por incertidumbre",
                        "atribuye explícitamente lo dicho al autor", "preserva rumores, incertidumbre y límites de observación",
                        "exactamente una postura global del AUTOR", "nunca repitas un par",
                        "category=por_clasificar y claims=[]", "Ejemplo 1, prudencia", "Ejemplo 2, intensidad", "Ejemplo 3, negación"):
        assert instruction in prompt
    assert json.loads(prompt.rsplit("Reportes:\n", 1)[1]) == [{"id": event["id"], "text": text}]
    assert len(client.requests) == 1 and request.max_tokens == 2048 and request.temperature == 0
    assert result["mode"] == "Synthetic" and result["prompt_version"] == "territorial-control-claims-v5"


@pytest.mark.parametrize("claims", [
    [claim(evidence_text="Invented quotation")], [claim(evidence_text="")],
    [claim(relation="verified")], [claim(category="invented")], [claim(locality="Paris")],
    [claim(confidence=float("nan"))], [claim(summary_en="")], [claim()] * 9, {}, [],
])
def test_claims_fail_closed_on_ungrounded_or_invalid_model_output(claims):
    event = normalize_event({"platform": "x", "source_id": "one", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7, "claims": claims}
    client = ClaimsModel([labels])
    with pytest.raises(ValueError):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == (2 if claims == [claim(evidence_text="Invented quotation")] else 1)


@pytest.mark.parametrize("corrected", [True, False])
def test_noncontiguous_native_quote_gets_one_strict_correction_per_post(corrected):
    first = "El pasto junto al foco está oscuro y todavía humea, acá en Chapinero."
    third = "El humo no ha parado."
    event = normalize_event({"platform": "instagram", "source_id": "noncontiguous", "mode": "simulation",
        "text": first + " ¿Nos pueden orientar las autoridades sobre ese tramo? " + third + " #Colombia #Bogota #incendio",
        "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "incendio", "locality": "Chapinero", "severity": "medium", "confidence": 0.7,
             "claims": [claim("incendio", "Chapinero", evidence_text=first + " " + third)]}

    class CorrectionModel(ClaimsModel):
        def chat(self, request):
            if self.requests and corrected:
                self.labels = [{**label, "claims": [claim("incendio", "Chapinero", evidence_text=first)]}]
            return super().chat(request)

    client = CorrectionModel([label])
    if corrected:
        result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
        assert result["claims"][0]["evidence_text"] == first
        assert result["mode"] == "Synthetic" and result["prompt_version"] == "territorial-control-claims-v5"
        assert event["text"] == first + " ¿Nos pueden orientar las autoridades sobre ese tramo? " + third + " #Colombia #Bogota #incendio"
    else:
        with pytest.raises(ValueError, match="quote the original post literally"):
            classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 2
    first_request, correction = client.requests
    original = first_request.chat_request.messages[0].content[0].text
    repaired = correction.chat_request.messages[0].content[0].text
    assert "único fragmento contiguo" in original and repaired.startswith("Corrección:")
    assert repaired.endswith(original)
    assert json.loads(repaired.rsplit("Reportes:\n", 1)[1]) == [{"id": event["id"], "text": event["text"]}]
    assert first + " " + third not in repaired  # Never inject the faulty quote as source evidence.
    for request in client.requests:
        assert request.compartment_id == "compartment" and request.serving_mode.model_id == "model"
        assert request.chat_request.max_tokens == 2048 and request.chat_request.temperature == 0
        assert request.chat_request.response_format == first_request.chat_request.response_format
        assert request.chat_request.response_format.json_schema.is_strict is True


@pytest.mark.parametrize("relation", ["supports", "contradicts"])
@pytest.mark.parametrize("second", ["corrected", "duplicate", "nonliteral"])
def test_duplicate_risk_claims_share_one_correction_budget_with_literal_quotes(relation, second):
    text = "Dicen que hay inundación en Kennedy, Bogotá. Aclaro: no hay inundación en Kennedy, Bogotá."
    event = normalize_event({"platform": "x", "source_id": "duplicate-risk", "mode": "real",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    denied = claim(relation="contradicts", evidence_text="no hay inundación en Kennedy, Bogotá.",
                   summary_en="The author corrects a rumor and denies flooding in Kennedy, Bogotá.")
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
             "claims": [denied, {**denied, "relation": relation}]}

    class CorrectingModel(ClaimsModel):
        def chat(self, request):
            if self.requests and second != "duplicate":
                self.labels = [{**label, "claims": [{**denied, "evidence_text":
                    denied["evidence_text"] if second == "corrected" else "Invented quote"}]}]
            return super().chat(request)

    client = CorrectingModel([label])
    if second == "corrected":
        result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
        assert len(result["claims"]) == 1 and result["claims"][0]["relation"] == "contradicts"
        assert result["text"] == text and result["mode"] == "real"
    else:
        reason = "duplicate risk-locality" if second == "duplicate" else "quote the original post literally"
        with pytest.raises(ValueError, match=reason):
            classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 2
    first, correction = [request.chat_request for request in client.requests]
    assert correction.messages[0].content[0].text.startswith("Corrección: se repitió un par")
    assert correction.messages[0].content[0].text.endswith(first.messages[0].content[0].text)
    assert correction.response_format == first.response_format and correction.max_tokens == first.max_tokens == 2048
    assert json.loads(correction.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1]) == [{"id": event["id"], "text": text}]


@pytest.mark.parametrize("category,locality,quote", [
    ("inundacion", "Bosa", "No hay inundación en Bosa, Bogotá."),
    ("incendio", "Kennedy", "No hay incendio en Kennedy, Bogotá."),
])
def test_unique_risk_contract_preserves_different_risks_or_localities(category, locality, quote):
    event = normalize_event({"platform": "x", "source_id": "distinct-risks", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá. " + quote, "created_at": "2026-10-05T14:00:00Z"})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
        "claims": [claim(), claim(category, locality, relation="contradicts", evidence_text=quote,
            summary_en="The author explicitly denies this risk in the named locality.")]}
    client = ClaimsModel([labels])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    assert len(result["claims"]) == 2 and len(client.requests) == 1
    assert {(row["category"], row["locality"]) for row in result["claims"]} == {("inundacion", "Kennedy"), (category, locality)}


def test_quote_then_duplicate_failure_never_gets_a_third_response():
    event = normalize_event({"platform": "x", "source_id": "shared-budget", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7}

    class CorrectingModel(ClaimsModel):
        def chat(self, request):
            self.labels = [{**label, "claims": [claim(), claim()] if self.requests else [claim(evidence_text="Invented quote")]}]
            return super().chat(request)

    client = CorrectingModel([])
    with pytest.raises(ValueError, match="duplicate risk-locality"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 2


@pytest.mark.parametrize("failure", [RuntimeError("provider unavailable"),
    ValueError("Claim evidence must quote the original post literally"),
    ValueError("Classifier returned duplicate risk-locality claims")])
def test_provider_failure_never_triggers_quote_correction(failure):
    event = normalize_event({"platform": "x", "source_id": "provider", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})

    class FailedModel(ClaimsModel):
        def chat(self, request):
            self.requests.append(request)
            raise failure

    client = FailedModel([])
    with pytest.raises(type(failure), match=str(failure)):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 1


@pytest.mark.parametrize("response", ["not JSON", '{"items":[]}',
    '{"items":[{"id":"wrong-id"}]}'])
def test_response_contract_errors_never_trigger_quote_correction(response):
    event = normalize_event({"platform": "x", "source_id": "contract", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    client = ClaimsModel([], response_text=response)
    with pytest.raises(ValueError):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 1


def test_quote_correction_budget_resets_only_for_the_next_post():
    events = [normalize_event({"platform": "x", "source_id": str(index), "mode": "simulation",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"}) for index in range(2)]

    class TwoPostsModel(ClaimsModel):
        def chat(self, request):
            payload = json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
            self.labels = [{"id": payload["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
                "confidence": 0.7, "claims": [claim(evidence_text=payload["text"] if len(self.requests) % 2 else "Not a literal quote")]}]
            return super().chat(request)

    client = TwoPostsModel([])
    result = classify(events, {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert [row["id"] for row in result] == [row["id"] for row in events] and len(client.requests) == 4
    assert [json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]["id"]
            for request in client.requests] == [events[0]["id"], events[0]["id"], events[1]["id"], events[1]["id"]]


@pytest.mark.parametrize("text", ["Concierto en Bogotá",
    "Hay incendio en Usme y no hay incendio en Usme. No sé cuál afirmación es correcta. Bogotá, Colombia."])
def test_explicit_empty_claims_are_retained_for_irrelevant_or_irreconcilable_posts(text):
    event = normalize_event({"platform": "x", "source_id": "concert", "mode": "real",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
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
