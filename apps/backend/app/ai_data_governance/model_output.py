"""Bounded model JSON parsing, never code extraction or executable repair."""

from __future__ import annotations

import json


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Model output contains duplicate object members")
        result[key] = value
    return result


def model_json(content, *, max_bytes: int = 262144) -> dict:
    if isinstance(content, list):
        if not all(isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) for part in content):
            raise ValueError("The model response is not a text-only result")
        content = "".join(part["text"] for part in content)
    if not isinstance(content, str) or len(content.encode("utf-8")) > max_bytes:
        raise ValueError("Model response is missing or exceeds its bound")
    value = json.loads(content, object_pairs_hook=_unique_members,
                       parse_constant=lambda _: _invalid_constant())
    if not isinstance(value, dict):
        raise ValueError("A model result must be one JSON object")
    return value


def _invalid_constant():
    raise ValueError("Non-finite model values are not admitted")
