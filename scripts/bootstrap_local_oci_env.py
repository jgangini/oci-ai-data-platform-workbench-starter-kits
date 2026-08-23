"""Create a Docker Compose .env using the live AIDP lab resources in OCI.

The script deliberately writes sensitive values only to the requested local file. It never
prints config values, private keys, or the generated environment.
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import oci
from oci import pagination

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.backend.app.security import hash_secret


LAB_PREFIX = "aidp-lab-"
CONTAINER_OCI_CONFIG = "/etc/aidp-lab/oci/config"
CONTAINER_OCI_KEY = "/etc/aidp-lab/oci/key.pem"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=False)
    parser.add_argument("--key", type=Path, required=False)
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--suffix", help="Explicit AIDP lab suffix when several labs exist.")
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--template", type=Path, default=Path(".env.example"))
    parser.add_argument("--access-email", type=Path, help="Deploy Studio access email used for local login credentials.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def load_config(config_path: Path, key_path: Path, profile: str) -> dict[str, str]:
    if not key_path.is_file():
        raise RuntimeError(f"OCI API key was not found: {key_path}")
    parser = configparser.ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        raise RuntimeError(f"OCI config was not found: {config_path}")
    if profile not in parser:
        raise RuntimeError(f"OCI config profile was not found: {profile}")
    config = dict(parser[profile])
    config["key_file"] = str(key_path.resolve())
    oci.config.validate_config(config)
    return config


def render_oci_config(config: dict[str, str]) -> str:
    """Render only non-secret API-key metadata for the local container."""
    if config.get("pass_phrase"):
        raise RuntimeError("OCI-local does not copy key passphrases; use a dedicated unencrypted API key")
    fields = ("user", "fingerprint", "tenancy", "region")
    values = {key: str(config.get(key) or "").strip() for key in fields}
    for key, value in values.items():
        if not value:
            raise RuntimeError(f"OCI config is missing: {key}")
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"OCI config value contains a newline: {key}")
    lines = ["[DEFAULT]", *(f"{key}={values[key]}" for key in fields), f"key_file={CONTAINER_OCI_KEY}"]
    return "\n".join(lines) + "\n"


def one(label: str, values: Iterable[Any]) -> Any:
    matches = list(values)
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {label}; found {len(matches)}. Supply --suffix if required.")
    return matches[0]


def one_named(label: str, values: Iterable[Any], expected_name: str) -> Any:
    return one(
        label,
        (
            item
            for item in values
            if str(getattr(item, "name", "")) == expected_name
        ),
    )


def platform_endpoint(platform: Any, region: str) -> str:
    endpoint = str(getattr(platform, "web_socket_endpoint", "") or "").strip()
    if endpoint:
        return endpoint
    alias = str(getattr(platform, "alias_key", "") or "").strip()
    if not alias:
        return ""
    region_key = next(
        (
            short_name
            for short_name, region_name in oci.regions.REGIONS_SHORT_NAMES.items()
            if region_name == region
        ),
        "",
    )
    if not region_key:
        raise RuntimeError(f"OCI SDK has no short region key for {region}")
    return alias if alias.endswith(region_key) else f"{alias}{region_key}"


def platform_workspace_name(platform: Any, suffix: str) -> str:
    published = str(getattr(platform, "default_workspace_name", "") or "").strip()
    if published:
        return published
    if str(getattr(platform, "display_name", "") or "") != f"aidp-lab-{suffix}":
        raise RuntimeError("AIDP platform name does not match the selected lab suffix")
    return f"aidp-lab-workspace-{suffix}"


def resources(method: Any, **kwargs: Any) -> list[Any]:
    """Read both OCI `items` and Identity Domains SCIM `resources` responses."""
    response = method(**kwargs)
    data = response.data
    if hasattr(data, "resources"):
        # Identity Domains paginates with startIndex/count, not opc-next-page.
        collected = list(data.resources)
        total = int(getattr(data, "total_results", len(collected)) or 0)
        while len(collected) < total:
            page_kwargs = {**kwargs, "start_index": len(collected) + 1, "count": 1000}
            page = method(**page_kwargs).data
            batch = list(page.resources)
            if not batch:
                break
            collected.extend(batch)
        return collected
    return list(pagination.list_call_get_all_results(method, **kwargs).data)


def home_config(config: dict[str, str]) -> dict[str, str]:
    identity = oci.identity.IdentityClient(config)
    subscriptions = identity.list_region_subscriptions(config["tenancy"]).data
    home = one("OCI home region", (item for item in subscriptions if item.is_home_region))
    result = dict(config)
    result["region"] = home.region_name
    return result


def template_hashes(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.replace("$$", "$")
    admin_hash = values.get("ADMIN_PASSWORD_HASH", "")
    code_hash = values.get("REGISTRATION_CODE_HASH", "")
    if not (admin_hash.startswith("pbkdf2_sha256$") and code_hash.startswith("pbkdf2_sha256$")):
        raise RuntimeError(f"{path} must supply PBKDF2 test hashes")
    return admin_hash, code_hash


class _AccessEmailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "td":
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "td" and self._cell is not None:
            self.cells.append(" ".join("".join(self._cell).split()))
            self._cell = None


def access_email_credentials(path: Path) -> tuple[str, str, str]:
    parser = _AccessEmailParser()
    parser.feed(path.read_text(encoding="utf-8"))
    labels = {"Username": "username", "Password": "password", "Lab registration code": "code"}
    values = {
        labels[cell]: parser.cells[index + 1]
        for index, cell in enumerate(parser.cells[:-1])
        if cell in labels
    }
    missing = [label for label, key in labels.items() if not values.get(key)]
    if missing:
        raise RuntimeError(f"{path} is missing access field(s): {', '.join(missing)}")
    return values["username"], hash_secret(values["password"]), hash_secret(values["code"])


def build_workbench_url(endpoint: str, tenancy_name: str, domain_name: str) -> str:
    """Build the direct Workbench link emitted by OCI Console's Copy URL action."""
    host = urlparse(endpoint).netloc or endpoint.split("/", 1)[0]
    if not host or not tenancy_name:
        return ""
    if not host.endswith(".datalake.oci.oraclecloud.com"):
        host = f"{host}.datalake.oci.oraclecloud.com"
    return f"https://{host}#?tenant={tenancy_name}&domain={domain_name}"


