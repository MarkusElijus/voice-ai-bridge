"""End-of-call notification to Make.com (after-call-report scenario).

Posts a Vapi-shape `end-of-call-report` envelope to the Make.com webhook so
the existing post-call fanout (Google Chat card, Quo SMS to caller, Google
Sheets log, HubSpot contact lookup, transfer post-processing) keeps working
when Aria replaces Vapi-Aria.

Why Vapi-shape and not our own contract? The live Make.com scenario was
authored against Vapi's `end-of-call-report` payload — its modules reference
fields like `{{1.message.analysis.structuredData.first_name}}` and
`{{1.message.artifact.structuredOutputs.bf732bb0-...result}}` directly. We
emit those exact field paths so the scenario doesn't need to be rewritten
for the cutover. Once Aria is on the production phone number and stable,
we can author a Aria-native payload contract and migrate the scenario.

Failure handling: this module is best-effort. If the webhook is unset
(local dev / unconfigured) we silently no-op. If the POST fails we log and
move on — losing one after-call report is much less bad than letting
post_call.run crash mid-pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

import storage
from logging_config import log
from settings import settings
from xai_session import XaiVoiceSession

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ: Any = ZoneInfo("America/Chicago")
except Exception:  # noqa: BLE001 — Windows without tzdata, container without /usr/share/zoneinfo
    _LOCAL_TZ = timezone.utc


# GUIDs for the two `artifact.structuredOutputs` entries the Make.com scenario
# reads. The downstream scenario's mustache templates reference these IDs by
# value, so they must match the IDs used in the scenario's module config.
# Replace with your own GUIDs (matching the ones your scenario expects).
_STRUCTURED_OUTPUT_SUMMARY_ID = "00000000-0000-0000-0000-000000000001"
_STRUCTURED_OUTPUT_SUCCESS_ID = "00000000-0000-0000-0000-000000000002"

# Long-TTL signed URL for the recording. Make.com runs can span minutes
# (especially when AI nodes retry), and a single scenario execution may
# reference the URL more than once. 24 h is generous but cheap.
_RECORDING_URL_TTL_SECONDS = 24 * 3600


async def post_end_of_call_report(
    call_id: str,
    session: XaiVoiceSession,
    summary: Any,
    recording_path: str | None,
) -> None:
    """POST a Vapi-shape end-of-call-report to MAKE_VAPI_WEBHOOK_URL.

    `summary` is a CallSummary; typed as Any to keep this module free of a
    post_call import (post_call already imports this module → would cycle).
    """
    if not settings.MAKE_VAPI_WEBHOOK_URL:
        log.info("notify_make.skip_no_webhook", call_id=call_id)
        return

    try:
        recording_url = await _recording_signed_url(recording_path)
        payload = _build_vapi_envelope(
            call_id=call_id,
            session=session,
            summary=summary,
            recording_url=recording_url,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            resp = await client.post(
                settings.MAKE_VAPI_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code >= 400:
            log.error(
                "notify_make.failed",
                call_id=call_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
            return
        log.info(
            "notify_make.ok",
            call_id=call_id,
            status=resp.status_code,
            disposition=getattr(summary, "disposition", None),
            after_hours=getattr(summary, "after_hours", None),
            success_evaluation=getattr(summary, "success_evaluation", None),
            recording=bool(recording_url),
        )
    except Exception:  # noqa: BLE001
        log.exception("notify_make.exception", call_id=call_id)


async def _recording_signed_url(recording_path: str | None) -> str | None:
    """Generate a long-TTL signed URL for the recording so Make.com can
    fetch it during the scenario run + include it in the Chat card. Returns
    None if no recording exists or Supabase Storage isn't configured."""
    if not recording_path:
        return None
    return await storage.signed_url(recording_path, expires_in=_RECORDING_URL_TTL_SECONDS)


def _flat_transcript(session: XaiVoiceSession) -> str:
    """Render `transcript_turns` as Vapi's flat speaker-prefixed transcript.

    Vapi's `message.transcript` is a single string with each turn on its
    own line prefixed by `Caller:` / `AI:`. The Make.com scenario passes
    this verbatim into the email body and the AI nodes' input, so we mimic
    the shape exactly. If turn-level data isn't available, fall back to the
    two-block format `_format_transcript` produces in post_call.
    """
    turns = session.transcript_turns or []
    if turns:
        lines: list[str] = []
        for t in turns:
            role = t.get("role")
            text = (t.get("text") or "").strip()
            if not text:
                continue
            speaker = "Caller" if role == "caller" else "AI"
            lines.append(f"{speaker}: {text}")
        if lines:
            return "\n".join(lines)
    # Fallback: two blocks. Better than empty.
    caller = (session.caller_transcript or "").strip()
    bot = (session.bot_transcript or "").strip()
    parts: list[str] = []
    if caller:
        parts.append(f"Caller said overall: {caller}")
    if bot:
        parts.append(f"AI said overall: {bot}")
    return "\n\n".join(parts)


