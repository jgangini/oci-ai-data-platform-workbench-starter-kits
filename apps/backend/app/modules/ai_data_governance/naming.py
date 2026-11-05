"""Existing participant checkpointer schema naming."""
import re

PARTICIPANT_KEY = re.compile(r"u[1-9][0-9]*")

def database_names(participant_key: str) -> tuple[str, str]:
    """Return the existing Autonomous DB schemas used by Agent checkpointers."""
    if PARTICIPANT_KEY.fullmatch(participant_key) is None or int(participant_key[1:]) < 101:
        raise ValueError("A participant key starting at u101 is required")
    stem = participant_key.upper()
    return f"{stem}_AGENT", f"{stem}_AGENT_RO"
