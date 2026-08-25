"""ZOUIT intersection analytics backed by UCHASTOK geometries."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pyproj import CRS, Transformer
from shapely import minimum_bounding_circle, minimum_bounding_radius
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union

try:
    from shapely import make_valid
except ImportError:  # Shapely 1.x fallback
    make_valid = None


API_BASE_URL = "https://zu.aigogo.ru/api/v1"
ZOUIT_LAYERS = (
    "zouit_heritage",
    "zouit_infrastructure",
    "zouit_security",
    "zouit_nature",
    "zouit_other",
)
CADASTRAL_NUMBER_RE = re.compile(r"^\d{2}:\d{2}:\d{6,7}:\d+$")
SOCIAL_CATEGORIES = (
    "schools",
    "kindergartens",
    "clinics",
    "hospitals",
    "pharmacies",
    "grocery",
    "malls",
    "worship",
    "food",
    "sport",
    "pickup_points",
)
SOCIAL_CATEGORY_TITLES = {
    "schools": "Школы",
    "kindergartens": "Детские сады",
    "clinics": "Поликлиники и врачи",
    "hospitals": "Больницы",
    "pharmacies": "Аптеки",
    "grocery": "Продуктовые магазины",
    "malls": "Торговые центры",
    "worship": "Религиозные объекты",
    "food": "Общественное питание",
    "sport": "Спорт и досуг",
    "pickup_points": "Пункты выдачи заказов",
}
SOCIAL_RADIUS_EXTENSION_M = 1000.0


class AnalyticsError(RuntimeError):
    """Expected error that can be safely shown to the user."""


def _valid_geometry(geometry):
    if geometry.is_valid:
        return geometry
    if make_valid is not None:
        return make_valid(geometry)
    return geometry.buffer(0)


def _geojson(value, *, label: str):
    """Accept raw GeoJSON or the API's {geometry: GeoJSON} envelope."""
    if not isinstance(value, Mapping):
        raise AnalyticsError(f"В ответе отсутствует геометрия: {label}")
    candidate = value.get("geometry", value)
    if not isinstance(candidate, Mapping) or "type" not in candidate:
        raise AnalyticsError(f"Некорректная геометрия в ответе: {label}")
    geometry = _valid_geometry(shape(candidate))
    if geometry.is_empty:
        raise AnalyticsError(f"Пустая геометрия в ответе: {label}")
    return geometry


def _local_equal_area_transformer(parcel_geometry):
    center = parcel_geometry.centroid
    target = CRS.from_proj4(
        f"+proj=laea +lat_0={center.y:.10f} +lon_0={center.x:.10f} "
        "+datum=WGS84 +units=m +no_defs"
    )
    return Transformer.from_crs("EPSG:4326", target, always_xy=True).transform


def calculate_zouit_intersections(payload: Mapping) -> dict:
    """Calculate per-zone and union intersections from a REST zones payload."""
    parcel_block = payload.get("parcel")
    if not isinstance(parcel_block, Mapping):
        raise AnalyticsError("Ответ API не содержит блока parcel")

    parcel_geometry = _geojson(parcel_block.get("geometry"), label="участок")
    project = _local_equal_area_transformer(parcel_geometry)
    parcel_projected = _valid_geometry(transform(project, parcel_geometry))
    parcel_area_m2 = parcel_projected.area
    if parcel_area_m2 <= 0:
        raise AnalyticsError("Площадь геометрии участка равна нулю")

    zones_block = payload.get("zones", {})
    zones = zones_block.get("zones", []) if isinstance(zones_block, Mapping) else []
    results = []
    intersections = []
    skipped = []

    for index, zone in enumerate(zones):
        if not isinstance(zone, Mapping):
            skipped.append(f"зона #{index + 1}: некорректный объект")
            continue
        layer_code = str(zone.get("layer_code", ""))
        if layer_code not in ZOUIT_LAYERS:
            continue
        try:
            zone_geometry = _geojson(zone.get("geometry"), label=layer_code)
            zone_projected = _valid_geometry(transform(project, zone_geometry))
            intersection = _valid_geometry(parcel_projected.intersection(zone_projected))
        except (AnalyticsError, ValueError) as exc:
            skipped.append(f"{layer_code}: {exc}")
            continue

        area_m2 = intersection.area
        if area_m2 <= 0:
            continue
        intersections.append(intersection)
        results.append(
            {
                "layer_code": layer_code,
                "layer_title": zone.get("layer_title") or layer_code,
                "reg_number": zone.get("reg_number") or zone.get("reg_numb"),
                "intersection_m2": area_m2,
                "parcel_share_pct": area_m2 / parcel_area_m2 * 100,
                "api_overlap_m2": zone.get("overlap_m2"),
            }
        )

    union_area_m2 = unary_union(intersections).area if intersections else 0.0
    return {
        "parcel_area_m2": parcel_area_m2,
        "zones": results,
        "union_intersection_m2": union_area_m2,
        "union_parcel_share_pct": union_area_m2 / parcel_area_m2 * 100,
        "skipped": skipped,
    }


