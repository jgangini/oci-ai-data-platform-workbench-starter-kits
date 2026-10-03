from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.prisma.agent import incident_query
from app.prisma.agent_gateway import invoke


def test_incident_query_binds_filters_and_normalizes_period():
    sql, values = incident_query("publication-1", locality="Kennedy' OR 1=1 --", platform="x",
        date_from="2026-10-02T08:00:00-05:00", date_to="2026-10-02T14:00:00Z")
    assert values["date_from"] == "2026-10-02T13:00:00+00:00"
    assert values["date_to"] == "2026-10-02T14:00:00+00:00"
    assert values["platform"] == "x" and values["version"] == "publication-1"
    assert values["locality"] not in sql
    assert "e.version=i.version" in sql and "e.evidence_id=ids.eid" in sql
    assert "FETCH FIRST 100 ROWS ONLY" in sql


@pytest.mark.parametrize("period", [
    {"date_from": "invalid"}, {"date_to": "2026-10-02T10:00:00"},
    {"date_from": "2026-10-02T15:00:00Z", "date_to": "2026-10-02T10:00:00Z"},
])
def test_invalid_period_rejected_before_sql_or_gateway(period):
    with pytest.raises(ValueError):
        incident_query("v1", **period)
    with pytest.raises(HTTPException) as caught:
        invoke(SimpleNamespace(), "unused", {"question": "Consulta", "version": "v1", "filters": period},
               "cookie", b"key", {"version": "v1"})
    assert caught.value.status_code == 422
