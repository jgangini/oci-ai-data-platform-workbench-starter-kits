"""A single module implementation has no deployment or environment selector."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_runtime_selector_in_terraform_or_bootstrap():
    assert 'variable "aidp_runtime_mode"' not in (ROOT / "terraform/b_variables.tf").read_text(encoding="utf-8")
    assert 'aidp_runtime_mode' not in (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    assert 'GOVERNANCE_RUNTIME_MODE=' not in (ROOT / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")


def test_no_runtime_selector_in_deploy_studio():
    manifest = json.loads((ROOT / "terraform/deploy-studio.json").read_text(encoding="utf-8"))
    assert 'aidp_runtime_mode' not in json.dumps(manifest)
    assert 'governance_runtime_mode' not in json.dumps(manifest)