def discover(config: dict[str, str], suffix: str | None) -> dict[str, str]:
    home = home_config(config)
    identity = oci.identity.IdentityClient(home)
    domains = identity.list_domains(home["tenancy"], display_name="Default", lifecycle_state="ACTIVE").data
    domain = one("active Default Identity Domain", domains)
    idcs = oci.identity_domains.IdentityDomainsClient(home, service_endpoint=domain.url)

    developer_groups = resources(
        idcs.list_groups,
        filter='displayName sw "aidp-lab-developers-"',
        attributes="id,displayName",
        count=1000,
    )
    if suffix:
        developer_groups = [group for group in developer_groups if group.display_name == f"aidp-lab-developers-{suffix}"]
    developer_group = one("AIDP developer group", developer_groups)
    selected_suffix = developer_group.display_name.removeprefix("aidp-lab-developers-")
    pending_group = one(
        "AIDP pending group",
        (
            group
            for group in resources(
                idcs.list_groups,
                filter=f'displayName eq "aidp-lab-pending-{selected_suffix}"',
                attributes="id,displayName",
                count=1000,
            )
        ),
    )
    compartments = [config["tenancy"]]
    compartments.extend(
        item.id
        for item in resources(
            identity.list_compartments,
            compartment_id=config["tenancy"],
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        )
    )
    aidp = oci.ai_data_platform.AiDataPlatformClient(config)
    platforms = [
        platform
        for compartment_id in compartments
        for platform in resources(
            aidp.list_ai_data_platforms,
            compartment_id=compartment_id,
            lifecycle_state="ACTIVE",
        )
        if str(getattr(platform, "lifecycle_state", "")).upper() == "ACTIVE"
    ]
    platform_summary = one(
        "AIDP platform",
        (item for item in platforms if item.display_name == f"aidp-lab-{selected_suffix}"),
    )
    platform = aidp.get_ai_data_platform(platform_summary.id).data
    object_storage = oci.object_storage.ObjectStorageClient(config)
    namespace = str(object_storage.get_namespace().data)
    bucket = one_named(
        "AIDP data bucket",
        resources(
            object_storage.list_buckets,
            namespace_name=namespace,
            compartment_id=platform.compartment_id,
        ),
        f"aidp-data-{selected_suffix}",
    )
    return {
        "IDENTITY_DOMAIN_URL": domain.url.rstrip("/"),
        "IDENTITY_DEVELOPER_GROUP_ID": developer_group.id,
        "IDENTITY_PENDING_GROUP_ID": pending_group.id,
        "AIDP_WORKBENCH_URL": build_workbench_url(
            platform_endpoint(platform, config["region"]),
            identity.get_tenancy(config["tenancy"]).data.name,
            domain.display_name,
        ),
        "AIDP_PLATFORM_ID": platform.id,
        "AIDP_WORKSPACE_NAME": platform_workspace_name(platform, selected_suffix),
        "AIDP_REGION": config["region"],
        "OBJECTSTORAGE_NAMESPACE": namespace,
        "BUCKET_NAME": str(bucket.name),
        "AIDP_SETTINGS_FILE": "/var/lib/aidp-lab/settings.json",
        "LAB_MARKER": f"aidp-lab-{selected_suffix}",
    }


