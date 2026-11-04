"""Initialize native descriptions of this module's own control tables only."""

from __future__ import annotations

from typing import Callable
from urllib.parse import quote

from .catalog import CatalogScope, _check_scope, is_redacted_metadata
from .schema import NAMESPACE, descriptions


Request = Callable[..., tuple[dict, dict]]


def confirmed_generated_readback(request, context, confirm_description, check_active):
    """Confirm only a known generated header; callers must verify its durable claim."""
    target = "/tables/" + quote(context["table_key"], safe="")
    document = context["document"]

    def readback(method, path, **kwargs):
        if method != "GET" or path != target:
            raise ValueError("Generated-description confirmation admits only its exact GET")
        detail, headers = request(method, path, **kwargs)
        if (confirm_description is None or context["column_name"] is not None
                or context["asset"]["asset_kind"] != "TABLE" or not isinstance(detail, dict)
                or detail.get("key") != context["table_key"]
                or not is_redacted_metadata(detail.get("description"))):
            return detail, headers
        scope = context["scope"]
        _check_scope(detail, CatalogScope(scope["catalog_key"], scope["schema_key"]))
        check_active()
        try:
            confirmed = confirm_description(context["table_key"], document["text_hash"]) is True
        except Exception:
            confirmed = False
        check_active()
        return ({**detail, "description": document["description_text"]} if confirmed else detail), headers

    return readback


def _read_description(request: Request, path: str, table_key: str, column_name: str | None,
                      expected_column_key: str | None = None) -> str | None:
    detail, _ = request("GET", path)
    if not isinstance(detail, dict) or detail.get("key") != table_key:
        raise ValueError("Native table identity differs from the admitted destination")
    if column_name is None:
        if "description" not in detail:
            raise ValueError("Native table description coverage is unavailable")
        value = detail["description"]
    else:
        value = _column_description(detail, column_name, expected_column_key)
    if value is not None and not isinstance(value, str):
        raise ValueError("Invalid native description")
    if is_redacted_metadata(value):
        raise ValueError("Native description coverage is redacted")
    return value


def _column_description(detail, column_name, expected_column_key):
    fields = detail.get("tableFields")
    if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
        raise ValueError("Native column coverage is unavailable")
    matching = [field for field in fields if field.get("fieldName") == column_name]
    if len(matching) != 1:
        raise ValueError("Native column identity is missing or ambiguous")
    if expected_column_key is not None and (matching[0].get("key") or matching[0].get("fieldKey")) != expected_column_key:
        raise ValueError("Native column identity changed since admission")
    if "fieldDescription" not in matching[0]:
        raise ValueError("Native column description coverage is unavailable")
    return matching[0]["fieldDescription"]


def publish_generated_description(
    request: Request, table_key: str, text: str, *, column_name: str | None = None,
    expected_column_key: str | None = None, readback_request=None,
) -> dict:
    """Fill a blank description; observed nonempty text, including [IA], is preserved.

    The authorized workflow owns scope admission and must persist its publication
    intent before calling this transport boundary. This is checked read/write/read,
    not atomic CAS. There is at most one PUT and no automatic write retry.
    """
    _validate_description_destination(table_key, column_name, expected_column_key)
    if not isinstance(text, str) or not text.endswith(" [IA]") or not text[:-5].strip() or text.count("[IA]") != 1:
        raise ValueError("Generated documentation requires exactly one final [IA] marker")
    return _publish_empty_description(request, table_key, text, column_name, expected_column_key,
        readback_request=readback_request)


def _validate_description_destination(table_key, column_name, expected_column_key):
    if not isinstance(table_key, str) or not table_key or len(table_key) > 1024:
        raise ValueError("An admitted native table identity is required")
    if column_name is not None and (not isinstance(column_name, str) or not column_name or len(column_name) > 256):
        raise ValueError("Invalid admitted native column identity")
    if expected_column_key is not None and (column_name is None or not isinstance(expected_column_key, str)
                                           or not expected_column_key or len(expected_column_key) > 1024):
        raise ValueError("Invalid admitted native column key")


def _publish_empty_description(request: Request, table_key: str, text: str, column_name: str | None,
                               expected_column_key: str | None = None, *, readback_request=None) -> dict:
    path = "/tables/" + quote(table_key, safe="")
    before = _read_description(request, path, table_key, column_name, expected_column_key)
    if before is not None and before.strip():
        return {"status": "preserved", "write_attempted": False,
                "write_acknowledged": False, "observed_description": before}
    payload = ({"description": text} if column_name is None else
               {"updateTableFields": [{"fieldName": column_name, "fieldDescription": text}]})
    acknowledged = False
    try:
        request("PUT", path, payload=payload)
        acknowledged = True
    except Exception:
        # A failed transport can hide a committed write. Reconcile once; never
        # repeat the PUT here or expose the transport's credential-bearing error.
        pass
    try:
        observed = _read_description(readback_request or request, path, table_key, column_name, expected_column_key)
    except Exception:
        return {"status": "unknown", "write_attempted": True,
                "write_acknowledged": acknowledged, "observed_description": None}
    if observed == text:
        status = "verified"
    elif observed is None or not observed.strip():
        status = "unknown"
    else:
        status = "conflict"
    return {"status": status, "write_attempted": True,
            "write_acknowledged": acknowledged, "observed_description": observed}


def initialize_table_descriptions(request: Request) -> dict[str, list[str]]:
    """Reconcile Spark comments with AIDP's separate native table descriptions.

    Transport must report non-2xx as errors and preserve response headers. This
    Fixed model comments are approved technical documentation, not AI drafts.
    This bootstrap shares the blank-only rule; it is not a CAS capability probe.
    """
    outcome: dict[str, list[str]] = {"verified": [], "preserved": []}
    for name, documentation in descriptions().items():
        key = f"{NAMESPACE}.{name.lower()}"
        expected = documentation["table"]
        result = _publish_empty_description(request, key, expected, None)
        if result["status"] == "verified" or result["observed_description"] == expected:
            outcome["verified"].append(name)
            continue
        if result["status"] == "preserved":
            outcome["preserved"].append(name)
            continue
        raise ValueError("Native table description was not verified after initialization")
    return outcome