def calculate_social_infrastructure(payload: Mapping) -> dict:
    """Filter social objects by the expanded minimum bounding circle."""
    parcel_block = payload.get("parcel")
    environment = payload.get("environment")
    if not isinstance(parcel_block, Mapping):
        raise AnalyticsError("Ответ API не содержит блока parcel")
    if not isinstance(environment, Mapping):
        raise AnalyticsError("Ответ API не содержит блока environment")

    parcel_geometry = _geojson(parcel_block.get("geometry"), label="участок")
    project = _local_equal_area_transformer(parcel_geometry)
    parcel_projected = _valid_geometry(transform(project, parcel_geometry))

    bounding_circle = minimum_bounding_circle(parcel_projected)
    center = bounding_circle.centroid
    minimum_radius_m = float(minimum_bounding_radius(parcel_projected))
    target_radius_m = minimum_radius_m + SOCIAL_RADIUS_EXTENSION_M
    target_circle = center.buffer(target_radius_m)

    social = environment.get("social", {})
    if not isinstance(social, Mapping):
        raise AnalyticsError("Ответ API не содержит социальной инфраструктуры")

    categories = []
    skipped = []
    seen = set()
    total_inside = 0
    total_outside = 0

    for category in SOCIAL_CATEGORIES:
        block = social.get(category, {})
        if not isinstance(block, Mapping):
            continue
        objects = block.get("objects", [])
        if not isinstance(objects, list):
            continue
        category_objects = []
        for index, item in enumerate(objects):
            if not isinstance(item, Mapping):
                skipped.append(f"{category} #{index + 1}: некорректный объект")
                continue
            location = item.get("location")
            if not isinstance(location, Mapping):
                skipped.append(f"{category} #{index + 1}: отсутствуют координаты")
                continue
            try:
                point_wgs84 = Point(float(location["lon"]), float(location["lat"]))
            except (KeyError, TypeError, ValueError):
                skipped.append(f"{category} #{index + 1}: некорректные координаты")
                continue

            reference = item.get("osm_ref")
            identity = reference or (
                category,
                round(point_wgs84.x, 7),
                round(point_wgs84.y, 7),
                item.get("name"),
            )
            if identity in seen:
                continue
            seen.add(identity)

            point_projected = transform(project, point_wgs84)
            inside = target_circle.covers(point_projected)
            distance_m = parcel_projected.distance(point_projected) if inside else math.inf
            total_inside += int(inside)
            total_outside += int(not inside)
            category_objects.append(
                {
                    "name": item.get("name") or item.get("kind") or "Без названия",
                    "kind": item.get("kind"),
                    "osm_ref": reference,
                    "inside_target_circle": inside,
                    "distance_m": distance_m,
                }
            )

        category_objects.sort(
            key=lambda obj: (not obj["inside_target_circle"], obj["distance_m"], obj["name"])
        )
        categories.append(
            {
                "code": category,
                "title": SOCIAL_CATEGORY_TITLES[category],
                "source_status": block.get("status"),
                "source_count_total": block.get("count_total"),
                "objects": category_objects,
            }
        )

    return {
        "minimum_radius_m": minimum_radius_m,
        "radius_extension_m": SOCIAL_RADIUS_EXTENSION_M,
        "target_radius_m": target_radius_m,
        "categories": categories,
        "total_inside": total_inside,
        "total_outside": total_outside,
        "low_coverage": bool(environment.get("low_coverage", False)),
        "skipped": skipped,
    }


