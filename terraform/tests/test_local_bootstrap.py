from types import SimpleNamespace

import pytest

from scripts.bootstrap_local_oci_env import (
    access_email_credentials,
    one_named,
    platform_endpoint,
    platform_workspace_name,
)
from apps.backend.app.security import verify_secret


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
