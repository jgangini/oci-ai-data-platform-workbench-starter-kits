import base64

import pytest

from governance_gateway.policy import Principal
from governance_gateway.store import MemoryControlStore
from governance_gateway.tokenization import KmsSettings, TokenizationError, VaultTokenizer


class Cipher:
    def encrypt(self, plaintext: bytes) -> tuple[str, str]:
        return base64.b64encode(plaintext).decode("ascii"), "key-version-1"

    def decrypt(self, ciphertext: str, key_version: str) -> bytes:
        assert key_version == "key-version-1"
        return base64.b64decode(ciphertext)


def test_tokenization_round_trip_stores_only_ciphertext_and_audits() -> None:
    store = MemoryControlStore()
    tokenizer = VaultTokenizer(store, Cipher())
    principal = Principal("service", scopes=frozenset({"governance.tokenize", "governance.detokenize"}))
    token = tokenizer.tokenize("secret@example.com", principal)
    assert token.startswith("aidptok_v1_")
    assert "secret@example.com" not in repr(store._tokens)
    assert tokenizer.detokenize(token, principal) == "secret@example.com"
    assert [event["decision"] for event in store.audit_events] == ["TOKENIZED", "DETOKENIZED"]


def test_tokenization_rejects_values_larger_than_the_gateway_contract() -> None:
    with pytest.raises(TokenizationError, match="too large"):
        VaultTokenizer(MemoryControlStore(), Cipher()).tokenize("x" * 16_385, Principal("service"))


def test_kms_settings_require_a_vault_key_and_https_crypto_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOVERNANCE_TOKENIZATION_KEY_OCID", "not-a-key")
    monkeypatch.setenv("GOVERNANCE_TOKENIZATION_CRYPTO_ENDPOINT", "http://kms.example")
    with pytest.raises(TokenizationError, match="required"):
        KmsSettings.from_environment()
