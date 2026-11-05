"""Native identities for the single supported Governance installation."""
GOVERNANCE_MODULE_ID = "ai_data_governance"
GOVERNANCE_DISPLAY_NAME = "AI Data Governance"
GOVERNANCE_CREDENTIAL_NAME = "AidpRuntime"
GOVERNANCE_AGENT_NAME = "ai_data_governance"
GOVERNANCE_AGENT_COMPUTE_NAME = "aidp_data_governance_agent_compute"
GOVERNANCE_JOB_NAME = "wf_ai_dg_sync_master_catalog"
RUNTIME_FAMILY = "AI_DATA_GOVERNANCE_TEST"


def governance_compute_payload() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "aidp/workspaces/default/agent_flow/compute/aidp_data_governance_agent_compute.json"
    payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
    if payload.get("type") != "AI_COMPUTE" or payload.get("displayName") != GOVERNANCE_AGENT_COMPUTE_NAME:
        raise ValueError("Governance compute descriptor changed native identity")
    return payload
