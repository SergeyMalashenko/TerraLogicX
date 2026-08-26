"""Streamlit dashboard for collected UCHASTOK datasets."""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st


DATASET_ENV = "UCHASTOK_DATASET_PATH"


def _database_path() -> Path:
    value = os.environ.get(DATASET_ENV)
    if not value:
        st.error(f"Не задана переменная {DATASET_ENV}")
        st.stop()
    path = Path(value).expanduser().resolve()
    path = path / "dataset.duckdb" if path.is_dir() else path
    if not path.is_file():
        st.error(f"База данных не найдена: {path}")
        st.stop()
    return path


@st.cache_data(show_spinner=False)
def query(database: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    connection = duckdb.connect(database, read_only=True)
    try:
        return connection.execute(sql, params).fetchdf()
    finally:
        connection.close()


def _as_object(value):
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return value


def _coordinates(geometry):
    if not isinstance(geometry, dict):
        return []
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return coordinates
    if geometry.get("type") == "MultiPolygon":
        return [ring for polygon in coordinates for ring in polygon]
    return []


def render_map(raw_path: str):
    try:
        import pydeck as pdk

        profile = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        parcel_geometry = (profile.get("parcel", {}).get("geometry", {}) or {}).get("geometry")
        features = []
        if parcel_geometry:
            features.append({
                "type": "Feature",
                "properties": {"kind": "Участок", "color": [28, 125, 85, 90]},
                "geometry": parcel_geometry,
            })
        for zone in (profile.get("zones", {}) or {}).get("zones", []):
            if zone.get("geometry"):
                features.append({
                    "type": "Feature",
                    "properties": {
                        "kind": zone.get("layer_title") or zone.get("layer_code"),
                        "color": [220, 45, 55, 55],
                    },
                    "geometry": zone["geometry"],
                })
        points = []
        for category, block in ((profile.get("environment", {}) or {}).get("social", {}) or {}).items():
            for item in block.get("objects", []) if isinstance(block, dict) else []:
                location = item.get("location") or {}
                if location.get("lat") is not None and location.get("lon") is not None:
                    points.append({
                        "lat": location["lat"], "lon": location["lon"],
                        "name": item.get("name") or item.get("kind"), "category": category,
                    })
        layers = [pdk.Layer(
            "GeoJsonLayer", {"type": "FeatureCollection", "features": features},
            pickable=True, stroked=True, filled=True, get_fill_color="properties.color",
            get_line_color=[20, 70, 50], line_width_min_pixels=2,
        )]
        if points:
            layers.append(pdk.Layer(
                "ScatterplotLayer", points, get_position="[lon, lat]", get_radius=18,
                get_fill_color=[30, 100, 220, 180], pickable=True,
            ))
        rings = _coordinates(parcel_geometry)
        flat = [point for ring in rings for point in ring]
        if not flat:
            st.info("В исходном профиле нет геометрии для карты")
            return
        longitude = sum(point[0] for point in flat) / len(flat)
        latitude = sum(point[1] for point in flat) / len(flat)
        st.pydeck_chart(pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(latitude=latitude, longitude=longitude, zoom=13),
            tooltip={"text": "{kind}{name}"},
            map_style=None,
        ))
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        st.warning(f"Карта недоступна: {exc}")


