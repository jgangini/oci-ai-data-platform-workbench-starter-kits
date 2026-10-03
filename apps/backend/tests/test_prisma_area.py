import pytest
from app.prisma.area import parse_bbox, within_bbox
from app.prisma.agent import incident_query
from app.prisma.agent_gateway import validated_filters
from fastapi import HTTPException


def test_area_bounds_are_inclusive_and_missing_coordinates_stay_outside():
    bounds = parse_bbox("-74.2,4.6,-74.1,4.7")
    assert within_bbox({"lat": 4.6, "lon": -74.2}, bounds)
    assert within_bbox({"lat": 4.7, "lon": -74.1}, bounds)
    assert not within_bbox({"lat": 4.700001, "lon": -74.1}, bounds)
    assert not within_bbox({"lat": None, "lon": None}, bounds)
    assert not within_bbox({"lat": True, "lon": -74.1}, parse_bbox("-180,-90,180,90"))
    assert within_bbox({}, parse_bbox(""))


@pytest.mark.parametrize("value", ["1,2,3", "1,,3,4", "nan,2,3,4", "-181,0,180,1", "0,-91,1,1", "3,2,1,4", "1,4,3,2", "170,-10,-170,10", "0x10,0,20,10", "1_0,0,20,10"])
def test_invalid_area_cannot_reach_the_agent(value):
    with pytest.raises(ValueError):
        parse_bbox(value)
    with pytest.raises(HTTPException) as error:
        validated_filters({"bbox": value})
    assert error.value.status_code == 422


def test_area_query_uses_numeric_bind_parameters():
    sql, values = incident_query("v1", bbox="-74.2,4.6,-74.1,4.7")
    assert "BETWEEN :west AND :east" in sql and "BETWEEN :south AND :north" in sql
    assert {key: values[key] for key in ("west", "south", "east", "north")} == {"west": -74.2, "south": 4.6, "east": -74.1, "north": 4.7}
    assert all(incident_query("v1")[1][key] is None for key in ("west", "south", "east", "north"))
