"""Hermes registration for the UCHASTOK analytics tool."""

from pathlib import Path

from .schemas import SOCIAL_INFRASTRUCTURE_SCHEMA, ZOUIT_INTERSECTIONS_SCHEMA
from .tools import analyze_social_infrastructure, analyze_zouit_intersections


def register(ctx):
    ctx.register_tool(
        name="analyze_zouit_intersections",
        toolset="uchastok",
        schema=ZOUIT_INTERSECTIONS_SCHEMA,
        handler=analyze_zouit_intersections,
    )
    ctx.register_tool(
        name="analyze_social_infrastructure",
        toolset="uchastok",
        schema=SOCIAL_INFRASTRUCTURE_SCHEMA,
        handler=analyze_social_infrastructure,
    )
    ctx.register_skill("uchastok", Path(__file__).parent / "skills" / "uchastok")
