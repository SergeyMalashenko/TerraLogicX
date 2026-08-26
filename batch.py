"""Resumable batch collection and dataset export for cadastral parcels."""

from __future__ import annotations

import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from .tools import (
        API_BASE_URL,
        AnalyticsError,
        CADASTRAL_NUMBER_RE,
        calculate_social_infrastructure,
        calculate_zouit_intersections,
    )
except ImportError:  # Direct execution from a source checkout.
    from tools import (
        API_BASE_URL,
        AnalyticsError,
        CADASTRAL_NUMBER_RE,
        calculate_social_infrastructure,
        calculate_zouit_intersections,
    )

RETRYABLE_CODES = {"upstream_unavailable", "all_proxies_blocked", "upstream_error"}
FINAL_STATUSES = {"completed", "partial"}
TABLES_TO_EXPORT = ("parcels", "features", "zones", "social_objects", "annotation_tasks")


class BatchError(RuntimeError):
    """Expected batch collection failure."""


class SourceError(BatchError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_cadastral_numbers(path: Path) -> list[str]:
    """Read a txt file or a CSV with a cadastral_number column."""
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise BatchError("Файл кадастровых номеров пуст")
    first_line = text.splitlines()[0].lower()
    if "cadastral_number" in first_line:
        delimiter = ";" if ";" in first_line else "\t" if "\t" in first_line else ","
        rows = csv.DictReader(text.splitlines(), delimiter=delimiter)
        values = [str(row.get("cadastral_number", "")).strip() for row in rows]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]

    result = []
    seen = set()
    for value in values:
        if not CADASTRAL_NUMBER_RE.fullmatch(value):
            raise BatchError(f"Некорректный кадастровый номер: {value}")
        if value not in seen:
            seen.add(value)
            result.append(value)
    if not result:
        raise BatchError("В файле нет кадастровых номеров")
    return result


def fetch_profile(
    cadastral_number: str,
    depth: str,
    *,
    attempts: int = 3,
    retry_delay_s: float = 60.0,
) -> dict:
    api_key = os.environ.get("UCHASTOK_API_KEY")
    if not api_key:
        raise BatchError("Не задана переменная окружения UCHASTOK_API_KEY")
    query = urlencode(
        {
            "depth": depth,
            "environment": "true",
            "zone_geometry": "true",
            "refresh": "false",
        }
    )
    url = f"{API_BASE_URL}/parcels/{quote(cadastral_number, safe='')}/profile?{query}"

    last_error = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={"Accept": "application/json", "X-API-Key": api_key},
            method="GET",
        )
        try:
            with urlopen(request, timeout=180) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                error = payload.get("error", {})
                code = str(error.get("code") or f"http_{exc.code}")
                message = str(error.get("message") or exc.reason)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                code, message = f"http_{exc.code}", str(exc.reason)
            last_error = SourceError(code, message, retryable=code in RETRYABLE_CODES)
        except (URLError, TimeoutError) as exc:
            last_error = SourceError("network_error", str(exc), retryable=True)

        if not last_error.retryable or attempt == attempts:
            raise last_error
        time.sleep(retry_delay_s * attempt)
    raise last_error or BatchError("Неизвестная ошибка загрузки")


