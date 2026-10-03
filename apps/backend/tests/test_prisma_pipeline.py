import copy
import json
from types import SimpleNamespace

import pytest

from app.prisma import pipeline
from app.prisma.core import default_source, normalize_event, simulation_events
from app.prisma.classification import classify
from app.prisma.runtime_secrets import runtime_auth


CONFIG = {"namespace": "namespace", "bucket": "gold-bucket", "region": "test-region", "model_id": "model",
          "compartment_id": "compartment", "catalog": "oci_medallion"}
NOW = 1791209100.0


class Lake:
    def __init__(self, log):
        self.log, self.data, self.fail = log, {"bronze": {}, "silver": {}, "gold": {}}, None

    def put(self, layer, records):
        self.log.append(layer)
        if self.fail == layer:
            raise RuntimeError("Injected durable write failure")
        for record in records:
            self.data[layer].setdefault(record["id"], copy.deepcopy(record))

    def pending(self):
        return [value for key, value in self.data["bronze"].items() if key not in self.data["silver"]]

    def visible(self, run_id, _now):
        return [value for value in self.data["silver"].values()
                if value["mode"] == "real" or value["raw_metadata"].get("scenario_run_id") == run_id]


class Objects:
    def __init__(self, log):
        self.log, self.data, self.fail = log, {}, None

    def put_object(self, namespace, bucket, key, data, **_kwargs):
        self.log.append(key)
        if self.fail and key.startswith(self.fail):
            raise RuntimeError("Injected Object Storage failure")
        assert namespace == CONFIG["namespace"] and bucket == CONFIG["bucket"]
        self.data[key] = data


@pytest.fixture
def runtime(monkeypatch):
    log, docs, publications = [], {}, {}

    def read(_connection, name):
        return copy.deepcopy(docs.get(name, {"revision": 0}))

    def mutate(connection, name, change):
        current = read(connection, name)
        updated = {**change(current), "revision": current.get("revision", 0) + 1}
        docs[name] = copy.deepcopy(updated)
        log.append("doc:" + name)
        return updated

    def publish(_connection, snapshot):
        log.append("adb")
        previous = publications.setdefault(snapshot["version"], copy.deepcopy(snapshot))
        assert previous == snapshot, "An immutable version changed during retry"

    monkeypatch.setattr(pipeline, "read_document", read)
    monkeypatch.setattr(pipeline, "mutate_document", mutate)
    monkeypatch.setattr(pipeline, "publish", publish)
    return log, docs, publications, Lake(log), Objects(log)


def test_cursor_is_not_committed_before_landing_and_bronze(runtime):
    log, docs, _, lake, objects = runtime
    lake.fail = "bronze"
    with pytest.raises(RuntimeError):
        pipeline.ingest_page(object(), objects, lake, CONFIG, "x", simulation_events(0), {"cursor": {"since_id": "30"}})
    assert "checkpoint_x" not in docs
    assert any(key.startswith("01_landing/prisma/") for key in objects.data)
    lake.fail = None
    pipeline.ingest_page(object(), objects, lake, CONFIG, "x", simulation_events(0), {"cursor": {"since_id": "30"}})
    assert log[-2:] == ["bronze", "doc:checkpoint_x"]
    assert len(lake.data["bronze"]) == 1


def test_snapshot_pointer_moves_only_after_adb_and_snapshot_object_and_retry_is_immutable(runtime):
    log, docs, publications, lake, objects = runtime
    objects.fail = "04_gold/prisma/snapshots/"
    state = {"status": "idle", "elapsed_seconds": 0, "run_id": None}
    with pytest.raises(RuntimeError):
        pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, state, NOW)
    assert len(publications) == 1
    assert "04_gold/prisma/current.json" not in objects.data
    version = docs["runtime"]["publication"]["version"]
    objects.fail = None
    snapshot = pipeline.publish_snapshot(object(), objects, lake, CONFIG, [], {}, state, NOW + 60)
    assert snapshot["version"] == version
    assert snapshot["published_at"] == docs["runtime"]["publication"]["published_at"]
    assert log[-4:] == ["gold", "adb", f"04_gold/prisma/snapshots/{version}.json", "04_gold/prisma/current.json"]


def test_empty_bootstrap_runs_actual_publication_path_without_simulated_data(runtime):
    log, docs, _, lake, objects = runtime
    def no_classification(_events):
        raise AssertionError("An empty deployment must not classify fabricated data")
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                            client=object(), classifier=no_classification, clock=lambda: NOW)
    assert snapshot["incidents"] == snapshot["evidence"] == []
    assert snapshot["runtime"] == "aidp"
    assert docs["status_pipeline"]["status"] == "ready"
    assert "04_gold/prisma/current.json" in objects.data
    assert not any(key.startswith("01_landing") for key in objects.data)
    assert len(lake.data["gold"]) == 1


