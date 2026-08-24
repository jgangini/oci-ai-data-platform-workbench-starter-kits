# Operations and local run modes

Release v1.0.2 has three operational modes that matter for development and validation.

## 1. Full OCI deployment
The top-level README describes the Deploy Studio package that provisions OCI resources, creates the VM, and wires Identity Domains and AIDP together. This is the production-like path and the one that matters most for lab operators.

Deploy Studio keeps the uploaded operator config/key in private temporary inputs. During post-apply, the VM publishes only a temporary RSA 3072-bit public key through Run Command. The hook encrypts the operator profile with AES-256-GCM, wraps the data key with RSA-OAEP/SHA-256, and places the envelope at the exact `.bootstrap/operator-credentials.json` object. The VM validates the expected operator OCID and API-key fingerprint, writes the profile atomically with mode `0600`, deletes and verifies removal of the object, and removes the temporary RSA key. Post-apply does not report success until the object has been consumed and strict health passes.

The uploaded OCI API key must be an unencrypted RSA PEM. Repository preflight rejects passphrases, encrypted PEMs, invalid files, and non-RSA keys before provisioning without logging their contents or paths.

Runtime Identity Domains and AIDP calls reuse that operator profile. The lab creates no dedicated provisioner identity, `AIDP_LAB_PROVISIONER`, additional API key, Vault/KMS secret, or OAuth client. The instance principal is authorized only for the exact bootstrap object and is not a runtime fallback.

## 2. Local-only development profile
[`docker/docker-compose.dev.yml`](../docker/docker-compose.dev.yml) runs the same image with a local `.env.dev` file and localhost-only ports. It uses in-memory users instead of OCI Identity Domains, so it is good for UI and backend iteration but cannot prove cloud permissions.

Typical flow from the README:
```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```
Stop it with:
```powershell
docker compose -f docker/docker-compose.dev.yml down
```

## 3. OCI-connected local profile
[`docker/docker-compose.oci-local.yml`](../docker/docker-compose.oci-local.yml) runs the same image against live OCI resources on localhost-only ports. It is intentionally not a replacement for the VM.

The bootstrap helper [`scripts/bootstrap_local_oci_env.py`](../scripts/bootstrap_local_oci_env.py) discovers the live lab resources and writes a local `.env` without printing credentials. It can also self-check its env rendering and escaping behavior.

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem> --access-email <deployment-email.html>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml config --quiet
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

The generated `.env` includes `IDENTITY_DOMAIN_URL`, `OCI_CONFIG_FILE=/etc/aidp-lab/oci/config`, `OBJECTSTORAGE_NAMESPACE`, `BUCKET_NAME`, and two host paths used only for Compose binds. With `--access-email`, it also derives the same administrator and registration verifiers used by the deployment; plaintext access values are neither printed nor written to the generated environment. The helper also writes a sanitized, non-secret `.tmp/oci-local/<suffix>/config` whose `key_file` points inside the container. Compose mounts that config and the original operator key as read-only files; it never copies or prints the private key. Identity Domains and AIDP requests are signed with that profile; the container has no OAuth or instance-principal fallback. OCI-local rejects a config passphrase instead of copying it, so the supplied operator API key must be unencrypted.

## Operational scripts
- [`scripts/arch-preflight.ps1`](../scripts/arch-preflight.ps1) — preflight checks before substantial changes
- [`scripts/arch-postflight.ps1`](../scripts/arch-postflight.ps1) — follow-up checks after changes
- [`scripts/bootstrap_local_oci_env.py`](../scripts/bootstrap_local_oci_env.py) — generate an OCI-backed local env file

The repo root README says the architecture scripts should bracket non-trivial edits. The scripts themselves are part of the operational contract, so changes to runtime behavior should usually include a quick pass through those checks.

## What to watch out for
- Do not use the OCI-connected local profile as a substitute for the VM; it has no restart policy and binds only to `127.0.0.1`.
- Never commit generated `.env` files, keys, state, or certificates.
- `.tmp/` is ignored because it holds only the generated non-secret OCI-local config; the original private key remains at the operator-supplied path.
- A successful OCI bootstrap leaves no `.bootstrap/operator-credentials.json` object and no temporary RSA key. A surviving object means credential installation did not complete and the deployment must remain failed.
- The local development profile uses sample hashes and fake users; it does not validate real OCI IAM or API signing.
- The HTTPS endpoints are self-signed in the lab environment, so browser trust warnings are expected.

## Useful checks
- Backend tests are the fastest way to validate registration/login/session behavior.
- Frontend security tests protect against accidental browser-side secret persistence.
- Terraform validation is the best first check for infrastructure edits.
- `python scripts/bootstrap_local_oci_env.py --self-check` validates env escaping and the sanitized container-key path without contacting OCI.
