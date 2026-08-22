from __future__ import annotations

import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from .policy import Principal


class TokenizationError(RuntimeError):
    pass


class TokenStore(Protocol):
    def save_token(self, token_id: str, ciphertext: str, key_version: str) -> None: ...
    def load_token(self, token_id: str) -> tuple[str, str]: ...
    def audit(self, event: dict[str, Any]) -> None: ...


class TokenCipher(Protocol):
    def encrypt(self, plaintext: bytes) -> tuple[str, str]: ...
    def decrypt(self, ciphertext: str, key_version: str) -> bytes: ...


class VaultTokenizer:
    def __init__(self, store: TokenStore, cipher: TokenCipher) -> None:
        self.store = store
        self.cipher = cipher

    def tokenize(self, value: str, principal: Principal) -> str:
        plaintext = value.encode("utf-8")
        if len(plaintext) > 16_384:
            raise TokenizationError("The value is too large to tokenize.")
        token_id = f"aidptok_v1_{uuid.uuid4().hex}"
        try:
            ciphertext, key_version = self.cipher.encrypt(plaintext)
            self.store.save_token(token_id, ciphertext, key_version)
        except Exception as exc:
            raise TokenizationError("OCI Vault tokenization failed.") from exc
        self._audit(principal, "TOKENIZED", token_id, key_version)
        return token_id

    def detokenize(self, token_id: str, principal: Principal) -> str:
        try:
            ciphertext, key_version = self.store.load_token(token_id)
            plaintext = self.cipher.decrypt(ciphertext, key_version)
            value = plaintext.decode("utf-8")
        except KeyError:
            raise
        except Exception as exc:
            raise TokenizationError("OCI Vault detokenization failed.") from exc
        self._audit(principal, "DETOKENIZED", token_id, key_version)
        return value

    def _audit(self, principal: Principal, decision: str, token_id: str, key_version: str) -> None:
        self.store.audit({
            "principal": hashlib.sha256(principal.subject.encode("utf-8")).hexdigest(),
            "decision": decision,
            "query_id": token_id,
            "affected_columns": [],
            "policy_version": key_version,
        })


@dataclass(frozen=True)
class KmsSettings:
    key_id: str
    crypto_endpoint: str

    @classmethod
    def from_environment(cls) -> "KmsSettings":
        key_id = os.environ.get("GOVERNANCE_TOKENIZATION_KEY_OCID", "").strip()
        endpoint = os.environ.get("GOVERNANCE_TOKENIZATION_CRYPTO_ENDPOINT", "").strip().rstrip("/")
        if not key_id.startswith("ocid1.key.") or not endpoint.startswith("https://"):
            raise TokenizationError("OCI Vault tokenization key and crypto endpoint are required.")
        return cls(key_id, endpoint)


class OciKmsCipher:
    """Encrypt and decrypt through OCI KMS; key bytes never enter the gateway."""

    def __init__(self, settings: KmsSettings) -> None:
        self.settings = settings
        self._client: Any | None = None
        self._lock = Lock()

    def encrypt(self, plaintext: bytes) -> tuple[str, str]:
        import oci

        response = self._get_client().encrypt(oci.key_management.models.EncryptDataDetails(
            key_id=self.settings.key_id,
            plaintext=base64.b64encode(plaintext).decode("ascii"),
        ))
        ciphertext = str(getattr(response.data, "ciphertext", "") or "")
        key_version = str(getattr(response.data, "key_version_id", "") or self.settings.key_id)
        if not ciphertext:
            raise TokenizationError("OCI KMS returned no ciphertext.")
        return ciphertext, key_version

    def decrypt(self, ciphertext: str, key_version: str) -> bytes:
        del key_version
        import oci

        response = self._get_client().decrypt(oci.key_management.models.DecryptDataDetails(
            key_id=self.settings.key_id,
            ciphertext=ciphertext,
        ))
        plaintext = str(getattr(response.data, "plaintext", "") or "")
        if not plaintext:
            raise TokenizationError("OCI KMS returned no plaintext.")
        return base64.b64decode(plaintext, validate=True)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                import oci

                signer = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
                self._client = oci.key_management.KmsCryptoClient(
                    config={}, signer=signer, service_endpoint=self.settings.crypto_endpoint,
                )
        return self._client