class DatasetStore:
    def __init__(self, output_dir: Path):
        try:
            import duckdb
        except ImportError as exc:
            raise BatchError("Установите зависимость: python -m pip install duckdb>=1.4,<2") from exc
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.output_dir / "raw"
        self.processed_dir = self.output_dir / "processed"
        self.labels_dir = self.output_dir / "labels"
        self.runs_dir = self.output_dir / "runs"
        for directory in (self.raw_dir, self.processed_dir, self.labels_dir, self.runs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.output_dir / "dataset.duckdb"))
        self._create_schema()

    def close(self):
        self.connection.close()

    def _create_schema(self):
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_runs (
                run_id VARCHAR PRIMARY KEY, dataset_version VARCHAR, started_at TIMESTAMP,
                finished_at TIMESTAMP, status VARCHAR, input_count INTEGER,
                completed_count INTEGER, partial_count INTEGER, failed_count INTEGER,
                depth VARCHAR, config_json JSON
            );
            CREATE TABLE IF NOT EXISTS parcels (
                run_id VARCHAR, cadastral_number VARCHAR, status VARCHAR,
                collected_at TIMESTAMP, raw_path VARCHAR, partial BOOLEAN,
                missing_blocks_json JSON, error_code VARCHAR, error_message VARCHAR,
                PRIMARY KEY (run_id, cadastral_number)
            );
            CREATE TABLE IF NOT EXISTS features (
                run_id VARCHAR, cadastral_number VARCHAR, area_registry_m2 DOUBLE,
                area_computed_m2 DOUBLE, cost_value DOUBLE, cost_per_m2 DOUBLE,
                category VARCHAR, permitted_use VARCHAR, ownership_type VARCHAR,
                zouit_count INTEGER, zouit_union_m2 DOUBLE, zouit_union_pct DOUBLE,
                social_inside_count INTEGER, social_outside_count INTEGER,
                target_circle_radius_m DOUBLE, critical_risk_count INTEGER,
                high_risk_count INTEGER, score_total DOUBLE, score_confidence DOUBLE,
                data_completeness DOUBLE,
                PRIMARY KEY (run_id, cadastral_number)
            );
            CREATE TABLE IF NOT EXISTS zones (
                run_id VARCHAR, cadastral_number VARCHAR, zone_index INTEGER,
                layer_code VARCHAR, layer_title VARCHAR, reg_number VARCHAR,
                overlap_m2 DOUBLE, overlap_ratio DOUBLE, building_impact VARCHAR,
                geometry_json JSON,
                PRIMARY KEY (run_id, cadastral_number, zone_index)
            );
            CREATE TABLE IF NOT EXISTS social_objects (
                run_id VARCHAR, cadastral_number VARCHAR, category VARCHAR,
                object_index INTEGER, name VARCHAR, kind VARCHAR, osm_ref VARCHAR,
                inside_target_circle BOOLEAN, distance_m DOUBLE,
                PRIMARY KEY (run_id, cadastral_number, category, object_index)
            );
            CREATE TABLE IF NOT EXISTS annotation_tasks (
                task_id VARCHAR PRIMARY KEY, run_id VARCHAR, dataset_version VARCHAR,
                cadastral_number VARCHAR, payload_json JSON
            );
            """
        )

    def start_run(self, run_id: str, dataset_version: str, count: int, depth: str, workers: int):
        self.connection.execute(
            """
            INSERT INTO collection_runs VALUES (?, ?, ?, NULL, 'running', ?, 0, 0, 0, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET status='running', input_count=excluded.input_count
            """,
            [run_id, dataset_version, utc_now(), count, depth, json.dumps({"workers": workers})],
        )

    def completed_numbers(self, run_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT cadastral_number FROM parcels WHERE run_id=? AND status IN ('completed','partial')",
            [run_id],
        ).fetchall()
        return {row[0] for row in rows}

    def save_error(self, run_id: str, cadastral_number: str, code: str, message: str):
        self.connection.execute(
            """
            INSERT INTO parcels VALUES (?, ?, 'failed', ?, NULL, false, '[]', ?, ?)
            ON CONFLICT (run_id, cadastral_number) DO UPDATE SET
              status='failed', collected_at=excluded.collected_at,
              error_code=excluded.error_code, error_message=excluded.error_message
            """,
            [run_id, cadastral_number, utc_now(), code, message],
        )

    def save_profile(self, run_id: str, dataset_version: str, cadastral_number: str, profile: Mapping):
        safe_number = cadastral_number.replace(":", "_")
        parcel_dir = self.raw_dir / safe_number
        parcel_dir.mkdir(parents=True, exist_ok=True)
        raw_path = parcel_dir / f"{run_id}_profile.json"
        raw_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )

        meta = profile.get("meta", {}) if isinstance(profile.get("meta"), Mapping) else {}
        partial = bool(meta.get("partial", False))
        missing_blocks = list(meta.get("missing_blocks", []))
        status = "partial" if partial else "completed"
        self.connection.execute("BEGIN")
        try:
            self.connection.execute(
                """
                INSERT INTO parcels VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT (run_id, cadastral_number) DO UPDATE SET
                  status=excluded.status, collected_at=excluded.collected_at,
                  raw_path=excluded.raw_path, partial=excluded.partial,
                  missing_blocks_json=excluded.missing_blocks_json,
                  error_code=NULL, error_message=NULL
                """,
                [run_id, cadastral_number, status, utc_now(), str(raw_path), partial, json.dumps(missing_blocks)],
            )
            self._save_normalized(run_id, dataset_version, cadastral_number, profile)
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def _save_normalized(self, run_id: str, dataset_version: str, cn: str, profile: Mapping):
        parcel = profile.get("parcel", {})
        registry = parcel.get("registry", {}) if isinstance(parcel, Mapping) else {}
        area = parcel.get("area", {}) if isinstance(parcel, Mapping) else {}
        risks = profile.get("risks", {}) or {}
        score = profile.get("score", {}) or {}
        meta = profile.get("meta", {}) or {}
        try:
            zouit = calculate_zouit_intersections(profile)
        except AnalyticsError:
            zouit = {"zones": [], "union_intersection_m2": None, "union_parcel_share_pct": None}
        try:
            social = calculate_social_infrastructure(profile)
        except AnalyticsError:
            social = {"categories": [], "total_inside": 0, "total_outside": 0, "target_radius_m": None}

        factors = risks.get("factors", []) if isinstance(risks, Mapping) else []
        completeness = max(0.0, 1.0 - len(meta.get("missing_blocks", [])) / 8.0)
        registry_area = area.get("registry_m2") if isinstance(area, Mapping) else None
        cost_value = registry.get("cost_value") if isinstance(registry, Mapping) else None
        cost_per_m2 = cost_value / registry_area if cost_value and registry_area else None
        feature_values = [
            run_id, cn, registry_area, area.get("computed_m2"), cost_value, cost_per_m2,
            registry.get("category"), registry.get("permitted_use"), registry.get("ownership_type"),
            len(zouit["zones"]), zouit["union_intersection_m2"], zouit["union_parcel_share_pct"],
            social["total_inside"], social["total_outside"], social["target_radius_m"],
            sum(f.get("severity") == "critical" for f in factors),
            sum(f.get("severity") == "high" for f in factors),
            score.get("total") if isinstance(score, Mapping) else None,
            score.get("confidence") if isinstance(score, Mapping) else None,
            completeness,
        ]
        self.connection.execute(
            "DELETE FROM features WHERE run_id=? AND cadastral_number=?", [run_id, cn]
        )
        self.connection.execute("INSERT INTO features VALUES (" + ",".join("?" * 20) + ")", feature_values)

        self.connection.execute("DELETE FROM zones WHERE run_id=? AND cadastral_number=?", [run_id, cn])
        source_zones = {
            (zone.get("layer_code"), zone.get("reg_number") or zone.get("reg_numb")): zone
            for zone in (profile.get("zones", {}) or {}).get("zones", [])
            if isinstance(zone, Mapping)
        }
        for index, zone in enumerate(zouit["zones"]):
            source = source_zones.get((zone.get("layer_code"), zone.get("reg_number")), {})
            self.connection.execute(
                "INSERT INTO zones VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, cn, index, zone.get("layer_code"), zone.get("layer_title"),
                 zone.get("reg_number"), zone.get("intersection_m2"),
                 zone.get("parcel_share_pct"), source.get("building_impact"),
                 json.dumps(source.get("geometry"), ensure_ascii=False)],
            )

        self.connection.execute(
            "DELETE FROM social_objects WHERE run_id=? AND cadastral_number=?", [run_id, cn]
        )
        nearest = {}
        for category in social["categories"]:
            finite = [o for o in category["objects"] if o["inside_target_circle"]]
            nearest[category["code"]] = finite[0]["distance_m"] if finite else None
            for index, item in enumerate(category["objects"]):
                self.connection.execute(
                    "INSERT INTO social_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, cn, category["code"], index, item["name"], item["kind"],
                     item["osm_ref"], item["inside_target_circle"], item["distance_m"]],
                )

        task_id = f"{dataset_version}:{cn}"
        task = {
            "task_id": task_id,
            "dataset_version": dataset_version,
            "cadastral_number": cn,
            "facts": {
                "area_registry_m2": registry_area,
                "area_computed_m2": area.get("computed_m2"),
                "category": registry.get("category"),
                "permitted_use": registry.get("permitted_use"),
                "ownership_type": registry.get("ownership_type"),
                "cost_value": cost_value,
                "cost_per_m2": cost_per_m2,
            },
            "restrictions": {
                "zouit_count": len(zouit["zones"]),
                "zouit_union_m2": zouit["union_intersection_m2"],
                "zouit_union_pct": zouit["union_parcel_share_pct"],
            },
            "social_summary": {
                "inside_count": social["total_inside"],
                "target_circle_radius_m": social["target_radius_m"],
                "nearest_m": nearest,
            },
            "risks": [
                {key: factor.get(key) for key in ("code", "title", "severity", "category", "comment")}
                for factor in factors
            ],
            "data_quality": {
                "partial": bool(meta.get("partial", False)),
                "missing_blocks": list(meta.get("missing_blocks", [])),
                "score_confidence": score.get("confidence") if isinstance(score, Mapping) else None,
                "generated_at": meta.get("generated_at"),
            },
        }
        payload_json = json.dumps(task, ensure_ascii=False, allow_nan=False)
        self.connection.execute("DELETE FROM annotation_tasks WHERE task_id=?", [task_id])
        self.connection.execute(
            "INSERT INTO annotation_tasks VALUES (?, ?, ?, ?, ?)",
            [task_id, run_id, dataset_version, cn, payload_json],
        )

    def finish_run(self, run_id: str):
        counts = dict(
            self.connection.execute(
                "SELECT status, count(*) FROM parcels WHERE run_id=? GROUP BY status", [run_id]
            ).fetchall()
        )
        failed = counts.get("failed", 0)
        status = "completed_with_errors" if failed else "completed"
        self.connection.execute(
            """
            UPDATE collection_runs SET finished_at=?, status=?, completed_count=?,
              partial_count=?, failed_count=? WHERE run_id=?
            """,
            [utc_now(), status, counts.get("completed", 0), counts.get("partial", 0), failed, run_id],
        )
        return counts

    def export(self, run_id: str, dataset_version: str):
        for table in TABLES_TO_EXPORT:
            target = self.processed_dir / f"{run_id}_{table}.parquet"
            if target.exists():
                target.unlink()
            table_name = table.replace('"', '')
            escaped_run = run_id.replace("'", "''")
            escaped_target = str(target).replace("'", "''")
            self.connection.execute(
                f"COPY (SELECT * FROM {table_name} WHERE run_id='{escaped_run}') "
                f"TO '{escaped_target}' (FORMAT PARQUET)"
            )

        tasks_path = self.labels_dir / f"{dataset_version}_annotation_tasks.jsonl"
        rows = self.connection.execute(
            "SELECT payload_json FROM annotation_tasks WHERE run_id=? ORDER BY cadastral_number",
            [run_id],
        ).fetchall()
        with tasks_path.open("w", encoding="utf-8") as stream:
            for (payload,) in rows:
                value = json.loads(payload) if isinstance(payload, str) else payload
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        self._write_labeling_assets(dataset_version)
        return tasks_path

    def _write_labeling_assets(self, dataset_version: str):
        schema = {
            "dataset_version": dataset_version,
            "unique_key": ["task_id", "annotator_id"],
            "ratings": {
                name: {"type": "integer", "minimum": 1, "maximum": 5, "required": True}
                for name in (
                    "legal_attractiveness", "physical_attractiveness",
                    "infrastructure_attractiveness", "social_attractiveness",
                    "market_attractiveness", "overall_attractiveness",
                )
            },
            "other_fields": {
                "investment_horizon": ["short", "medium", "long"],
                "recommended_use": "string", "label_confidence": "number 0..1",
                "comment": "string", "submitted_at": "ISO-8601",
            },
        }
        (self.labels_dir / f"{dataset_version}_annotation_schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        guidelines = """# Инструкция разметчика

Оценивайте каждый участок независимо по шкале 1–5. Не пытайтесь восстановить
автоматический score: он намеренно не включён в карточку задания. Учитывайте
`data_quality`; при неполных данных снижайте `label_confidence` и объясняйте это
в `comment`. Оценки других разметчиков до отправки ответа не показываются.

На арбитраж направляются записи, где размах `overall_attractiveness` не меньше 2.
Итоговая оценка формируется медианой независимых ответов.
"""
        (self.labels_dir / f"{dataset_version}_guidelines.md").write_text(
            guidelines, encoding="utf-8"
        )


def collect_batch(
    cadastral_numbers: Iterable[str],
    output_dir: Path,
    *,
    run_id: str,
    dataset_version: str,
    depth: str = "standard",
    workers: int = 1,
    resume: bool = False,
    fetcher: Callable[[str, str], dict] = fetch_profile,
) -> dict:
    numbers = list(cadastral_numbers)
    if not 1 <= len(numbers) <= 10000:
        raise BatchError("Число участков должно быть от 1 до 10000")
    if depth not in {"minimal", "standard", "full"}:
        raise BatchError("depth должен быть minimal, standard или full")
    if not 1 <= workers <= 4:
        raise BatchError("workers должен быть от 1 до 4")

    store = DatasetStore(output_dir)
    try:
        store.start_run(run_id, dataset_version, len(numbers), depth, workers)
        completed = store.completed_numbers(run_id) if resume else set()
        pending = [number for number in numbers if number not in completed]

        def fetch(number):
            return number, fetcher(number, depth)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch, number): number for number in pending}
            for future in as_completed(futures):
                number = futures[future]
                try:
                    _, profile = future.result()
                    store.save_profile(run_id, dataset_version, number, profile)
                    print(f"[OK] {number}")
                except SourceError as exc:
                    store.save_error(run_id, number, exc.code, str(exc))
                    print(f"[ERROR] {number}: {exc.code}: {exc}")
                except Exception as exc:
                    store.save_error(run_id, number, "processing_error", str(exc))
                    print(f"[ERROR] {number}: processing_error: {exc}")

        counts = store.finish_run(run_id)
        tasks_path = store.export(run_id, dataset_version)
        manifest = {
            "run_id": run_id,
            "dataset_version": dataset_version,
            "input_count": len(numbers),
            "counts": counts,
            "annotation_tasks": str(tasks_path),
            "finished_at": utc_now(),
        }
        manifest_path = store.runs_dir / run_id / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
    finally:
        store.close()
