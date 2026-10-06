"""Verify the exact managed-resource delta using mock providers; never call OCI."""
import json
from pathlib import Path
import subprocess


root = Path(__file__).resolve().parents[1]
result = subprocess.run(
    ["terraform", "test", "-json", "-verbose", f"-filter={Path('tests') / 'portal_modules.tftest.hcl'}"],
    cwd=root, capture_output=True, text=True, check=True,
)
events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
plans = [event["test_plan"] for event in events
         if event.get("type") == "test_plan" and event.get("@testrun") == "install_gods_eye_view"]
assert len(plans) == 1, "The module installation plan must run exactly once"
changes = {
    item["address"]: item["change"]["actions"]
    for item in plans[0]["resource_changes"]
    if item["change"]["actions"] != ["no-op"]
}
assert changes == {
    "oci_core_instance.gods_eye_view[0]": ["create"],
    "oci_identity_dynamic_group.gods_eye_view[0]": ["create"],
    "oci_identity_policy.gods_eye_view[0]": ["create"],
    "oci_identity_policy.gods_eye_view_run_command[0]": ["create"],
}, f"Unexpected infrastructure changes: {changes}"
print("PASS: four module creates; existing portal, network and data resources unchanged")
