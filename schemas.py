"""Schemas exposed to Hermes."""

ZOUIT_INTERSECTIONS_SCHEMA = {
    "name": "analyze_zouit_intersections",
    "description": (
        "Получает геометрию участка и всех найденных ЗОУИТ через UCHASTOK REST, "
        "пересчитывает пересечения в Shapely и выводит площади в квадратных метрах."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cadastral_number": {
                "type": "string",
                "description": "Кадастровый номер в каноническом виде, например 52:18:0070045:174.",
                "pattern": r"^\d{2}:\d{2}:\d{6,7}:\d+$",
            },
            "depth": {
                "type": "string",
                "enum": ["standard", "full"],
                "default": "standard",
                "description": "Глубина получения реквизитов зон.",
            },
        },
        "required": ["cadastral_number"],
        "additionalProperties": False,
    },
}

SOCIAL_INFRASTRUCTURE_SCHEMA = {
    "name": "analyze_social_infrastructure",
    "description": (
        "Строит минимальную описывающую окружность участка, увеличивает её "
        "радиус на 1000 метров и рассчитывает Shapely-расстояния до объектов "
        "социальной инфраструктуры. Для объектов вне окружности выводит ∞."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cadastral_number": {
                "type": "string",
                "description": "Кадастровый номер, например 52:18:0070045:174.",
                "pattern": r"^\d{2}:\d{2}:\d{6,7}:\d+$",
            }
        },
        "required": ["cadastral_number"],
        "additionalProperties": False,
    },
}
