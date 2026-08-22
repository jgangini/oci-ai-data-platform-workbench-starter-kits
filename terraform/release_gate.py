from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_PROJECT = "oci-aidp-cloud-migration-lab"
EXPECTED_REPOSITORIES = {
    "https://github.com/jgangini/oci-aidp-cloud-migration-lab",
    "https://github.com/jgangini/oci-aidp-cloud-migration-lab.git",
}
EXPECTED_REFS = frozenset({"v2.1.0"})

_SHA = re.compile(r"^[0-9a-f]{40}$")
_COMPARTMENT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_OCI_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[1-9][0-9]*$")
_ALLOWED_IDENTITY_DOMAIN_GROUPS = frozenset({"developers", "pending"})
_IDENTITY_DOMAIN_RESOURCE = re.compile(
    r'resource\s+"oci_identity_domains_(user|group|grant)"\s+"([^"]+)"',
    re.IGNORECASE,
)
_SOURCE_PATTERNS = (
    ("lab-2/lab-3 reference", re.compile(r"oci-aidp-cloud-migration-lab-(?:2|3)(?![0-9])", re.IGNORECASE)),
    (
        "explicit OSCS/OpenSearch resource",
        re.compile(
            r"resource\s+\"[^\"]*(?:opensearch|oscs)[^\"]*\"|oci[._-]opensearch|/opensearch(?:/|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "external AIDP volume",
        re.compile(
            r"volumeType\s*[\"']?\s*[:=]\s*[\"']EXTERNAL[\"']|"
            r"resource\s+\"oci_ai_data_platform[^\"]*volume[^\"]*\"",
            re.IGNORECASE,
        ),
    ),
    (
        "OCI Vault or customer-managed KMS resource",
        re.compile(r'resource\s+"(?:oci_kms_|oci_vault_)[^"]+"', re.IGNORECASE),
    ),
    (
        "Identity Domains OAuth client",
        re.compile(r'resource\s+"oci_identity_domains_app"', re.IGNORECASE),
    ),
    ("optional VNIC policy", re.compile(r"\bmanage\s+vnics\b", re.IGNORECASE)),
    ("optional subnet policy", re.compile(r"\buse\s+subnets\b", re.IGNORECASE)),
    ("optional NSG policy", re.compile(r"\buse\s+network-security-groups\b", re.IGNORECASE)),
    (
        "optional Object Storage deletion policy",
        re.compile(
            r"allow\s+service\s+objectstorage-[^\s]+\s+to\s+manage\s+object-family\b",
            re.IGNORECASE,
        ),
    ),
    ("AIDP_LAB_PROVISIONER literal", re.compile(r"\bAIDP_LAB_PROVISIONER\b", re.IGNORECASE)),
)


def _compartment_name(context: dict[str, Any]) -> str:
    value = context.get("compartment", "")
    name = value.strip() if isinstance(value, str) else ""
    if not _COMPARTMENT_NAME.fullmatch(name):
        raise ValueError("compartment name must use 1-100 letters, numbers, periods, hyphens, or underscores")
    return name


def _compartment_mode(context: dict[str, Any]) -> str:
    value = context.get("compartment_mode", "")
    mode = value.strip().lower() if isinstance(value, str) else ""
    if mode not in {"new", "existing"}:
        raise ValueError("compartment mode must be new or existing")
    return mode


def compartment_target(context: dict[str, Any]) -> tuple[str, str]:
    return _compartment_name(context), _compartment_mode(context)


def validate_context(context: dict[str, Any]) -> None:
    source = context.get("source") or {}
    if context.get("project_id") != EXPECTED_PROJECT:
        raise ValueError(f"release requires project_id {EXPECTED_PROJECT}")
    if str(source.get("repository") or "").rstrip("/") not in EXPECTED_REPOSITORIES:
        raise ValueError("release requires the trusted GitHub repository")
    if source.get("ref") not in EXPECTED_REFS:
        raise ValueError("release requires source ref v2.1.0")
    if not _SHA.fullmatch(str(source.get("commit_sha") or "")):
        raise ValueError("release requires a full lowercase 40-character source SHA")
    if _OCI_REGION.fullmatch(str(context.get("region") or "")) is None:
        raise ValueError("release requires a valid effective OCI region")
    compartment_target(context)


def _deployment_files(terraform_root: Path) -> list[Path]:
    files = list(terraform_root.glob("*.tf"))
    files.extend(terraform_root.glob("hooks/*.py"))
    files.extend(terraform_root.glob("templatefile/*.sh"))
    return sorted(path for path in files if path.is_file())


def _technical_identity_finding(resource_kind: str, resource_name: str) -> str | None:
    if resource_kind.lower() == "group" and resource_name.lower() in _ALLOWED_IDENTITY_DOMAIN_GROUPS:
        return None
    return f"technical Identity Domains {resource_kind.lower()}"


def _forbidden_finding(text: str) -> str | None:
    finding = next((label for label, pattern in _SOURCE_PATTERNS if pattern.search(text)), None)
    if finding:
        return finding
    for kind, name in _IDENTITY_DOMAIN_RESOURCE.findall(text):
        finding = _technical_identity_finding(kind, name)
        if finding:
            return finding
    return None


def validate_source(terraform_root: Path) -> None:
    files = _deployment_files(terraform_root)
    if not files:
        raise ValueError("release source contains no deployable Terraform files")
    for path in files:
        text = path.read_text(encoding="utf-8")
        finding = _forbidden_finding(text)
        if finding:
            raise ValueError(f"release source contains {finding} in {path.relative_to(terraform_root)}")
        if 'resource "oci_objectstorage_bucket"' in text and re.search(r"\bkms_key_id\s*=", text):
            raise ValueError("the lab bucket must use its Oracle-managed encryption key")


def _has_nonempty_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return any(
            (item_key == key and item_value not in (None, "", False)) or _has_nonempty_key(item_value, key)
            for item_key, item_value in value.items()
        )
    if isinstance(value, list):
        return any(_has_nonempty_key(item, key) for item in value)
    return False


def _planned_values(resource: dict[str, Any]) -> dict[str, Any]:
    change = resource.get("change")
    if not isinstance(change, dict):
        return {}
    after = change.get("after")
    return after if isinstance(after, dict) else {}


def _forbidden_plan_type(resource_type: str, address: str) -> str | None:
    if resource_type.startswith(("oci_kms_", "oci_vault_")):
        return "OCI Vault or customer-managed KMS resource"
    if resource_type == "oci_identity_domains_app":
        return "Identity Domains OAuth client"
    if "ai_data_platform" in resource_type and "volume" in resource_type:
        return "external AIDP volume"
    identity_prefix = "oci_identity_domains_"
    identity_kind = resource_type.removeprefix(identity_prefix)
    if resource_type.startswith(identity_prefix) and identity_kind in {"user", "group", "grant"}:
        resource_name = address.rsplit(".", 1)[-1].split("[", 1)[0]
        return _technical_identity_finding(identity_kind, resource_name)
    return None


def validate_plan(plan: dict[str, Any]) -> None:
    managed = [change for change in plan.get("resource_changes") or [] if change.get("mode", "managed") == "managed"]
    if not managed:
        raise ValueError("release plan contains no managed resources")
    for resource in managed:
        address = str(resource.get("address") or resource.get("type") or "unknown resource")
        actions = (resource.get("change") or {}).get("actions") or []
        if actions != ["create"]:
            raise ValueError(f"fresh-only release rejects actions {actions!r} for {address}")
        serialized = json.dumps(resource, sort_keys=True, separators=(",", ":"))
        finding = _forbidden_finding(serialized)
        if finding:
            raise ValueError(f"release plan contains {finding} in {address}")
        resource_type = str(resource.get("type") or "").lower()
        forbidden_type = _forbidden_plan_type(resource_type, address)
        if forbidden_type:
            raise ValueError(f"release plan contains {forbidden_type} in {address}")
        if resource_type == "oci_objectstorage_bucket" and _has_nonempty_key(_planned_values(resource), "kms_key_id"):
            raise ValueError(f"release plan assigns a customer-managed key to {address}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the immutable fresh-only v2.1.0 AIDP lab release contract.")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--context-json", type=Path)
    parser.add_argument("--plan-json", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_source(args.source_root)
        if args.context_json:
            validate_context(json.loads(args.context_json.read_text(encoding="utf-8")))
        if args.plan_json:
            validate_plan(json.loads(args.plan_json.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release gate failed: {exc}", file=sys.stderr)
        return 1
    print("release gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
