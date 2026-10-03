"""OCI-signed gateway call with bounded, assistant-only output parsing."""
import hashlib
import hmac
import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from .area import parse_bbox


def assistant_texts(value, assistant=False):
    if isinstance(value, str):
        return [value] if assistant else []
    if isinstance(value, list):
        return [text for item in value for text in assistant_texts(item, assistant)]
    if not isinstance(value, dict):
        return []
    role, kind = str(value.get("role", "")).lower(), str(value.get("type", "")).lower()
    if role in {"user", "human", "tool"} or kind in {"trace", "reasoning", "tool_call", "function_call"} or kind.startswith("input_"):
        return []
    assistant = assistant or role in {"assistant", "ai"}
    texts = [value[key] for key in ("output_text", "answer", "text")
             if assistant and isinstance(value.get(key), str)]
    for key in ("output", "content", "message", "messages", "response", "result"):
        if key in value:
            texts.extend(assistant_texts(value[key], assistant))
    return list(dict.fromkeys(texts))


def validated_filters(filters):
    if not isinstance(filters, dict) or set(filters) - {"locality", "platform", "category", "severity", "mode", "date_from", "date_to", "bbox"}:
        raise HTTPException(422, "Invalid filters")
    if any(not isinstance(v, str) or len(v) > 100 for v in filters.values()):
        raise HTTPException(422, "Invalid filters")
    try:
        dates = [datetime.fromisoformat(filters[name].replace("Z", "+00:00")) if filters.get(name) else None
                 for name in ("date_from", "date_to")]
        if any(value and value.tzinfo is None for value in dates) or all(dates) and dates[0] > dates[1]:
            raise ValueError("Invalid period")
    except ValueError as exc:
        raise HTTPException(422, "Invalid period") from exc
    try:
        parse_bbox(filters.get("bbox"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return filters


def query_content(payload, snapshot):
    question = str(payload.get("question", "")).strip()
    if not 1 <= len(question) <= 2000 or payload.get("version") != snapshot.get("version"):
        raise HTTPException(409, "Invalid question or outdated publication")
    filters = validated_filters(payload.get("filters", {}))
    incidents = {item["id"] for item in snapshot.get("incidents", [])}
    if payload.get("incident_id") and payload["incident_id"] not in incidents:
        raise HTTPException(422, "Unknown incident")
    return json.dumps({"question": question, "context": {"version": snapshot["version"],
        "published_at": snapshot.get("published_at"), "incident_id": payload.get("incident_id"), "filters": filters}}, ensure_ascii=False)


def scoped_session(payload, cookie, key):
    try:
        session = str(UUID(str(payload.get("session_id"))))
    except ValueError as exc:
        raise HTTPException(422, "Invalid session") from exc
    digest = hmac.digest(key, (cookie + "\0" + session).encode(), "sha256")
    return str(UUID(bytes=digest[:16], version=4))


def checked_endpoint(endpoint, region):
    url = urlsplit(endpoint)
    expected_host = f"gateway.aidp.{region}.oci.oraclecloud.com"
    if (url.scheme != "https" or url.netloc != expected_host or url.query or url.fragment
        or not re.fullmatch(r"/agentendpoint/[A-Za-z0-9_.-]+/chat", url.path)):
        raise HTTPException(503, "Invalid AIDP endpoint")
    return endpoint


def invoke(client, endpoint, payload, cookie, key, snapshot):
    content = query_content(payload, snapshot)
    session = scoped_session(payload, cookie, key)
    endpoint = checked_endpoint(endpoint, client.settings.aidp_region)
    response = client.session.post(endpoint, auth=client.signer, timeout=(10, 90),
        headers={"x-session-id": session}, json={"sessionKey": session, "isStreamEnabled": False, "trace": False,
        "input": [{"role": "User", "content": [{"type": "INPUT_TEXT", "text": content}]}]})
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("status", "completed") != "completed":
        raise HTTPException(502, "The agent response did not complete")
    texts = assistant_texts(body)
    if not texts:
        raise HTTPException(502, "The agent did not return a response")
    result = json.loads(texts[-1])
    evidence = {item["id"] for item in snapshot.get("evidence", [])}
    if (not isinstance(result, dict) or result.get("version") != snapshot["version"]
        or not isinstance(result.get("evidence_ids"), list)
        or any(not isinstance(ref, str) or ref not in evidence for ref in result["evidence_ids"])):
        raise HTTPException(502, "The response does not match the evidence")
    return result
