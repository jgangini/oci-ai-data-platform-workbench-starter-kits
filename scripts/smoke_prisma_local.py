"""Exercise the real Docker nginx/admin/viewer boundary without screenshots."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

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
        login = client.post("/api/admin/login", json=access)
        assert login.status_code == 204, f"Login failed: {login.status_code}"
        state = client.get("/api/admin/prisma/sources")
        state.raise_for_status()
        assert len(state.json()["sources"]) == 4
        assert "bearer_token" not in state.text
        response = client.post("/api/admin/prisma/simulation", json={"action": "start"})
        response.raise_for_status()
        response = client.get("/api/prisma/snapshot")
        response.raise_for_status()
        snapshot = response.json()
        assert snapshot["runtime"] == "local_fixture"
        assert all(event["mode"] == "simulation" for event in snapshot["evidence"])
        viewer = client.get("/prisma/")
        viewer.raise_for_status()
        assert "Territorial Control" in viewer.text
        chat = client.post("/api/prisma/chat", json={"question": "¿Qué incidentes hay en Bogotá?", "version": snapshot["version"]})
        chat.raise_for_status()
        answer = chat.json()
        assert answer["runtime"] == "local_fixture" and "SIMULADO" in answer["answer"]
        assert set(answer["evidence_ids"]) <= {event["id"] for event in snapshot["evidence"]}
        stale = client.post("/api/prisma/chat", json={"question": "Muéstrame incidentes", "version": "stale"})
        assert stale.status_code == 409
        response = client.post("/api/admin/prisma/simulation", json={"action": "pause"})
        response.raise_for_status()
        print(json.dumps({"status": "passed", "runtime": "local_fixture", "sources": 4,
            "incidents": len(snapshot["incidents"]), "evidence": len(snapshot["evidence"]),
            "checks": ["unauthorized", "login", "source_controls", "simulation", "viewer_proxy", "chat_evidence", "stale_version", "pause"]}))


if __name__ == "__main__":
    main()
