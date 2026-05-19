"""Static assistant registry and policy helpers.

The prompts table already supports multiple ``agent_id`` values. This module
keeps the small amount of non-prompt assistant metadata in code: labels,
fallback prompt files, business-hours routing, and per-agent tool policy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools import AGENT_TOOL_DEFS


AGENT_ARIA = "aria"
AGENT_ARIA_AFTER_HOURS = "aria_after_hours"
DEFAULT_AGENT_ID = AGENT_ARIA

CT = ZoneInfo("America/Chicago")
PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    label: str
    description: str
    prompt_fallback_path: Path
    enabled_tools: tuple[str, ...]
    routing_note: str


AGENTS: dict[str, AgentConfig] = {
    AGENT_ARIA: AgentConfig(
        agent_id=AGENT_ARIA,
        label="Aria - Daytime",
        description="Business-hours intake, scheduling, voicemail, and urgent attorney warm transfer.",
        prompt_fallback_path=PROMPTS_DIR / "aria_instructions.md",
        enabled_tools=(
            "send_sms_summary_openphone",
            "hubspot_get_availability_v3",
            "hubspot_book_meeting_v3",
            "transferCall_v3",
            "end_call",
        ),
        routing_note=(
            "Selected during phone-service business hours: Monday-Thursday 8:00 AM-4:30 PM "
            "Central and Friday 8:00 AM-2:00 PM Central."
        ),
    ),
    AGENT_ARIA_AFTER_HOURS: AgentConfig(
        agent_id=AGENT_ARIA_AFTER_HOURS,
        label="Aria - After Hours",
        description="After-hours intake, appointment scheduling, message-taking, and voicemail only.",
        prompt_fallback_path=PROMPTS_DIR / "aria_after_hours_instructions.md",
        enabled_tools=(
            "hubspot_get_availability_v3",
            "hubspot_book_meeting_v3",
            "transferCall_v3",
            "end_call",
        ),
        routing_note=(
            "Selected outside phone-service business hours, including all Saturday and Sunday. "
            "Attorney warm transfer is blocked for this assistant."
        ),
    ),
}


_BUSINESS_WINDOWS: dict[int, tuple[time, time]] = {
    # Python weekday(): Monday=0, Sunday=6.
    0: (time(8, 0), time(16, 30)),
    1: (time(8, 0), time(16, 30)),
    2: (time(8, 0), time(16, 30)),
    3: (time(8, 0), time(16, 30)),
    4: (time(8, 0), time(14, 0)),
}


def normalize_agent_id(agent_id: str | None) -> str:
    """Return a known agent id, falling back to daytime Aria."""
    if agent_id and agent_id in AGENTS:
        return agent_id
    return DEFAULT_AGENT_ID


def get_agent_config(agent_id: str | None) -> AgentConfig:
    return AGENTS[normalize_agent_id(agent_id)]


def list_agent_configs() -> list[AgentConfig]:
    return list(AGENTS.values())


def is_business_hours(dt: datetime | None = None, *, friday_close_time: time | None = None) -> bool:
    """Return True when ``dt`` is inside the phone-service business-hours schedule.

    Boundaries are start-inclusive and end-exclusive: 8:00 AM is daytime;
    4:30 PM Monday-Thursday and 2:00 PM Friday are after-hours. A
    ``friday_close_time`` override only applies to Friday and is intended for
    one-off early closures.
    """
    current = dt or datetime.now(CT)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CT)
    local = current.astimezone(CT)
    window = _BUSINESS_WINDOWS.get(local.weekday())
    if window is None:
        return False
    start, end = window
    if local.weekday() == 4 and friday_close_time is not None:
        end = min(end, friday_close_time)
    local_time = local.time()
    return start <= local_time < end


def agent_for_datetime(dt: datetime | None = None, *, friday_close_time: time | None = None) -> str:
    return AGENT_ARIA if is_business_hours(dt, friday_close_time=friday_close_time) else AGENT_ARIA_AFTER_HOURS


def tool_defs_for_agent(agent_id: str | None) -> list[dict[str, Any]]:
    """Return xAI realtime tool definitions filtered for the selected agent."""
    config = get_agent_config(agent_id)
    enabled = set(config.enabled_tools)
    tools = [deepcopy(tool) for tool in AGENT_TOOL_DEFS if tool.get("name") in enabled]

    if config.agent_id == AGENT_ARIA_AFTER_HOURS:
        for tool in tools:
            if tool.get("name") == "transferCall_v3":
                tool["description"] = (
                    "Transfer the live call to the Acme Law voicemail box when the caller "
                    "explicitly asks to leave a voicemail. For this after-hours assistant, use "
                    "destination +15555550101 and reason 'voicemail' only. Do not use this for "
                    "live staff handoff."
                )
                props = tool["parameters"]["properties"]
                props["destination"]["enum"] = ["+15555550101"]
                props["destination"]["description"] = "+15555550101 voicemail."
                props["reason"]["enum"] = ["voicemail"]
                props["reason"]["description"] = "Voicemail transfer only."
                props.pop("summary", None)
                break

    return tools


def chat_tool_defs_for_agent(agent_id: str | None) -> list[dict[str, Any]]:
    """Return OpenAI-compatible chat tool definitions for the selected agent."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for tool in tool_defs_for_agent(agent_id)
    ]


def validate_tool_call(agent_id: str | None, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error payload when a tool call violates the assistant policy."""
    config = get_agent_config(agent_id)
    if name not in config.enabled_tools:
        return {
            "error": "tool_not_allowed",
            "detail": f"{name} is not enabled for {config.label}.",
        }

    if config.agent_id == AGENT_ARIA_AFTER_HOURS and name == "transferCall_v3":
        destination = args.get("destination")
        reason = args.get("reason")
        if destination != "+15555550101" or reason != "voicemail":
            return {
                "error": "tool_not_allowed",
                "detail": "After-hours Aria may only transfer callers to voicemail.",
            }

    return None
