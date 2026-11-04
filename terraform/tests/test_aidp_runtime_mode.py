"""The Deploy Studio selector reaches the existing application environment."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


def test_runtime_selector_manifest_and_cloud_init_share_the_declared_input():
    manifest = json.loads((ROOT / "deploy-studio.json").read_text(encoding="utf-8"))
    fields = {field["name"]: field for field in manifest["form"]["fields"]}
    field = fields["aidp_runtime_mode"]
    assert field["type"] == "select" and not field["required"]
    assert field["default"] == "legacy"
    assert [option["value"] for option in field["options"]] == ["legacy", "v2_test"]
    assert "governance_runtime_mode" not in fields
    compute = (ROOT / "g_oci_core_instance.tf").read_text(encoding="utf-8")
    assert re.search(r"\baidp_runtime_mode\s*=\s*var\.aidp_runtime_mode\b", compute)
    template = (ROOT / "templatefile/user_data.sh").read_text(encoding="utf-8")
    assert "GOVERNANCE_RUNTIME_MODE=${aidp_runtime_mode}" in template


@pytest.fixture
def runtime_contract(tmp_path):
    executable = shutil.which("terraform")
    if executable is None:
        pytest.skip("Terraform is required to execute the runtime variable contract")
    variables = (ROOT / "b_variables.tf").read_text(encoding="utf-8")
    declaration = 'variable "aidp_runtime_mode" {'
    block = declaration + variables.split(declaration, 1)[1].split('\nvariable "', 1)[0]
    template = (ROOT / "templatefile/user_data.sh").read_text(encoding="utf-8")
    environment_line = next(line for line in template.splitlines() if line.startswith("GOVERNANCE_RUNTIME_MODE="))
    (tmp_path / "runtime.env.tftpl").write_text(environment_line + "\n", encoding="utf-8")
    (tmp_path / "runtime.tf").write_text(block + '\noutput "runtime_environment" {\n'
        '  value = templatefile("${path.module}/runtime.env.tftpl", { aidp_runtime_mode = var.aidp_runtime_mode })\n'
        '}\n', encoding="utf-8")
    return executable, tmp_path


@pytest.mark.parametrize(("mode", "expected"), [(None, "legacy"), ("legacy", "legacy"), ("v2_test", "v2_test")])
def test_terraform_renders_only_selected_mode_in_existing_environment(runtime_contract, mode, expected):
    executable, folder = runtime_contract
    command = [executable, "plan", "-input=false", "-refresh=false", "-lock=false", "-no-color"]
    if mode is not None:
        command.append("-var=aidp_runtime_mode=" + mode)
    result = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "GOVERNANCE_RUNTIME_MODE=" + expected in result.stdout


@pytest.mark.parametrize("mode", ["prod", "v2", "", "legacy;echo unsafe"])
def test_terraform_rejects_unsupported_mode_before_cloud_init(runtime_contract, mode):
    executable, folder = runtime_contract
    result = subprocess.run([executable, "plan", "-input=false", "-refresh=false", "-lock=false", "-no-color",
        "-var=aidp_runtime_mode=" + mode], cwd=folder, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "aidp_runtime_mode must be legacy or v2_test." in result.stderr
