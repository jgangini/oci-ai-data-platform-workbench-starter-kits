"""Rainfall is a separate review category, never sufficient evidence of flooding."""
import json
import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gods_eye_view.classification import PROMPT_VERSION, _CLASSIFICATION_SCHEMA, _claims, classify
from app.gods_eye_view.core import CATEGORIES, LOCALITIES, SEVERITIES, build_snapshot, default_source, normalize_event


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
            "relation": "supports", "evidence_span_id": "S1",
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
    with pytest.raises(ValueError, match="invalid evidence span reference"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)


def test_multiple_grounded_claims_preserve_one_post_and_ignore_model_provenance_overrides():
    event = normalize_event({"platform": "x", "source_id": "two-claims", "mode": "simulation",
        "text": "Hay inundación en Kennedy, Bogotá. No hay incendio en Bosa, Bogotá.",
        "created_at": "2026-10-05T14:00:00Z", "raw_metadata": {"evaluation_secret": "NOT-FOR-THE-MODEL"}})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
        "mode": "real", "model_version": "forged", "prompt_version": "forged", "claims": [claim(),
            claim("incendio", "Bosa", relation="contradicts",
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
    assert contract.is_strict is True and contract.name == "gods_eye_view_classification"
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
    assert claims["items"]["properties"]["evidence_span_id"] == {"type": "string", "enum": ["S1"]}
    assert "evidence_text" not in claims["items"]["properties"]
    assert result["claims"][0]["evidence_text"] == event["text"]
    assert "evidence_span_id" not in result["claims"][0]
    assert result["prompt_version"] == "gods-eye-view-control-claims-v6"


def test_stance_request_distinguishes_risk_existence_from_intensity_and_attributes_summaries():
    text = ("La columna de humo está más bajita en Chapinero. "
            "Sería apresurado decir que el incendio terminó solo por eso. Bogotá, Colombia.")
    event = normalize_event({"platform": "instagram", "source_id": "stance-regression", "mode": "simulation",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "incendio", "locality": "Chapinero", "severity": "medium", "confidence": 0.7,
        "claims": [claim("incendio", "Chapinero",
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
    assert json.loads(prompt.rsplit("Reportes:\n", 1)[1]) == [{"id": event["id"], "text": text, "evidence_spans": {"S1": text}}]
    assert len(client.requests) == 1 and request.max_tokens == 2048 and request.temperature == 0
    assert result["mode"] == "Synthetic" and result["prompt_version"] == "gods-eye-view-control-claims-v6"


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
    assert len(client.requests) == 1


def test_native_post_0062_selects_complete_literal_span_without_reproducing_the_failed_quote():
    fixture = Path(__file__).resolve().parents[3] / "datasets/synthetic/social-media/natural-hazards/colombia/bogota/v1/posts/post-0062/post.json"
    text = json.loads(fixture.read_text(encoding="utf-8"))["message"]
    event = normalize_event({"platform": "instagram", "source_id": "cycle-11:bogota-v1:post-0062", "mode": "simulation",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "incendio", "locality": "Chapinero", "severity": "medium", "confidence": 0.7,
             "claims": [claim("incendio", "Chapinero", summary_en="The author reports continued smoke in Chapinero and asks for official guidance.")]}
    client = ClaimsModel([label])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    assert result["claims"][0]["evidence_text"] == text and "evidence_span_id" not in result["claims"][0]
    assert result["text"] == text and result["mode"] == "Synthetic" and len(client.requests) == 1
    payload = json.loads(client.requests[0].chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
    assert payload == {"id": event["id"], "text": text, "evidence_spans": {"S1": text}}
    invalid_quote = text.split(" ¿Nos pueden orientar", 1)[0] + " El humo no ha parado."
    with pytest.raises(ValueError, match="quote the original post literally"):
        _claims([{**result["claims"][0], "evidence_text": invalid_quote}], text)


@pytest.mark.parametrize("selection", [{}, {"evidence_span_id": "S0"}, {"evidence_span_id": "S2"},
    {"evidence_span_id": 1}, {"evidence_span_id": None}, {"evidence_span_id": ["S1"]},
    {"evidence_span_id": "s1"}, {"evidence_span_id": " S1"},
    {"evidence_text": "Hay inundación en Kennedy, Bogotá."},
    {"evidence_span_id": "S1", "evidence_text": "Hay inundación en Kennedy, Bogotá."},
    {"evidence_span_id": "S1", "evidence_text": "Invented quote"}])
def test_invalid_span_selection_or_free_quote_is_never_repaired_or_accepted(selection):
    event = normalize_event({"platform": "x", "source_id": "selection", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    selected = claim()
    selected.pop("evidence_span_id")
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
             "claims": [{**selected, **selection}]}
    client = ClaimsModel([label])
    with pytest.raises(ValueError, match="invalid evidence span reference"):
        classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 1


@pytest.mark.parametrize("length", [1, 999, 1000, 1001, 1499, 1500, 1501, 12000, 12001])
def test_literal_spans_cover_bounded_input_with_overlap_and_preserve_selected_bytes(length):
    text = "".join(chr(0x4E00 + index) for index in range(length))
    event = normalize_event({"platform": "x", "source_id": "long-spans", "mode": "real",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    event["text"] = text  # Exercise the request boundary independently of normalize_event's own cap.

    class SelectingModel(ClaimsModel):
        def chat(self, request):
            payload = json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
            self.labels = [{"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
                "confidence": 0.7, "claims": [claim(evidence_span_id=list(payload["evidence_spans"])[-1])]}]
            return super().chat(request)

    client = SelectingModel([])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    request = client.requests[0].chat_request
    payload = json.loads(request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
    bounded = text[:12000]
    spans = payload["evidence_spans"]
    assert payload["text"] == bounded and 1 <= len(spans) <= 23
    assert list(spans) == [f"S{index + 1}" for index in range(len(spans))]
    offsets = [bounded.index(span) for span in spans.values()]
    assert offsets[0] == 0 and offsets[-1] + len(list(spans.values())[-1]) == len(bounded)
    assert all(0 < right - left <= 500 for left, right in zip(offsets, offsets[1:]))
    assert all(1 <= len(span) <= 1000 and span in bounded for span in spans.values())
    assert len(spans) == (1 if length <= 1000 else 23 if length >= 12000 else 2 if length <= 1500 else 3)
    assert request.response_format.json_schema.schema["properties"]["items"]["items"]["properties"]["claims"]["items"]["properties"]["evidence_span_id"]["enum"] == list(spans)
    assert result["claims"][0]["evidence_text"] == list(spans.values())[-1]
    assert result["text"] == bounded and request.max_tokens == 2048


@pytest.mark.parametrize("text", ["", " ", "\r\n\t ", " " * 12000 + "outside bounded input"])
def test_empty_bounded_post_fails_before_model_request(text):
    client = ClaimsModel([])
    with pytest.raises(ValueError, match="non-empty post text"):
        classify([{"id": "x:empty", "text": text}], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert client.requests == []


def test_whitespace_only_windows_are_not_selectable_and_unicode_bytes_are_preserved():
    text = " " * 11000 + "\r\n¿Hay inundación en Bogotá? 🌧️  "
    event = normalize_event({"platform": "x", "source_id": "spaced", "mode": "real",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})

    class SelectingModel(ClaimsModel):
        def chat(self, request):
            payload = json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
            self.labels = [{"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
                "confidence": 0.7, "claims": [claim(evidence_span_id=list(payload["evidence_spans"])[-1])]}]
            assert payload["text"] == text and all(span.strip() for span in payload["evidence_spans"].values())
            return super().chat(request)

    client = SelectingModel([])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    assert result["claims"][0]["evidence_text"] == text[-1000:]


def test_each_post_has_an_isolated_schema_and_eight_claims_return_only_short_references():
    original_schema = deepcopy(_CLASSIFICATION_SCHEMA)
    events = [normalize_event({"platform": "x", "source_id": f"isolated-{index}", "mode": "real",
        "text": "".join(chr(0x4E00 + index * 2000 + offset) for offset in range(length)),
        "created_at": "2026-10-05T14:00:00Z"}) for index, length in enumerate((1000, 1001))]

    class EightClaimsModel(ClaimsModel):
        def chat(self, request):
            payload = json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
            self.labels = [{"id": payload["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
                "confidence": 0.7, "claims": [claim(category=list(CATEGORIES)[index // len(LOCALITIES)],
                    locality=list(LOCALITIES)[index % len(LOCALITIES)], evidence_span_id=list(payload["evidence_spans"])[-1])
                    for index in range(8)]}]
            assert "evidence_text" not in json.dumps(self.labels)
            return super().chat(request)

    client = EightClaimsModel([])
    results = classify(events, {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 2 and _CLASSIFICATION_SCHEMA == original_schema
    enums = [request.chat_request.response_format.json_schema.schema["properties"]["items"]["items"]["properties"]["claims"]["items"]["properties"]["evidence_span_id"]["enum"]
             for request in client.requests]
    assert enums == [["S1"], ["S1", "S2"]]
    enums[0].append("not-shared")
    assert enums[1] == ["S1", "S2"] and _CLASSIFICATION_SCHEMA == original_schema
    for event, result in zip(events, results):
        assert len(result["claims"]) == 8
        assert all(row["evidence_text"] == event["text"][-1000:] and "evidence_span_id" not in row for row in result["claims"])


@pytest.mark.parametrize("relation", ["supports", "contradicts"])
@pytest.mark.parametrize("second", ["corrected", "duplicate", "invalid-reference"])
def test_duplicate_risk_claims_get_one_correction_with_the_same_span_contract(relation, second):
    text = "Dicen que hay inundación en Kennedy, Bogotá. Aclaro: no hay inundación en Kennedy, Bogotá."
    event = normalize_event({"platform": "x", "source_id": "duplicate-risk", "mode": "real",
        "text": text, "created_at": "2026-10-05T14:00:00Z"})
    denied = claim(relation="contradicts",
                   summary_en="The author corrects a rumor and denies flooding in Kennedy, Bogotá.")
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
             "claims": [denied, {**denied, "relation": relation}]}

    class CorrectingModel(ClaimsModel):
        def chat(self, request):
            if self.requests and second != "duplicate":
                self.labels = [{**label, "claims": [{**denied, "evidence_span_id":
                    "S1" if second == "corrected" else "unknown"}]}]
            return super().chat(request)

    client = CorrectingModel([label])
    if second == "corrected":
        result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
        assert len(result["claims"]) == 1 and result["claims"][0]["relation"] == "contradicts"
        assert result["text"] == text and result["mode"] == "real"
    else:
        reason = "duplicate risk-locality" if second == "duplicate" else "invalid evidence span reference"
        with pytest.raises(ValueError, match=reason):
            classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)
    assert len(client.requests) == 2
    first, correction = [request.chat_request for request in client.requests]
    assert correction.messages[0].content[0].text.startswith("Corrección: se repitió un par")
    assert correction.messages[0].content[0].text.endswith(first.messages[0].content[0].text)
    assert correction.response_format == first.response_format and correction.max_tokens == first.max_tokens == 2048
    assert json.loads(correction.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1]) == [{"id": event["id"], "text": text, "evidence_spans": {"S1": text}}]


@pytest.mark.parametrize("category,locality,quote", [
    ("inundacion", "Bosa", "No hay inundación en Bosa, Bogotá."),
    ("incendio", "Kennedy", "No hay incendio en Kennedy, Bogotá."),
])
def test_unique_risk_contract_preserves_different_risks_or_localities(category, locality, quote):
    event = normalize_event({"platform": "x", "source_id": "distinct-risks", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá. " + quote, "created_at": "2026-10-05T14:00:00Z"})
    labels = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7,
        "claims": [claim(), claim(category, locality, relation="contradicts",
            summary_en="The author explicitly denies this risk in the named locality.")]}
    client = ClaimsModel([labels])
    result = classify([event], {"model_id": "model", "compartment_id": "compartment"}, client=client)[0]
    assert len(result["claims"]) == 2 and len(client.requests) == 1
    assert {(row["category"], row["locality"]) for row in result["claims"]} == {("inundacion", "Kennedy"), (category, locality)}


def test_duplicate_failure_never_gets_a_third_response():
    event = normalize_event({"platform": "x", "source_id": "shared-budget", "mode": "real",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"})
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium", "confidence": 0.7}

    class CorrectingModel(ClaimsModel):
        def chat(self, request):
            self.labels = [{**label, "claims": [claim(), claim()]}]
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


def test_duplicate_correction_budget_resets_only_for_the_next_post():
    events = [normalize_event({"platform": "x", "source_id": str(index), "mode": "simulation",
        "text": "Hay inundación en Kennedy, Bogotá.", "created_at": "2026-10-05T14:00:00Z"}) for index in range(2)]

    class TwoPostsModel(ClaimsModel):
        def chat(self, request):
            payload = json.loads(request.chat_request.messages[0].content[0].text.rsplit("Reportes:\n", 1)[1])[0]
            self.labels = [{"id": payload["id"], "category": "inundacion", "locality": "Kennedy", "severity": "medium",
                "confidence": 0.7, "claims": [claim()] if len(self.requests) % 2 else [claim(), claim()]}]
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
    path = Path(__file__).resolve().parents[3] / "scripts/evaluate_gods_eye_view_corpus.py"
    spec = importlib.util.spec_from_file_location("offline_gods_eye_view_evaluation", path)
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
