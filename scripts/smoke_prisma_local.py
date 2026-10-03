"""Exercise Docker source capture, CSV publication and authenticated viewer; leave X disabled."""
import argparse
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:18081")
    parser.add_argument("--access-file", type=Path, default=Path(".tmp/prisma-local-access.json"))
    args = parser.parse_args()
    access = json.loads(args.access_file.read_text())
    target = urlsplit(args.url)
    if target.scheme not in {"http", "https"} or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("This smoke runner is restricted to loopback fixture services")
    with httpx.Client(base_url=args.url, trust_env=False, timeout=30) as client:
        assert client.get("/api/prisma/snapshot").status_code == 401
        assert client.get("/gods-eye-view/").status_code == 401
        for legacy, canonical in (("/prisma", "/gods-eye-view/"), ("/prisma/", "/gods-eye-view/"),
                                  ("/admin/prisma", "/admin/gods-eye-view"),
                                  ("/local/prisma/login", "/local/gods-eye-view/login"),
                                  ("/local/prisma/workspace", "/local/gods-eye-view/workspace")):
            redirected = client.get(legacy)
            assert redirected.status_code in {301, 308}
            assert urlsplit(redirected.headers["location"]).path == canonical
            assert urlsplit(urljoin(args.url, redirected.headers["location"])).netloc == target.netloc
        login = client.post("/api/admin/login", json=access)
        assert login.status_code == 204, f"Login failed: {login.status_code}"
        state = client.get("/api/admin/prisma/sources")
        state.raise_for_status()
        assert len(state.json()["sources"]) == 4
        assert "bearer_token" not in state.text
        source = next(item for item in state.json()["sources"] if item["platform"] == "x")
        assert source["mode"] == "simulation", "Use the synthetic-only fixture for this smoke test"
        endpoint = "/api/admin/prisma/sources/x"
        original = {name: source[name] for name in ("mode", "query", "interval_minutes")}
        before = client.get("/api/prisma/snapshot")
        before.raise_for_status()
        existing = {event["id"] for event in before.json()["evidence"]}
        landing_count = state.json()["capture_summary"]["landing_count"]
        try:
            client.put(endpoint, json={"enabled": False}).raise_for_status()
            client.put(endpoint, json={"enabled": True, "mode": "simulation", "interval_minutes": 5,
                "query": "#bogota #inundacion\n#colombia #incendio\n#desastre"}).raise_for_status()
            started = client.post(endpoint + "/run")
            started.raise_for_status()
            assert started.json()["source"]["capture_running"] is True
            assert started.json()["source"]["last_received_count"] >= 1
            captured = client.get("/api/admin/prisma/sources")
            captured.raise_for_status()
            summary = captured.json()["capture_summary"]
            assert summary["landing_count"] > landing_count and summary["last_landing_key"].endswith(".csv")
            response = client.get("/api/prisma/snapshot")
            response.raise_for_status()
            snapshot = response.json()
            assert snapshot["runtime"] == "local_fixture"
            published = {event["id"] for event in snapshot["evidence"]}
            added = [event for event in snapshot["evidence"] if event["id"] not in existing]
            assert existing <= published and added
            assert all(event["is_simulated"] is True for event in added)
            assert any(event["platform"] == "x" and event["locality"] == "Kennedy" for event in added)
            viewer = client.get("/gods-eye-view/")
            viewer.raise_for_status()
            assert "Territorial Control" in viewer.text
            chat = client.post("/api/prisma/chat", json={"question": "¿Qué incidentes hay en Kennedy?", "version": snapshot["version"]})
            chat.raise_for_status()
            answer = chat.json()
            assert answer["runtime"] == "local_fixture" and answer["evidence_ids"]
            assert set(answer["evidence_ids"]) <= published
            stale = client.post("/api/prisma/chat", json={"question": "Muéstrame incidentes", "version": "stale"})
            assert stale.status_code == 409
        finally:
            stopped = client.put(endpoint, json={**original, "enabled": False})
            stopped.raise_for_status()
            assert stopped.json()["capture_running"] is False and stopped.json()["enabled"] is False
        retained = client.get("/api/prisma/snapshot")
        retained.raise_for_status()
        assert published <= {event["id"] for event in retained.json()["evidence"]}
        print(json.dumps({"status": "passed", "runtime": "local_fixture", "sources": 4,
            "incidents": len(snapshot["incidents"]), "evidence": len(snapshot["evidence"]),
            "checks": ["unauthorized", "legacy_redirects", "login", "source_run", "csv_landing", "snapshot_publication",
                       "viewer_proxy", "chat_evidence", "stale_version", "source_disable", "evidence_preserved"]}))


if __name__ == "__main__":
    main()
