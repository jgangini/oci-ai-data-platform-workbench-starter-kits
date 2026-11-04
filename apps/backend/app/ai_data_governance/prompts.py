"""Read the versioned, source-controlled prompts; no second prompt definition."""

from __future__ import annotations

import re
from pathlib import Path


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "labs/ai_data_governance/prompts"


def documentary_prompts() -> dict[str, str]:
    text = (PROMPT_ROOT / "documentation-agent.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## (D[0-2])[^\n]*\n+```text\n(.*?)\n```", text, re.MULTILINE | re.DOTALL)
    if [name for name, _ in sections] != ["D0", "D1", "D2"] or "`DGD-PROMPTS-2`" not in text:
        raise ValueError("Documentary prompt source differs from the supported contract")
    return {name: content for name, content in sections}


def governance_prompts() -> dict[str, str]:
    text = (PROMPT_ROOT / "governance-agent.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## (G[0-6])[^\n]*\n+```text\n(.*?)\n```", text, re.MULTILINE | re.DOTALL)
    if [name for name, _ in sections] != ["G0", "G1", "G2", "G3", "G4", "G5", "G6"] or "`DG-PROMPTS-17`" not in text:
        raise ValueError("Governance prompt source differs from the supported contract")
    return dict(sections)