def test_failed_classification_retries_bronze_without_fabricating_success(runtime):
    _, docs, _, lake, objects = runtime
    docs["simulation"] = {"revision": 1, "run_id": "run-one", "status": "running", "started_at": NOW, "elapsed_seconds": 0}
    def failure(_events):
        raise ValueError("untrusted-sensitive-error")
    with pytest.raises(RuntimeError) as error:
        pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
                     client=object(), classifier=failure, clock=lambda: NOW)
    assert "untrusted-sensitive-error" not in str(error.value)
    assert docs["status_pipeline"]["status"] == "error"
    assert len(lake.data["bronze"]) == 1 and lake.data["silver"] == {}
    assert "04_gold/prisma/current.json" not in objects.data
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake, client=object(),
                            classifier=lambda events: [normalize_event(event) for event in events], clock=lambda: NOW)
    assert len(snapshot["evidence"]) == len(lake.data["bronze"]) == len(lake.data["silver"]) == 1
    assert snapshot["evidence"][0]["source_id"].startswith("run-one:")


def test_queued_manual_run_cannot_bypass_persisted_x_rate_limit(runtime):
    _, docs, _, lake, objects = runtime
    docs["checkpoint_x"] = {"retry_at": NOW + 90, "cursor": {"next_token": "page2"}, "revision": 1}
    source = {**default_source("x"), "mode": "real", "credential_configured": True}
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "run"}, None, NOW, object())
    assert docs["status_x"]["status"] == "rate_limited"
    assert docs["checkpoint_x"]["cursor"] == {"next_token": "page2"}
    assert docs["status_x"]["next_due"] == pipeline.utc_text(NOW + 90)


def test_reset_paused_at_zero_has_no_new_synthetic_evidence(runtime):
    _, docs, _, lake, objects = runtime
    docs["simulation"] = {"status": "paused", "elapsed_seconds": 0, "run_id": "new-reset-run"}
    snapshot = pipeline.run(None, None, CONFIG, connection=object(), objects=objects, lake=lake,
        client=object(), classifier=lambda events: [normalize_event(item) for item in events], clock=lambda: NOW)
    assert snapshot["evidence"] == []


def test_old_tick_does_not_clear_newer_pending_request(runtime):
    _, docs, _, _, _ = runtime
    docs["status_x"] = {"requested_action": "test", "request_id": "new-request", "status": "queued"}
    pipeline._status(object(), "x", {"status": "ready", "last_run_at": pipeline.utc_text(NOW)}, "old-request")
    assert docs["status_x"]["requested_action"] == "test" and docs["status_x"]["status"] == "queued"
    assert pipeline._due({"configuration_revision": 1, "next_due": pipeline.utc_text(NOW + 300)}, 2, NOW)


def test_x_received_count_tracks_successful_pages_and_survives_later_error(runtime, monkeypatch):
    _, docs, _, lake, objects = runtime
    source = {**default_source("x"), "mode": "real", "credential_configured": True}
    pages = [([simulation_events(0)[0]], {"next_token": "page2"}), ([], {"since_id": "30"})]
    monkeypatch.setattr(pipeline, "fetch_page", lambda *_args, **_kwargs: pages.pop(0))
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {}, lambda **_: "fake-token", NOW, object())
    assert docs["status_x"]["last_received_count"] == 1
    def fail(*_args, **_kwargs):
        raise pipeline.XFailure("access_denied")
    monkeypatch.setattr(pipeline, "fetch_page", fail)
    pipeline.poll_source(object(), objects, lake, CONFIG, source, {"requested_action": "run"}, lambda **_: "fake-token", NOW, object())
    assert docs["status_x"]["last_received_count"] == 1 and docs["status_x"]["last_error"] == "access_denied"


def test_classifier_closes_output_contract_and_preserves_simulation_provenance():
    event = normalize_event(simulation_events(0)[0])
    label = {"id": event["id"], "category": "inundacion", "locality": "Kennedy", "severity": "high", "confidence": 0.9, "mode": "real"}
    class Model:
        def chat(self, _request):
            text = json.dumps({"items": [label]})
            return SimpleNamespace(data=SimpleNamespace(chat_response=SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=[SimpleNamespace(text=text)]))])))
    result = classify([event], CONFIG, client=Model())
    assert result[0]["mode"] == "simulation"
    assert result[0]["classification_method"] == "oci_genai:model"
    label["confidence"] = 8
    with pytest.raises(ValueError, match="confidence"):
        classify([event], CONFIG, client=Model())
    label.update(locality="Sin localizar", category="incendio", confidence=0.2)
    result = classify([event], CONFIG, client=Model())[0]
    assert result["category"] == "incendio" and result["lat"] is None and result["lon"] is None


def test_runtime_auth_constructs_ordinary_signer_sdk_clients_without_key_files():
    import oci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    credentials = {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..test", "fingerprint": ":".join(["aa"] * 16), "private_key": key}
    config, signed = runtime_auth(lambda name, key: credentials[key], "us-ashburn-1")
    assert "key_file" not in config and config["key_content"] == key
    assert config["region"] == "us-ashburn-1"
    # Constructing clients exercises OCI's config validation without sending any request.
    oci.object_storage.ObjectStorageClient(config, signer=signed)
    oci.generative_ai_inference.GenerativeAiInferenceClient(config, signer=signed)