def render_env(values: dict[str, str]) -> str:
    required = (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD_HASH",
        "REGISTRATION_CODE_HASH",
        "IDENTITY_DOMAIN_URL",
        "IDENTITY_DEVELOPER_GROUP_ID",
        "IDENTITY_PENDING_GROUP_ID",
        "AIDP_PLATFORM_ID",
        "AIDP_WORKSPACE_NAME",
        "AIDP_REGION",
        "OCI_CONFIG_FILE",
        "OBJECTSTORAGE_NAMESPACE",
        "BUCKET_NAME",
        "OCI_CONFIG_HOST_PATH",
        "OCI_KEY_HOST_PATH",
        "AIDP_SETTINGS_FILE",
        "LAB_MARKER",
        "SESSION_SECRET_FILE",
        "COOKIE_SECURE",
        "LOCAL_DEVELOPMENT_MODE",
    )
    for key in required:
        if not values.get(key):
            raise RuntimeError(f"Missing generated environment value: {key}")
        if "\n" in values[key] or "\r" in values[key]:
            raise RuntimeError(f"Environment value contains a newline: {key}")
    keys = (*required, "AIDP_WORKBENCH_URL")
    return "\n".join(f"{key}={values.get(key, '').replace('$', '$$')}" for key in keys) + "\n"


def write_env(output: Path, content: str, force: bool) -> None:
    if output.exists() and not force:
        raise RuntimeError(f"Refusing to overwrite {output}. Pass --force after reviewing it.")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        temporary.replace(output)
        os.chmod(output, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def self_check() -> None:
    content = render_env(
        {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD_HASH": "pbkdf2_sha256$1$salt$hash",
            "REGISTRATION_CODE_HASH": "pbkdf2_sha256$1$salt$hash",
            "IDENTITY_DOMAIN_URL": "https://example.invalid",
            "IDENTITY_DEVELOPER_GROUP_ID": "group-a",
            "IDENTITY_PENDING_GROUP_ID": "group-b",
            "AIDP_WORKBENCH_URL": "https://example.datalake.oci.oraclecloud.com#?tenant=example&domain=Default",
            "AIDP_PLATFORM_ID": "ocid1.aidataplatform.oc1..example",
            "AIDP_WORKSPACE_NAME": "aidp-lab-workspace-example",
            "AIDP_REGION": "us-chicago-1",
            "OCI_CONFIG_FILE": CONTAINER_OCI_CONFIG,
            "OBJECTSTORAGE_NAMESPACE": "example-namespace",
            "BUCKET_NAME": "aidp-data-example",
            "OCI_CONFIG_HOST_PATH": "D:/safe/.tmp/oci-local/example/config",
            "OCI_KEY_HOST_PATH": "D:/keys/operator.pem",
            "AIDP_SETTINGS_FILE": "/var/lib/aidp-lab/settings.json",
            "LAB_MARKER": "aidp-lab-example",
            "SESSION_SECRET_FILE": "/var/lib/aidp-lab/session.key",
            "COOKIE_SECURE": "true",
            "LOCAL_DEVELOPMENT_MODE": "false",
        }
    )
    assert "LOCAL_DEVELOPMENT_MODE=false" in content
    assert f"OCI_CONFIG_FILE={CONTAINER_OCI_CONFIG}" in content
    sanitized = render_oci_config(
        {
            "user": "ocid1.user.oc1..example",
            "fingerprint": "00:11:22:33",
            "tenancy": "ocid1.tenancy.oc1..example",
            "region": "us-chicago-1",
            "key_file": "D:/keys/operator.pem",
        }
    )
    assert f"key_file={CONTAINER_OCI_KEY}" in sanitized
    assert "D:/keys/operator.pem" not in sanitized
    print("bootstrap_local_oci_env self-check passed")


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        return
    if not args.config or not args.key:
        raise RuntimeError("--config and --key are required unless --self-check is used")
    config = load_config(args.config, args.key, args.profile)
    if args.access_email:
        admin_username, admin_hash, registration_hash = access_email_credentials(args.access_email)
    else:
        admin_username = "admin"
        admin_hash, registration_hash = template_hashes(args.template)
    discovered = discover(config, args.suffix)
    selected_suffix = discovered["LAB_MARKER"].removeprefix(LAB_PREFIX)
    local_config = Path(".tmp") / "oci-local" / selected_suffix / "config"
    if not args.force:
        for path in (args.output, local_config):
            if path.exists():
                raise RuntimeError(f"Refusing to overwrite {path}. Pass --force after reviewing it.")
    values = {
        "ADMIN_USERNAME": admin_username,
        "ADMIN_PASSWORD_HASH": admin_hash,
        "REGISTRATION_CODE_HASH": registration_hash,
        "OCI_CONFIG_FILE": CONTAINER_OCI_CONFIG,
        "OCI_CONFIG_HOST_PATH": local_config.resolve().as_posix(),
        "OCI_KEY_HOST_PATH": args.key.resolve().as_posix(),
        "SESSION_SECRET_FILE": "/var/lib/aidp-lab/session.key",
        "COOKIE_SECURE": "true",
        "LOCAL_DEVELOPMENT_MODE": "false",
        **discovered,
    }
    write_env(local_config, render_oci_config(config), args.force)
    write_env(args.output, render_env(values), args.force)
    print(f"Created OCI-local configuration for {values['LAB_MARKER']}.")


if __name__ == "__main__":
    main()
