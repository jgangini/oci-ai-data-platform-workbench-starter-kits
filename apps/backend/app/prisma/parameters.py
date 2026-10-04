"""Admin-only facade to the private viewer's encrypted provider settings."""
from __future__ import annotations

import base64
import json
import os
import re
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, HTTPException, Request

from ..security import RateLimiter


def seal_parameters(public_key: str, payload: dict) -> dict:
    try:
        key = serialization.load_pem_public_key(public_key.encode())
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError()
        data_key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        wrapped = key.encrypt(data_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        ciphertext = AESGCM(data_key).encrypt(nonce, json.dumps(payload).encode(), None)
        return {"schema_version": 2, **{name: base64.b64encode(value).decode() for name, value in (
            ("wrapped_key_b64", wrapped), ("nonce_b64", nonce), ("ciphertext_b64", ciphertext))}}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(503, "The viewer credential store is unavailable") from None


async def viewer_parameters(request: Request, method="GET", provider_id=None, *, test=False):
    base = os.environ.get("PRISMA_VIEWER_URL", "").rstrip("/")
    if not base:
        raise HTTPException(503, "The private viewer is not configured in this deployment")
    path = "/internal/provider-parameters"
    headers = {"Cookie": request.headers.get("cookie", "")}
    if origin := request.headers.get("origin"):
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise HTTPException(403, "Use the administration page to configure providers") from None
        if (parsed.scheme not in {"http", "https"} or parsed.hostname != request.url.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise HTTPException(403, "Use the administration page to configure providers")
        headers["X-GEV-Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=5), trust_env=False, follow_redirects=False) as client:
            metadata = parameter_response(await client.get(base + path, headers=headers))
            if method == "GET":
                metadata.pop("public_key", None)
                return metadata
            payload = await parameter_payload(request, provider_id, metadata)
            sealed = seal_parameters(metadata.get("public_key"), {"provider_id": provider_id, **payload})
            response = await client.request(method, base + path + "/" + provider_id + ("/test" if test else ""),
                                            headers=headers, json=sealed)
            result = parameter_response(response)
            result.pop("public_key", None)
            return result
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "The viewer configuration service is unavailable; try again shortly") from None


def parameter_response(response):
    if response.status_code >= 400:
        messages = {401: "Administrator session required", 403: "Administrator access is required",
                    404: "This viewer release does not support the requested provider settings",
                    409: "Provider settings changed. Refresh the configuration before saving again",
                    422: "The provider credentials or configuration request are invalid",
                    429: "Too many provider tests; try again shortly"}
        raise HTTPException(response.status_code if response.status_code in messages else 503,
                            messages.get(response.status_code, "The viewer configuration service is unavailable"))
    if len(response.content) > 131072:
        raise ValueError()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError()
    return result


async def parameter_payload(request, provider_id, metadata):
    # Parse without Pydantic's input echo: invalid credentials must never appear in a 422 response.
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError()
        if request.headers.get("sec-fetch-site", "") not in {"", "same-origin", "none"}:
            raise HTTPException(403, "Use the administration page to configure providers")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise ValueError()
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {"expected_revision", "values"}:
            raise ValueError()
        revision, values = payload["expected_revision"], payload["values"]
        provider = next((p for p in metadata["providers"] if p["id"] == provider_id), None)
        if provider is None or not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", revision):
            raise ValueError()
        fields = {field["id"] for field in provider["fields"]}
        if not isinstance(values, dict) or not set(values) <= fields or any(
            not isinstance(v, str) or not v.strip() or len(v) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in v.strip())
            for v in values.values()
        ):
            raise ValueError()
        return {"expected_revision": revision, "values": {k: v.strip() for k, v in values.items()}}
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "The provider credentials or configuration request are invalid") from None


def mount_parameters(app, require_admin):
    router = APIRouter(dependencies=[Depends(require_admin)])
    limiter = RateLimiter(10, 60)

    @router.get("/api/admin/prisma/parameters")
    async def parameters(request: Request):
        return await viewer_parameters(request)

    @router.put("/api/admin/prisma/parameters/{provider_id}")
    async def save(request: Request, provider_id: str):
        return await viewer_parameters(request, "PUT", provider_id)

    @router.post("/api/admin/prisma/parameters/{provider_id}/test")
    async def test(request: Request, provider_id: str, principal: str = Depends(require_admin)):
        retry = limiter.consume(principal)
        if retry:
            raise HTTPException(429, "Too many provider tests; try again shortly", headers={"Retry-After": str(retry)})
        return await viewer_parameters(request, "POST", provider_id, test=True)

    app.include_router(router)
