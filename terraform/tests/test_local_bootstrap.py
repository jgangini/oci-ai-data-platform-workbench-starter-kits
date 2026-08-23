import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.bootstrap_local_oci_env import (
    access_email_credentials,
    one_named,
    parse_args,
    platform_endpoint,
    platform_workspace_name,
)
from apps.backend.app.security import verify_secret


def test_compose_local_profile_defaults_and_overrides_are_parameterized() -> None:
    compose = (
        Path(__file__).parents[2] / "docker" / "docker-compose.oci-local.yml"
    ).read_text(encoding="utf-8")

    assert "name: ${AIDP_LOCAL_PROJECT_NAME:-aidp-lab-oci-local}" in compose
    assert "- ${AIDP_LOCAL_ENV_FILE:-../.env}" in compose
    assert '"127.0.0.1:${AIDP_LOCAL_HOST_PORT:-18082}:80"' in compose


def test_local_bootstrap_accepts_profile_scoped_sanitized_config_path(monkeypatch) -> None:
    expected = Path(".local/dep_0123456789abcdef/oci/config")
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap_local_oci_env.py", "--local-config-output", str(expected)],
    )

    assert parse_args().local_config_output == expected


def test_local_bootstrap_uses_exact_bucket_name() -> None:
    buckets = [
        SimpleNamespace(name="aidp-data-other"),
        SimpleNamespace(name="aidp-data-selected"),
    ]

    assert one_named("bucket", buckets, "aidp-data-selected").name == "aidp-data-selected"


def test_local_bootstrap_uses_alias_and_deterministic_workspace_fallbacks() -> None:
    platform = SimpleNamespace(
        alias_key="workbench-alias",
        default_workspace_name=None,
        display_name="aidp-lab-selected",
        web_socket_endpoint=None,
    )

    assert platform_endpoint(platform, "us-chicago-1") == "workbench-aliasord"
    assert platform_workspace_name(platform, "selected") == "aidp-lab-workspace-selected"


def test_local_bootstrap_rejects_workspace_fallback_for_another_platform() -> None:
    platform = SimpleNamespace(
        default_workspace_name=None,
        display_name="aidp-lab-another",
    )

    with pytest.raises(RuntimeError, match="does not match"):
        platform_workspace_name(platform, "selected")


def test_local_bootstrap_reads_deployment_access_email_without_retaining_secrets(tmp_path) -> None:
    email = tmp_path / "email.html"
    email.write_text(
        """
        <table>
          <tr><td>Username</td><td><code>admin</code></td></tr>
          <tr><td>Password</td><td><code>LocalPassword123</code></td></tr>
          <tr><td>Lab registration code</td><td><code>ABCD-1234</code></td></tr>
        </table>
        """,
        encoding="utf-8",
    )

    username, password_hash, code_hash = access_email_credentials(email)

    assert username == "admin"
    assert verify_secret("LocalPassword123", password_hash)
    assert verify_secret("ABCD-1234", code_hash)
    assert "LocalPassword123" not in password_hash
    assert "ABCD-1234" not in code_hash
