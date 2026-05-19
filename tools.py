"""Aria tool registry, definitions, and dispatcher.

AGENT_TOOL_DEFS is what we send inside session.update.tools[].
dispatch_tool() runs the actual handler when xAI emits a function_call.

Handlers live under ../tools/ — kept separate so they can be unit-tested
without spinning up a WebSocket.

Note: the upstream Vapi assistant being replicated is named "Aria" — that
external entity stays "Aria" for parity testing purposes. Our internal
agent identifier is "aria" (matching the persona spoken on calls).
"""

from __future__ import annotations

import json
from typing import Any

# Import handlers — these are normal async functions that take **kwargs and
# return a JSON-serializable dict.
from tools_pkg import (  # type: ignore[import-not-found]
    end_call,
    hubspot_book_meeting,
    hubspot_get_availability,
    send_sms_summary_openphone,
    transfer_call,
)

from logging_config import log


AGENT_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "send_sms_summary_openphone",
        "description": "Send an SMS via OpenPhone containing a short call summary to an attorney before/during transfer.",
        "parameters": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient phone number in E.164 format, e.g. +15551234567"},
                "from":    {"type": "string", "description": "Sender OpenPhone number in E.164 format. If omitted, defaultFrom will be used."},
                "content": {"type": "string", "description": "SMS body content. Should be a concise summary of the call."},
            },
            "required": ["to", "content"],
        },
    },
    {
        "type": "function",
        "name": "hubspot_get_availability_v3",
        "description": (
            "Fetch HubSpot meeting availability and return two deterministic options "
            "(earliest slot and next calendar-day slot). Returns canonical startTimeMillisUtc "
            "for safe booking. No timestamp computation should be done by the model."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dayOffset": {"type": "number", "description": "Number of business days after the earliest available date to check. Defaults to 1."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "hubspot_book_meeting_v3",
        "description": (
            "Books a confirmed 15-minute HubSpot meeting after availability is confirmed. "
            "Sends booking request to Make.com webhook which handles HubSpot API call with OAuth."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "email":              {"type": "string", "description": "Caller email"},
                "phone":              {"type": "string", "description": "Caller phone number"},
                "lastName":           {"type": "string", "description": "Caller last name"},
                "firstName":          {"type": "string", "description": "Caller first name"},
                "description":        {"type": "string", "description": "Brief description of caller's question"},
                "startTimeMillisUtc": {"type": "number", "description": "UTC epoch milliseconds for meeting start time"},
            },
            "required": ["firstName", "lastName", "email", "phone", "description", "startTimeMillisUtc"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "transferCall_v3",
        "description": (
            "Transfer the live call. Use ONLY in two scenarios: "
            "(1) Caller requests voicemail — destination +15555550101, reason 'voicemail'. "
            "(2) Warm transfer to attorney +15555550100 (reason 'warm_transfer_attorney') when ALL of: "
            "valid real estate transaction; details validated (property + closing date + documents/issue); "
            "immediate/urgent need (closing within 14-30 days OR urgent keywords OR time-sensitive deadline); "
            "caller has agreed to be transferred. "
            "YOU MUST INVOKE THIS FUNCTION to actually transfer — speaking 'I'll transfer you' or "
            "'let me connect you' or 'one moment while I get you connected' alone does NOT transfer; "
            "the call STAYS HELD in your hands until this function_call is emitted. "
            "Sequence: (1) speak ONE brief declarative connect line ending in a period — e.g., "
            "\"I understand this is time-sensitive. Let me connect you with one of our real estate "
            "attorneys right now.\" — and (2) IMMEDIATELY emit this function call with destination, "
            "reason, and a complete `summary` argument. The summary is spoken aloud to the attorney "
            "via Twilio TTS when they pick up; no SMS heads-up is sent (that step is obsolete). "
            "Write the summary for ear: caller full name, buyer/seller status, property address, "
            "closing date, document/issue, urgency level. Do NOT wait for caller acknowledgement "
            "before invoking. Do NOT repeat 'I'll transfer you' across multiple turns — either "
            "invoke the tool now or continue collecting the required info first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "enum": ["+15555550101", "+15555550100"],
                    "description": "+15555550101 voicemail; +15555550100 attorney.",
                },
                "reason": {
                    "type": "string",
                    "enum": ["voicemail", "warm_transfer_attorney"],
                    "description": "Which scenario applies.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-paragraph call summary spoken to the attorney before bridging (warm_transfer_attorney only).",
                },
            },
            "required": ["destination", "reason"],
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": (
            "Terminate the live phone call. You MUST invoke this function once the "
            "conversation has reached a natural conclusion — caller said goodbye, all "
            "questions answered, message taken, or appointment confirmed. The call "
            "STAYS OPEN until you invoke this tool — speaking 'goodbye' alone does not "
            "hang up the line. Sequence: (1) speak a brief closing line "
            "(e.g., \"You're all set! Have a great day. Goodbye.\"), (2) immediately "
            "invoke this tool — do not wait for a further caller response. Do NOT use "
            "this to escape difficult conversations or to skip required steps."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


HANDLERS = {
    "send_sms_summary_openphone": send_sms_summary_openphone.handle,
    "hubspot_get_availability_v3": hubspot_get_availability.handle,
    "hubspot_book_meeting_v3": hubspot_book_meeting.handle,
    "transferCall_v3": transfer_call.handle,
    "end_call": end_call.handle,
}


async def dispatch_tool(name: str, args_json: str, *, call_sid: str, call_id: str) -> dict[str, Any]:
    """Run the named tool. Returns a JSON-serializable dict for function_call_output.output."""
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
    except json.JSONDecodeError as exc:
        log.error("tool.bad_args", call_id=call_id, name=name, error=str(exc))
        return {"error": "invalid_arguments", "detail": str(exc)}

    handler = HANDLERS.get(name)
    if handler is None:
        log.error("tool.unknown", call_id=call_id, name=name)
        return {"error": "unknown_tool", "name": name}

    log.info("tool.start", call_id=call_id, name=name, args=_sanitize(args))
    try:
        result = await handler(call_sid=call_sid, call_id=call_id, **args)
        log.info("tool.ok", call_id=call_id, name=name)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("tool.failed", call_id=call_id, name=name)
        return {"error": "tool_exception", "detail": str(exc)}


def _sanitize(args: dict[str, Any]) -> dict[str, Any]:
    """Drop / mask anything sensitive before logging.

    `content` (SMS body in send_sms_summary_openphone) and `summary` (warm-transfer
    paragraph in transferCall_v3) can both contain caller PII / legal matter
    details — Fly.io structured logs are persisted indefinitely, so truncate
    rather than passing them through verbatim.
    """
    redacted = dict(args)
    for k in ("email", "phone"):
        if k in redacted and isinstance(redacted[k], str):
            redacted[k] = redacted[k][:3] + "***"
    for k in ("content", "summary", "description"):
        if k in redacted and isinstance(redacted[k], str):
            v = redacted[k]
            redacted[k] = (v[:32] + "…") if len(v) > 32 else v
    return redacted


# ---------------------------------------------------------------------------
# Chat-completions variants for the admin Playground
# ---------------------------------------------------------------------------
#
# The xAI Realtime API expects flat tool entries:
#     {"type": "function", "name": ..., "description": ..., "parameters": {...}}
# The xAI Chat Completions API (OpenAI-compatible) expects nested:
#     {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
# We derive one from the other so the two stay in lock-step automatically.

AGENT_TOOL_DEFS_CHAT: list[dict[str, Any]] = [
    {"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
    for t in AGENT_TOOL_DEFS
]


# Allow-list of tools the Playground may run for real (read-only / safe). Anything
# NOT in this set is stubbed — so a future tool added to AGENT_TOOL_DEFS doesn't
# silently start firing for real in the playground just because someone forgot
# to update an opt-out list. The default is "stubbed".
PLAYGROUND_LIVE_TOOLS: frozenset[str] = frozenset({
    "hubspot_get_availability_v3",
})


async def dispatch_tool_playground(name: str, args_json: str, *, call_id: str) -> dict[str, Any]:
    """Playground variant of dispatch_tool.

    Runs only the explicit allow-list (currently `hubspot_get_availability_v3` —
    a read-only HubSpot read) for real. Everything else returns a tool-shaped
    stub so the model's downstream reasoning stays honest, but no real SMS,
    bookings, transfers, or hangups happen from playground use.
    """
    if name not in PLAYGROUND_LIVE_TOOLS:
        try:
            args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
        except json.JSONDecodeError:
            args = {}
        log.info("tool.playground_stubbed", call_id=call_id, name=name)
        return {
            "ok": True,
            "stubbed": True,
            "tool": name,
            "args_received": _sanitize(args if isinstance(args, dict) else {}),
            "note": "Playground mode — tool was NOT executed (no SMS / no booking / no transfer / no hangup).",
        }
    # Real execution path with a synthetic call_sid.
    return await dispatch_tool(name, args_json, call_sid="playground", call_id=call_id)