def main():
    st.set_page_config(page_title="UCHASTOK — анализ набора", layout="wide")
    database = str(_database_path())
    st.title("UCHASTOK — анализ земельных участков")

    runs = query(database, "SELECT run_id, status, finished_at FROM collection_runs ORDER BY started_at DESC")
    if runs.empty:
        st.warning("В базе пока нет запусков")
        return
    run_id = st.sidebar.selectbox("Запуск", runs["run_id"].tolist())
    data = query(database, """
        SELECT p.cadastral_number, p.status, p.partial, p.raw_path,
               f.area_registry_m2, f.cost_value, f.cost_per_m2, f.category,
               f.permitted_use, f.ownership_type, f.zouit_count,
               least(f.zouit_union_pct, 100.0) AS zouit_union_pct,
               f.social_inside_count, f.target_circle_radius_m,
               f.critical_risk_count, f.high_risk_count, f.data_completeness
        FROM parcels p LEFT JOIN features f USING (run_id, cadastral_number)
        WHERE p.run_id=? ORDER BY p.cadastral_number
    """, (run_id,))

    statuses = st.sidebar.multiselect("Статус", sorted(data["status"].dropna().unique()), default=sorted(data["status"].dropna().unique()))
    categories = sorted(data["category"].dropna().unique())
    selected_categories = st.sidebar.multiselect("Категория земель", categories, default=categories)
    filtered = data[data["status"].isin(statuses)]
    if categories:
        filtered = filtered[filtered["category"].isin(selected_categories)]

    total, completed, partial, failed = st.columns(4)
    total.metric("Участков", len(filtered))
    completed.metric("Успешно", int((filtered["status"] == "completed").sum()))
    partial.metric("Частично", int((filtered["status"] == "partial").sum()))
    failed.metric("Ошибки", int((filtered["status"] == "failed").sum()))

    overview, restrictions, social, detail = st.tabs([
        "Обзор", "ЗОУИТ", "Инфраструктура", "Карточка участка",
    ])
    with overview:
        st.subheader("Сравнение участков")
        chart = filtered.dropna(subset=["area_registry_m2", "cost_per_m2"])
        if not chart.empty:
            st.scatter_chart(chart, x="area_registry_m2", y="cost_per_m2", size="social_inside_count", color="zouit_union_pct")
        columns = ["cadastral_number", "status", "area_registry_m2", "cost_per_m2", "zouit_union_pct", "social_inside_count", "critical_risk_count", "high_risk_count"]
        st.dataframe(filtered[columns], width="stretch", hide_index=True)
        st.download_button("Скачать выборку CSV", filtered.to_csv(index=False).encode("utf-8-sig"), f"{run_id}_selection.csv", "text/csv")

    with restrictions:
        zones = query(database, """
            SELECT cadastral_number, layer_title, reg_number,
                   round(overlap_m2, 2) AS overlap_m2,
                   round(least(overlap_ratio, 100.0), 3) AS parcel_share_pct,
                   building_impact
            FROM zones WHERE run_id=? ORDER BY overlap_m2 DESC
        """, (run_id,))
        zones = zones[zones["cadastral_number"].isin(filtered["cadastral_number"])]
        st.metric("Пересечений", len(zones))
        if not zones.empty:
            top = zones.groupby("layer_title", as_index=False)["overlap_m2"].sum().nlargest(15, "overlap_m2")
            st.bar_chart(top, x="layer_title", y="overlap_m2")
        st.dataframe(zones, width="stretch", hide_index=True)

    with social:
        objects = query(database, """
            SELECT cadastral_number, category, name, kind, inside_target_circle,
                   round(distance_m, 1) AS distance_m
            FROM social_objects WHERE run_id=? ORDER BY distance_m
        """, (run_id,))
        objects = objects[objects["cadastral_number"].isin(filtered["cadastral_number"])]
        inside_only = st.checkbox("Только внутри целевой окружности", value=True)
        if inside_only:
            objects = objects[objects["inside_target_circle"]]
        if not objects.empty:
            counts = objects.groupby("category", as_index=False).size()
            st.bar_chart(counts, x="category", y="size")
        st.dataframe(objects, width="stretch", hide_index=True)

    with detail:
        if filtered.empty:
            st.info("Нет участков, соответствующих фильтрам")
        else:
            number = st.selectbox("Кадастровый номер", filtered["cadastral_number"].tolist())
            row = filtered[filtered["cadastral_number"] == number].iloc[0]
            a, b, c, d = st.columns(4)
            a.metric("Площадь, м²", f"{row['area_registry_m2']:,.1f}" if pd.notna(row["area_registry_m2"]) else "—")
            b.metric("Стоимость за м²", f"{row['cost_per_m2']:,.2f}" if pd.notna(row["cost_per_m2"]) else "—")
            c.metric("ЗОУИТ", int(row["zouit_count"]) if pd.notna(row["zouit_count"]) else 0)
            d.metric("Покрытие ЗОУИТ", f"{row['zouit_union_pct']:.2f}%" if pd.notna(row["zouit_union_pct"]) else "—")
            tasks = query(database, "SELECT payload_json FROM annotation_tasks WHERE run_id=? AND cadastral_number=?", (run_id, number))
            if not tasks.empty:
                payload = _as_object(tasks.iloc[0]["payload_json"])
                st.subheader("Риски")
                st.dataframe(pd.DataFrame(payload.get("risks", [])), width="stretch", hide_index=True)
                if payload.get("data_quality", {}).get("partial"):
                    st.warning("Данные частичные. Отсутствуют блоки: " + ", ".join(payload["data_quality"].get("missing_blocks", [])))
            if isinstance(row["raw_path"], str):
                st.subheader("Карта участка, зон и социальных объектов")
                render_map(row["raw_path"])


if __name__ == "__main__":
    main()