def _ended_reason(session: XaiVoiceSession, summary: Any) -> str:
    """Best-effort mapping to Vapi's `endedReason` values the scenario
    branches on. The transfer branch in module 55 specifically filters for
    `assistant-forwarded-call`; everything else flows through the standard
    notification branch."""
    # Was a transferCall_v3 tool invoked? -> Vapi's "assistant-forwarded-call"
    for tc in session.tool_calls:
        if tc.name == "transferCall_v3" and tc.error is None:
            return "assistant-forwarded-call"
    # Idle-give-up tear-down -> Vapi's "silence-timed-out"
    if session.end_source == "idle_timeout" or session.idle_consecutive_prompts >= 2:
        return "silence-timed-out"
    if session.end_source == "caller":
        return "customer-ended-call"
    # Default healthy completion.
    return "assistant-ended-call"


def _duration_minutes(session: XaiVoiceSession) -> float:
    if not session.ended_at:
        return 0.0
    return round(max(0.0, session.ended_at - session.started_at) / 60.0, 2)


def _iso_utc(epoch_seconds: float | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _call_date_display(epoch_seconds: float | None) -> str | None:
    """Format the call timestamp as 'MM/DD/YYYY HH:MM AM/PM' in America/Chicago.

    Falls back to UTC if zoneinfo isn't available (label changes to 'UTC' so
    consumers see they're not getting the firm's local time). The Make.com
    card pastes this verbatim — the operator explicitly asked for this format in the
    Google Chat card, so we render server-side and avoid Make's locale-
    dependent formatDate function entirely.
    """
    if epoch_seconds is None:
        return None
    dt = datetime.fromtimestamp(epoch_seconds, tz=_LOCAL_TZ)
    if _LOCAL_TZ is timezone.utc:
        return dt.strftime("%m/%d/%Y %I:%M %p UTC").replace(" 0", " ")  # trim leading 0 on hour
    # America/Chicago — show CDT/CST automatically per DST.
    tz_abbr = dt.strftime("%Z") or "CT"
    # `%I` is zero-padded hour; strip the leading zero so "03:27 PM" -> "3:27 PM"
    # matches the email-version look in the operator's screenshots.
    formatted = dt.strftime("%m/%d/%Y %I:%M %p")
    if formatted[11] == "0":
        formatted = formatted[:11] + formatted[12:]
    return f"{formatted} {tz_abbr}"


def _meeting_datetime_display(dt: datetime | None) -> str | None:
    """Format the scheduled meeting time as 'Wed May 13, 2026 at 02:30 PM CT'.

    Mirrors the admin dashboard's CALLER SUMMARY panel format the operator pointed at.
    """
    if dt is None:
        return None
    # Pydantic may give us a UTC dt; convert to local for display.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(_LOCAL_TZ)
    tz_abbr = dt_local.strftime("%Z") if _LOCAL_TZ is not timezone.utc else "UTC"
    return dt_local.strftime("%a %b %d, %Y at %I:%M %p") + (f" {tz_abbr}" if tz_abbr else "")


def _recommended_actions_html(actions: list[str] | None) -> str:
    """Render the recommended-actions list as a single-string HTML snippet that
    drops directly into a Google Chat `textParagraph` widget. Each action is
    prefixed with a ✅ for visual parity with the email version. Returns ""
    when the list is empty/None so the section gracefully collapses in the
    card if the LLM couldn't extract any actions."""
    if not actions:
        return ""
    safe = [a.replace("<", "&lt;").replace(">", "&gt;") for a in actions if a]
    return "<br>".join(f"✅ {a}" for a in safe)


def _yesno(b: bool | None) -> str | None:
    """Make.com filters compare against the literal strings 'Yes' / 'No'
    (e.g. module 72's `after_hours == "Yes"`). False becomes absent rather
    than 'No' for `after_hours` because the office-hours branch (mod 71)
    uses `notexist`, not equality."""
    if b is None:
        return None
    return "Yes" if b else "No"


def _build_vapi_envelope(
    *,
    call_id: str,
    session: XaiVoiceSession,
    summary: Any,
    recording_url: str | None,
) -> dict[str, Any]:
    """Assemble the `message` envelope. Field paths match what the downstream
    Make.com scenario's modules read via mustache references."""

    meeting_dt = getattr(summary, "meeting_datetime", None)
    meeting_scheduled_flag = getattr(summary, "meeting_scheduled", None)

    structured = {
        "first_name": getattr(summary, "first_name", None),
        "last_name": getattr(summary, "last_name", None),
        "caller_fullname": getattr(summary, "caller_fullname", None),
        "caller_email": getattr(summary, "caller_email", None),
        # Rename: our CallSummary field is `callback_number`; the scenario
        # reads `caller_number` (the Vapi structured-output field).
        "caller_number": getattr(summary, "callback_number", None),
        "caller_status": getattr(summary, "caller_status", None),
        "service_type": getattr(summary, "service_type", None),
        "call_outcome": getattr(summary, "call_outcome", None),
        # Rename: our `forward_msg_to` -> Vapi's `recipient`.
        "recipient": getattr(summary, "forward_msg_to", None),
        "sms_meeting_link": getattr(summary, "sms_meeting_link", None) or "No",
        # Meeting fields (new for the Google Chat card). meeting_scheduled is
        # rendered as Yes/No to match the other structured-data string flags;
        # meeting_datetime is pre-formatted human-readable for direct paste
        # into the card (Make.com doesn't have to call formatDate).
        "meeting_scheduled": "Yes" if meeting_scheduled_flag else "No",
        "meeting_datetime": _meeting_datetime_display(meeting_dt),
        "meeting_notes": getattr(summary, "meeting_notes", None),
    }
    # after_hours only appears in the payload when True — the scenario's
    # mod 71 branches on `notexist`, mod 72 on `== "Yes"`.
    if getattr(summary, "after_hours", False):
        structured["after_hours"] = "Yes"

    narrative = getattr(summary, "analysis_summary", None) or ""
    success_eval = getattr(summary, "success_evaluation", None)
    actions = getattr(summary, "recommended_actions", None) or []

    # Total cost = xAI + Twilio. Both are floats already in USD; sum as
    # float and emit as a string formatted to 4 decimals so the Make card
    # can paste it as-is (`$0.4123`) without bumping into Make's number
    # locale quirks.
    xai_cost = getattr(summary, "xai_cost_usd", None) or 0.0
    twilio_cost = getattr(summary, "twilio_cost_usd", None) or 0.0
    total_cost = round(float(xai_cost) + float(twilio_cost), 4)

    # Top-level Vapi message envelope.
    envelope: dict[str, Any] = {
        "message": {
            "type": "end-of-call-report",
            "endedReason": _ended_reason(session, summary),
            "timestamp": _iso_utc(session.ended_at) or _iso_utc(session.started_at),
            "startedAt": _iso_utc(session.started_at),
            # Pre-formatted human-readable call date for the card
            # ("MM/DD/YYYY H:MM AM/PM CT"). Server-side rendering avoids
            # Make.com's locale-dependent formatDate.
            "callDateTimeDisplay": _call_date_display(session.started_at),
            "durationMinutes": _duration_minutes(session),
            "transcript": _flat_transcript(session),
            "recordingUrl": recording_url,
            "summary": narrative,  # legacy top-level alias some Vapi versions use
            "totalCostUsd": f"{total_cost:.4f}",
            "totalCostUsdNumber": total_cost,  # raw number for arithmetic in Make
            "assistant": {"name": "Aria"},
            "call": {
                "id": call_id,
                "type": "inboundPhoneCall",
                "createdAt": _iso_utc(session.started_at),
                "customer": {"number": session.caller_number},
            },
            "analysis": {
                "summary": narrative,
                "successEvaluation": success_eval,
                "recommendedActions": list(actions),
                # Pre-rendered HTML snippet for direct paste into a Google
                # Chat textParagraph widget — Make.com avoids having to
                # iterate the list and concat <br>s itself.
                "recommendedActionsHtml": _recommended_actions_html(actions),
                "structuredData": structured,
            },
            "artifact": {
                "structuredOutputs": {
                    _STRUCTURED_OUTPUT_SUMMARY_ID: {
                        "result": narrative,
                    },
                    _STRUCTURED_OUTPUT_SUCCESS_ID: {
                        # Module 63 (Google Sheets row) reads .result as a
                        # string; emit the integer as str so toString-style
                        # mustache lookups Just Work.
                        "result": str(success_eval) if success_eval is not None else "",
                    },
                },
            },
            # Identifier the scenario uses for idempotency keys / per-call
            # de-duplication. Vapi uses its toolCallId here; we synthesize.
            "toolCallId": f"call_{uuid4().hex[:22]}",
        }
    }
    return envelope


# Re-export for tests / inspection. Not used at runtime.
__all__ = ["post_end_of_call_report"]
