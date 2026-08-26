import json
import tempfile
from pathlib import Path

import batch


def _polygon(x0, y0, x1, y1):
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def _profile():
    return {
        "parcel": {
            "geometry": {"geometry": _polygon(43.0, 56.0, 43.01, 56.01)},
            "area": {"registry_m2": 700000.0, "computed_m2": 691000.0},
            "registry": {
                "category": "Земли населённых пунктов",
                "permitted_use": "Для строительства",
                "cost_value": 14000000.0,
            },
        },
        "zones": {
            "zones": [{
                "layer_code": "zouit_other",
                "layer_title": "Тестовая зона",
                "reg_number": "Z-1",
                "geometry": _polygon(43.0, 56.0, 43.005, 56.01),
            }]
        },
        "environment": {
            "transport": {
                "transit_stops": {
                    "status": "ok",
                    "count_total": 1,
                    "nearest_m": 145.0,
                    "objects": [{
                        "name": "Улица Тестовая",
                        "kind": "bus_stop",
                        "osm_ref": "N/22",
                        "distance_m": 145.0,
                        "distance_from_centroid_m": 300.0,
                        "azimuth_deg": 90.0,
                        "location": {"lat": 56.003, "lon": 43.002},
                    }],
                }
            },
            "social": {
                "schools": {
                    "status": "ok",
                    "count_total": 1,
                    "objects": [{
                        "name": "Школа",
                        "kind": "school",
                        "osm_ref": "N/1",
                        "location": {"lat": 56.002, "lon": 43.001},
                    }],
                }
            }
        },
        "risks": {"factors": [{"code": "R1", "severity": "high", "title": "Риск"}]},
        "score": {"total": 73.0, "confidence": 0.8},
        "meta": {"partial": False, "missing_blocks": [], "generated_at": "2026-08-26T00:00:00Z"},
    }


def test_read_single_column_csv_without_delimiter():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "cadastral_numbers.csv"
        source.write_text(
            "cadastral_number\n52:18:0070045:174\n52:18:0070045:175\n",
            encoding="utf-8",
        )
        assert batch.read_cadastral_numbers(source) == [
            "52:18:0070045:174",
            "52:18:0070045:175",
        ]


def test_batch_export_and_resume():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        calls = []

        def fetcher(number, depth):
            calls.append((number, depth))
            return _profile()

        manifest = batch.collect_batch(
            ["52:18:0070045:174"], output, run_id="test-run",
            dataset_version="test-v1", workers=1, fetcher=fetcher,
        )
        assert manifest["counts"]["completed"] == 1
        assert calls == [("52:18:0070045:174", "standard")]
        task = json.loads((output / "labels/test-v1_annotation_tasks.jsonl").read_text())
        assert task["restrictions"]["zouit_union_pct"] > 49
        assert task["transport_summary"]["transit_stops"]["nearest_m"] == 145.0
        assert "score_total" not in json.dumps(task)
        assert (output / "processed/test-run_features.parquet").exists()
        assert (output / "processed/test-run_transport_objects.parquet").exists()

        def must_not_fetch(number, depth):
            raise AssertionError("completed parcel was fetched during resume")

        resumed = batch.collect_batch(
            ["52:18:0070045:174"], output, run_id="test-run",
            dataset_version="test-v1", workers=1, resume=True, fetcher=must_not_fetch,
        )
        assert resumed["counts"]["completed"] == 1


if __name__ == "__main__":
    test_read_single_column_csv_without_delimiter()
    test_batch_export_and_resume()