def _fetch_json(url: str, api_key: str) -> dict:
    request = Request(
        url,
        headers={"Accept": "application/json", "X-API-Key": api_key},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("error", {}).get("message")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = None
        raise AnalyticsError(message or f"UCHASTOK API вернул HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise AnalyticsError(f"Не удалось обратиться к UCHASTOK API: {exc}") from exc


def _fetch_zones(cadastral_number: str, depth: str) -> dict:
    api_key = os.environ.get("UCHASTOK_API_KEY")
    if not api_key:
        raise AnalyticsError("Не задана переменная окружения UCHASTOK_API_KEY")

    encoded_number = quote(cadastral_number, safe="")
    parcel_url = f"{API_BASE_URL}/parcels/{encoded_number}?depth=minimal"
    zones_query = urlencode(
        {
            "layers": ZOUIT_LAYERS,
            "geometry": "true",
            "depth": depth,
        },
        doseq=True,
    )
    zones_url = f"{API_BASE_URL}/parcels/{encoded_number}/zones?{zones_query}"

    parcel = _fetch_json(parcel_url, api_key)
    zones = _fetch_json(zones_url, api_key)
    return {"parcel": parcel, "zones": zones}


def _fetch_social_environment(cadastral_number: str) -> dict:
    api_key = os.environ.get("UCHASTOK_API_KEY")
    if not api_key:
        raise AnalyticsError("Не задана переменная окружения UCHASTOK_API_KEY")
    encoded_number = quote(cadastral_number, safe="")
    parcel_url = f"{API_BASE_URL}/parcels/{encoded_number}?depth=minimal"
    environment_url = f"{API_BASE_URL}/environment/{encoded_number}"
    return {
        "parcel": _fetch_json(parcel_url, api_key),
        "environment": _fetch_json(environment_url, api_key),
    }


def _format_report(cadastral_number: str, result: Mapping) -> str:
    lines = [
        f"Пересечения ЗОУИТ для участка {cadastral_number}",
        f"Площадь участка по геометрии: {result['parcel_area_m2']:,.2f} м²",
        "",
    ]
    zones = result["zones"]
    if not zones:
        lines.append("ЗОУИТ с геометрически подтверждённым пересечением не найдены.")
    else:
        for number, zone in enumerate(zones, start=1):
            registration = (
                f", реестровый номер {zone['reg_number']}" if zone["reg_number"] else ""
            )
            lines.append(
                f"{number}. {zone['layer_title']} [{zone['layer_code']}]{registration}: "
                f"{zone['intersection_m2']:,.2f} м² "
                f"({zone['parcel_share_pct']:.2f}% участка)"
            )
        lines.extend(
            [
                "",
                "Объединённое пересечение без двойного счёта: "
                f"{result['union_intersection_m2']:,.2f} м² "
                f"({result['union_parcel_share_pct']:.2f}% участка).",
            ]
        )
    if result["skipped"]:
        lines.extend(["", "Не удалось обработать:", *[f"- {item}" for item in result["skipped"]]])
    lines.extend(
        [
            "",
            "Расчёт выполнен Shapely в локальной равноплощадной проекции LAEA.",
            "Данные НСПД не являются выпиской из ЕГРН и не имеют юридической силы.",
        ]
    )
    return "\n".join(lines)


def _format_social_report(cadastral_number: str, result: Mapping) -> str:
    lines = [
        f"Социальная инфраструктура вокруг участка {cadastral_number}",
        f"Минимальный радиус: {result['minimum_radius_m']:,.2f} м",
        f"Добавлено к радиусу: {result['radius_extension_m']:,.0f} м",
        f"Целевой радиус: {result['target_radius_m']:,.2f} м",
        f"Внутри окружности: {result['total_inside']}; снаружи: {result['total_outside']}",
        "",
    ]
    for category in result["categories"]:
        objects = category["objects"]
        lines.append(
            f"{category['title']} — получено {len(objects)}, "
            f"источник сообщает {category['source_count_total'] or 0}"
        )
        if not objects:
            lines.append("  Объекты не получены.")
            continue
        for number, item in enumerate(objects, start=1):
            distance = f"{item['distance_m']:,.1f} м" if math.isfinite(item["distance_m"]) else "∞"
            reference = f" [{item['osm_ref']}]" if item["osm_ref"] else ""
            lines.append(f"  {number}. {item['name']}{reference} — {distance}")
        lines.append("")
    if result["low_coverage"]:
        lines.append("Предупреждение: источник отметил низкое покрытие OSM.")
    if result["skipped"]:
        lines.extend(["Не удалось обработать:", *[f"- {item}" for item in result["skipped"]]])
    lines.extend(
        [
            "Расстояние рассчитано Shapely от участка до точки объекта; "
            "для объектов вне целевой окружности показано ∞.",
            "Список ограничен объектами, возвращёнными UCHASTOK API.",
            "Данные OpenStreetMap: © участники OpenStreetMap, ODbL 1.0.",
        ]
    )
    return "\n".join(lines)


def analyze_zouit_intersections(params, **kwargs):
    """Hermes tool handler. The returned text is displayed in the conversation."""
    del kwargs
    cadastral_number = str(params.get("cadastral_number", "")).strip()
    depth = str(params.get("depth", "standard"))
    if not CADASTRAL_NUMBER_RE.fullmatch(cadastral_number):
        return "Ошибка: кадастровый номер должен иметь формат AA:BB:CCCCCC(C):DD."
    if depth not in {"standard", "full"}:
        return "Ошибка: depth должен быть standard или full."
    try:
        payload = _fetch_zones(cadastral_number, depth)
        result = calculate_zouit_intersections(payload)
        return _format_report(cadastral_number, result)
    except AnalyticsError as exc:
        return f"Ошибка анализа ЗОУИТ: {exc}"


def analyze_social_infrastructure(params, **kwargs):
    """Hermes handler for minimum-circle social infrastructure analysis."""
    del kwargs
    cadastral_number = str(params.get("cadastral_number", "")).strip()
    if not CADASTRAL_NUMBER_RE.fullmatch(cadastral_number):
        return "Ошибка: кадастровый номер должен иметь формат AA:BB:CCCCCC(C):DD."
    try:
        payload = _fetch_social_environment(cadastral_number)
        result = calculate_social_infrastructure(payload)
        return _format_social_report(cadastral_number, result)
    except AnalyticsError as exc:
        return f"Ошибка анализа социальной инфраструктуры: {exc}"
