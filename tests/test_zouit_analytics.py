import math
from math import isclose

from tools import calculate_social_infrastructure, calculate_zouit_intersections


def _polygon(x0, y0, x1, y1):
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def test_intersections_and_union_do_not_double_count():
    payload = {
        "parcel": {"geometry": {"geometry": _polygon(43.0, 56.0, 43.01, 56.01)}},
        "zones": {
            "zones": [
                {
                    "layer_code": "zouit_infrastructure",
                    "layer_title": "Инфраструктура",
                    "reg_number": "A",
                    "geometry": _polygon(43.0, 56.0, 43.006, 56.01),
                },
                {
                    "layer_code": "zouit_other",
                    "layer_title": "Иная ЗОУИТ",
                    "reg_number": "B",
                    "geometry": _polygon(43.004, 56.0, 43.01, 56.01),
                },
                {
                    "layer_code": "oopt",
                    "layer_title": "Не ЗОУИТ",
                    "geometry": _polygon(43.0, 56.0, 43.01, 56.01),
                },
            ]
        },
    }

    result = calculate_zouit_intersections(payload)

    assert len(result["zones"]) == 2
    assert all(zone["intersection_m2"] > 0 for zone in result["zones"])
    assert isclose(result["union_parcel_share_pct"], 100.0, abs_tol=0.02)
    assert result["union_intersection_m2"] < sum(
        zone["intersection_m2"] for zone in result["zones"]
    )


def test_social_distance_is_infinite_outside_target_circle():
    parcel = _polygon(43.0, 56.0, 43.001, 56.001)
    payload = {
        "parcel": {"geometry": {"geometry": parcel}},
        "environment": {
            "social": {
                "schools": {
                    "status": "ok",
                    "count_total": 2,
                    "objects": [
                        {
                            "name": "Рядом",
                            "kind": "school",
                            "osm_ref": "N/1",
                            "location": {"lat": 56.002, "lon": 43.001},
                        },
                        {
                            "name": "Далеко",
                            "kind": "school",
                            "osm_ref": "N/2",
                            "location": {"lat": 56.1, "lon": 43.1},
                        },
                    ],
                }
            },
            "low_coverage": False,
        },
    }

    result = calculate_social_infrastructure(payload)
    schools = next(category for category in result["categories"] if category["code"] == "schools")

    assert schools["objects"][0]["name"] == "Рядом"
    assert schools["objects"][0]["inside_target_circle"] is True
    assert schools["objects"][0]["distance_m"] < 1000
    assert schools["objects"][1]["name"] == "Далеко"
    assert schools["objects"][1]["inside_target_circle"] is False
    assert math.isinf(schools["objects"][1]["distance_m"])
