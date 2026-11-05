"""Write-only provider settings; encrypted on the viewer's private persistent volume."""
import base64
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROVIDERS = {
    "google-maps": ("Google Maps", ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_API_KEY")),
    "openai": ("OpenAI", ("OPENAI_API_KEY",)),
    "aisstream": ("AISStream", ("AISSTREAM_API_KEY",)),
    "firms": ("NASA FIRMS", ("FIRMS_MAP_KEY",)),
    "tomtom": ("TomTom", ("TOMTOM_API_KEY",)),
    "cesium-ion": ("Cesium ion", ("CESIUM_ION_TOKEN",)),
    "opensky": ("OpenSky", ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET")),
    "launch-library": ("Launch Library", ("LL2_API_TOKEN",)),
}
ENV_KEYS = frozenset(name for _, names in PROVIDERS.values() for name in names)
BROWSER_KEYS = frozenset({"GOOGLE_MAPS_API_KEY", "CESIUM_ION_TOKEN"})
LABELS = {"GOOGLE_MAPS_API_KEY": "Browser API key", "GOOGLE_MAPS_SERVER_API_KEY": "Server API key (optional)",
          "OPENSKY_CLIENT_ID": "Client ID", "OPENSKY_CLIENT_SECRET": "Client secret", "CESIUM_ION_TOKEN": "Access token"}
# ponytail: one bridge process owns writes; add a process lock before enabling multiple uvicorn workers.
LOCK = threading.RLock()
OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def directory():
    return Path(os.getenv("GEV_PROVIDER_SETTINGS_DIR", "/app/.upstream/.gev-cache/provider-settings"))


def file_signature():
    try:
        stat = (directory() / "settings.json").stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_key():
    folder = directory()
    if folder.is_symlink():
        raise ValueError("Provider storage must be a private directory")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    target = folder / "key.pem"
    if target.is_symlink():
        raise ValueError("Provider storage key is invalid")
    if not target.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        atomic_write(target, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    target.chmod(0o600)
    key = serialization.load_pem_private_key(target.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("Provider storage key is invalid")
    return key


def unseal(envelope, key):
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "wrapped_key_b64", "nonce_b64", "ciphertext_b64"} or envelope["schema_version"] != 2:
        raise ValueError("Invalid provider envelope")
    if any(not isinstance(envelope[name], str) or len(envelope[name]) > 32768 for name in ("wrapped_key_b64", "nonce_b64", "ciphertext_b64")):
        raise ValueError("Invalid provider envelope")
    wrapped, nonce, ciphertext = (base64.b64decode(envelope[name], validate=True) for name in ("wrapped_key_b64", "nonce_b64", "ciphertext_b64"))
    if len(nonce) != 12:
        raise ValueError("Invalid provider envelope")
    data_key = key.decrypt(wrapped, OAEP)
    result = json.loads(AESGCM(data_key).decrypt(nonce, ciphertext, None))
    if not isinstance(result, dict):
        raise ValueError("Invalid provider payload")
    return result


def seal(payload, key):
    data_key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    return {"schema_version": 2,
            "wrapped_key_b64": base64.b64encode(key.public_key().encrypt(data_key, OAEP)).decode(),
            "nonce_b64": base64.b64encode(nonce).decode(),
            "ciphertext_b64": base64.b64encode(AESGCM(data_key).encrypt(nonce, json.dumps(payload).encode(), None)).decode()}


def validate_values(values, allowed):
    if not isinstance(values, dict) or set(values) - set(allowed):
        raise ValueError("Only this provider's credential fields are accepted")
    clean = {}
    for name, value in values.items():
        if value is None:
            clean[name] = None
        elif isinstance(value, str) and re.fullmatch(r"[!-~]{1,512}", value.strip()):
            clean[name] = value.strip()
        else:
            raise ValueError("Use a credential of 1 to 512 printable characters without spaces")
    return clean


def stored(key=None):
    target = directory() / "settings.json"
    if target.is_symlink():
        raise ValueError("Provider settings file is invalid")
    if not target.exists():
        return {"revision": "environment", "values": {}}
    if target.stat().st_size > 65536:
        raise ValueError("Provider settings file is too large")
    # A missing private key must never replace the key of an existing encrypted document.
    if not (directory() / "key.pem").is_file():
        raise ValueError("Provider storage key is missing")
    result = unseal(json.loads(target.read_bytes()), key or private_key())
    if set(result) != {"revision", "values"} or not isinstance(result["revision"], str) or not re.fullmatch(r"[a-f0-9]{32}", result["revision"]):
        raise ValueError("Provider settings document is invalid")
    result["values"] = validate_values(result["values"], ENV_KEYS)
    return result


def environment(document):
    return {**os.environ, **{name: value or "" for name, value in document["values"].items()},
            "GEV_PROVIDER_REVISION": document["revision"]}


def metadata():
    with LOCK:
        document = stored()
        key, effective = private_key(), environment(document)
        providers = []
        for identifier, (label, names) in PROVIDERS.items():
            fields = [{"id": name, "label": LABELS.get(name, "API key"), "configured": bool(effective.get(name)),
                       "secret": True, "client_exposed": name in BROWSER_KEYS,
                       "optional": name == "GOOGLE_MAPS_SERVER_API_KEY"} for name in names]
            providers.append({"id": identifier, "label": label,
                              "configured": all(field["configured"] for field in fields if not field["optional"]), "fields": fields})
        return {"revision": document["revision"], "public_key": key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
            "providers": providers, "apply_mode": "native_worker_reload", "reload_required": False,
            "browser_reload_required": True}


class RevisionConflict(ValueError):
    pass


def candidate(identifier, envelope):
    if identifier not in PROVIDERS:
        raise ValueError("Unknown provider")
    document = stored()
    key = private_key()
    payload = unseal(envelope, key)
    if set(payload) != {"provider_id", "expected_revision", "values"} or payload["provider_id"] != identifier:
        raise ValueError("The provider payload is invalid")
    if payload["expected_revision"] != document["revision"]:
        raise RevisionConflict("Provider settings changed; refresh before saving or testing")
    values = validate_values(payload["values"], PROVIDERS[identifier][1])
    return key, {"revision": document["revision"], "values": {**document["values"], **values}}


def save(identifier, envelope):
    with LOCK:
        key, document = candidate(identifier, envelope)
        if document["values"] != stored(key)["values"]:
            document["revision"] = uuid4().hex
            atomic_write(directory() / "settings.json", json.dumps(seal(document, key)).encode())
        return document["revision"]


def test_environment(identifier, envelope):
    with LOCK:
        _, document = candidate(identifier, envelope)
        return environment(document)
