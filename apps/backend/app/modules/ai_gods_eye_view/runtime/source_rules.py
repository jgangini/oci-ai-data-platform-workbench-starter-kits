"""Shared capture configuration validation; report activity is separate from severity."""
from fastapi import HTTPException

from .core import canonical_mode


def validate_rules(source):
    window = source.get("correlation_window_minutes", 30)
    values = source.get("report_thresholds", {"low": 5, "medium": 10, "high": 20})
    if type(window) is not int or not 1 <= window <= 1440:
        raise ValueError("Correlation window must be between 1 and 1440 minutes")
    if (not isinstance(values, dict) or set(values) != {"low", "medium", "high"}
            or any(type(value) is not int for value in values.values())
            or not 1 <= values["low"] < values["medium"] < values["high"] <= 10000):
        raise ValueError("Report thresholds must be integers: 1 ≤ Low < Medium < High ≤ 10000")


def check_revision(current, expected):
    if expected is not None and expected != current.get("config_version", 1):
        raise HTTPException(409, "Source configuration changed. Reload before saving.")


def source_view(source):
    result = dict(source)
    result["mode"] = canonical_mode(source["mode"])
    if source.get("last_error"):
        state = "error"
    elif not source.get("enabled") or not source.get("capture_running"):
        state = "paused"
    elif source.get("status") == "capturing":
        state = "capturing"
    elif source.get("next_due"):
        state = "scheduled"
    else:
        state = "running"
    result["capture_state"] = state
    return result
