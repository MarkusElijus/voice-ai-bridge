"""End-of-call notification to Google Chat (direct, no Make.com middleman).

Posts a fully-rendered cardsV2 card to GOOGLE_CHAT_WEBHOOK_URL. Bypasses the
Make.com scenario for the card path because Make's HTTP module performs
mustache substitution into a JSON STRING body — transcripts/summaries
containing `"` or newline characters break the resulting JSON validation
(InvalidConfigurationError run 9484d4d6a9ed4daa945d3ec1f005d67b on
2026-05-11). Make.com still receives the same payload via notify_make for
the other after-call fanouts (Quo SMS, Sheets row, HubSpot contact lookup,
transfer post-processing) — only the Chat card path moved here.

Variations: office-hours vs after-hours differ only in the header (title
+ subtitle). All other content is the same. The branch picker is the
server-computed `summary.after_hours` flag (post_call._is_after_hours).

Best-effort: failures (HTTP error, missing webhook, exceptions) are caught
+ logged. They must never crash post_call.run.
"""

from __future__ import annotations

from typing import Any

import httpx

from logging_config import log
from notify_make import (
    _call_date_display,
    _duration_minutes,
    _flat_transcript,
    _meeting_datetime_display,
    _recommended_actions_html,
)
from settings import settings
from xai_session import XaiVoiceSession


_LOGO_URL = "https://acme-voice-agent.fly.dev/admin/static/logo.svg"
_DASHBOARD_BASE = "https://acme-voice-agent.fly.dev/admin/calls"

# Google Chat textParagraph hard limit is ~4000 chars. Truncate the
# transcript with a marker so a 30-minute call doesn't blow past it and
# trigger a 400 from the Chat API.
_TRANSCRIPT_MAX_CHARS = 3800

# ---------------------------------------------------------------------------
# Color theme (text-only — cardsV2 doesn't support background colors)
#
# cardsV2 widgets can't have backgrounds, so we get visual separation from:
#   1. Distinct color-emoji prefix on each section header (🔵🟢🟠🟣...)
#   2. Tinted topLabel text on the decorated widgets per section
#   3. After-hours variant uses a darker/redder palette for visual urgency
#
# If the operator wants true email-style soft-pastel backgrounds, that requires
# either (a) rendering the report as a PNG image and attaching it to the
# message, or (b) hosting a polished HTML report at /admin/calls/<id>/report
# and linking via the "View Full Report" button. Either is a follow-up.
# ---------------------------------------------------------------------------

_THEME_OFFICE = {
    "caller_info":      ("🔵", "#1d4ed8"),  # blue
    "call_details":     ("🟢", "#047857"),  # emerald
    "call_summary":     ("📋", "#92400e"),  # amber-brown
    "call_transcript":  ("📝", "#555555"),  # neutral gray (existing)
    "next_steps":       ("🟠", "#b45309"),  # orange
    "meeting":          ("🟣", "#6d28d9"),  # violet
    "call_recording":   ("🎵", "#0e7490"),  # cyan
    "footer":           ("",   "#888888"),
}

_THEME_AFTER = {
    "caller_info":      ("🌙", "#7c2d12"),  # dark sienna
    "call_details":     ("🌃", "#7f1d1d"),  # dark crimson
    "call_summary":     ("🌑", "#78350f"),  # dark amber
    "call_transcript":  ("📜", "#3f3f46"),  # graphite
    "next_steps":       ("🚨", "#991b1b"),  # alert red
    "meeting":          ("🌙", "#4c1d95"),  # dark violet
    "call_recording":   ("🎵", "#0c4a6e"),  # dark cyan
    "footer":           ("",   "#52525b"),
}


async def post_chat_card(
    call_id: str,
    session: XaiVoiceSession,
    summary: Any,
    recording_url: str | None,
) -> None:
    """POST a cardsV2 after-call card to GOOGLE_CHAT_WEBHOOK_URL.

    `summary` is a CallSummary; typed as Any to match notify_make's pattern
    and avoid a post_call import (which would cycle).
    """
    if not settings.GOOGLE_CHAT_WEBHOOK_URL:
        log.info("notify_chat.skip_no_webhook", call_id=call_id)
        return

    try:
        card = _build_card(
            call_id=call_id,
            session=session,
            summary=summary,
            recording_url=recording_url,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            resp = await client.post(
                settings.GOOGLE_CHAT_WEBHOOK_URL,
                json=card,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code >= 400:
            log.error(
                "notify_chat.failed",
                call_id=call_id,
                status=resp.status_code,
                # Redact the webhook URL if Chat ever echoes it back.
                body=resp.text[:300].replace("chat.googleapis.com", "<chat-host-redacted>"),
            )
            return
        log.info(
            "notify_chat.ok",
            call_id=call_id,
            status=resp.status_code,
            after_hours=bool(getattr(summary, "after_hours", False)),
            recording=bool(recording_url),
        )
    except Exception:  # noqa: BLE001
        log.exception("notify_chat.exception", call_id=call_id)


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------

def _decorated(label: str, value: Any, accent: str | None = None) -> dict[str, Any]:
    """A `decoratedText` widget with a top label and text. Coerces None ->
    em dash so Chat doesn't render literal 'None'. If `accent` is provided,
    the topLabel is tinted with that hex color via <font> for visual
    section grouping (cardsV2 doesn't support real section backgrounds)."""
    text = str(value) if value is not None and value != "" else "—"
    if accent:
        label = f'<font color="{accent}">{label}</font>'
    return {"decoratedText": {"topLabel": label, "text": text}}


def _two_col(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap two widget lists in a `columns` widget for side-by-side rendering.
    `horizontalSizeStyle: FILL_AVAILABLE_SPACE` keeps both columns balanced;
    Google Chat collapses to single-column on narrow mobile widths."""
    return {
        "columns": {
            "columnItems": [
                {
                    "horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                    "verticalAlignment": "TOP",
                    "widgets": left,
                },
                {
                    "horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                    "verticalAlignment": "TOP",
                    "widgets": right,
                },
            ]
        }
    }


def _truncate_transcript(text: str) -> str:
    if len(text) <= _TRANSCRIPT_MAX_CHARS:
        return text
    cut = text[:_TRANSCRIPT_MAX_CHARS]
    # Don't cut mid-line — back up to the last newline so the truncation
    # marker reads cleanly.
    last_nl = cut.rfind("\n")
    if last_nl > 0:
        cut = cut[:last_nl]
    return cut + "\n…[transcript truncated for Chat display; full text in dashboard]"


def _transcript_for_chat(session: XaiVoiceSession) -> str:
    """Render the transcript flat (AI: / Caller:), then convert literal
    newlines to <br> so Google Chat's textParagraph renders the line breaks
    visibly. Google Chat's textParagraph supports limited HTML — <br> works,
    a `\\n` in the value renders as one space."""
    raw = _flat_transcript(session) or "(no transcript captured)"
    truncated = _truncate_transcript(raw)
    # Escape angle brackets that could be in caller speech ("<3", etc.) so
    # Chat doesn't think they're HTML tags. Do this BEFORE adding <br>.
    safe = truncated.replace("<", "&lt;").replace(">", "&gt;")
    return safe.replace("\n", "<br>")


def _build_card(
    *,
    call_id: str,
    session: XaiVoiceSession,
    summary: Any,
    recording_url: str | None,
) -> dict[str, Any]:
    after_hours = bool(getattr(summary, "after_hours", False))
    theme = _THEME_AFTER if after_hours else _THEME_OFFICE
    fullname = getattr(summary, "caller_fullname", None) or "(unknown caller)"
    call_date = _call_date_display(session.started_at) or ""

    if after_hours:
        header = {
            "title": f"🌙 OUTSIDE OFFICE HOURS — {fullname}",
            "subtitle": f"{call_date} · Outside 8:30 AM – 4:30 PM Mon–Fri CT",
            "imageUrl": _LOGO_URL,
            "imageType": "CIRCLE",
        }
    else:
        # Caller name moved from subtitle to title (2026-05-11) so it sits
        # symmetric with the after-hours header format.
        header = {
            "title": f"📞 Call Summary Report — {fullname}",
            "subtitle": call_date,
            "imageUrl": _LOGO_URL,
            "imageType": "CIRCLE",
        }

    duration_min = _duration_minutes(session)
    success_eval = getattr(summary, "success_evaluation", None)
    success_display = f"{success_eval}/10" if success_eval is not None else "—"

    xai_cost = float(getattr(summary, "xai_cost_usd", None) or 0.0)
    twilio_cost = float(getattr(summary, "twilio_cost_usd", None) or 0.0)
    total_cost = round(xai_cost + twilio_cost, 4)

    meeting_dt_display = _meeting_datetime_display(getattr(summary, "meeting_datetime", None))
    meeting_scheduled = "Yes" if getattr(summary, "meeting_scheduled", False) else "No"

    actions_html = _recommended_actions_html(getattr(summary, "recommended_actions", None) or [])
    if not actions_html:
        actions_html = "<i>No specific follow-up actions identified.</i>"

    narrative = getattr(summary, "analysis_summary", None) or "(no narrative summary extracted)"

    # Section emoji + accent color from theme table.
    e_caller,    c_caller    = theme["caller_info"]
    e_details,   c_details   = theme["call_details"]
    e_summary,   c_summary   = theme["call_summary"]
    e_trans,     c_trans     = theme["call_transcript"]
    e_steps,     c_steps     = theme["next_steps"]
    e_meeting,   c_meeting   = theme["meeting"]
    e_rec,       c_rec       = theme["call_recording"]
    _,           c_footer    = theme["footer"]

    # Section-collapse policy (the operator's revised ask 2026-05-12):
    #   - Caller Information + Call Details: ALWAYS expanded — these are the
    #     two sections the team scans first, so they show on card landing.
    #     `collapsible` omitted entirely → no chevron, full content visible.
    #   - All other sections: collapsible + `uncollapsibleWidgetsCount: 0`
    #     so they start collapsed (header bar only) and expand on click.
    # cardsV2 API note: a `collapsible: true` section ALWAYS starts collapsed
    # (the `uncollapsibleWidgetsCount` controls how many widgets stay visible
    # when collapsed). There's no `expanded: true` flag, so "start expanded
    # but still collapsible" isn't directly supported — we drop the chevron
    # on the two always-open sections instead.
    sections: list[dict[str, Any]] = [
        {
            "header": f"{e_caller} Caller Information",
            "widgets": [
                _two_col(
                    left=[
                        _decorated("Caller Name", fullname, accent=c_caller),
                        _decorated("Caller Email", getattr(summary, "caller_email", None), accent=c_caller),
                        _decorated("Number Used to Call", session.caller_number, accent=c_caller),
                    ],
                    right=[
                        _decorated("Call Back Number (AI)", getattr(summary, "callback_number", None), accent=c_caller),
                        _decorated("Recipient", getattr(summary, "forward_msg_to", None), accent=c_caller),
                        _decorated("SMS Sent", getattr(summary, "sms_meeting_link", None) or "No", accent=c_caller),
                    ],
                ),
            ],
        },
        {
            "header": f"{e_details} Call Details",
            "widgets": [
                _two_col(
                    left=[
                        _decorated("Service Type", getattr(summary, "service_type", None), accent=c_details),
                        _decorated("Caller Status", getattr(summary, "caller_status", None), accent=c_details),
                        _decorated("AI Call Rating", success_display, accent=c_details),
                    ],
                    right=[
                        _decorated("Call Duration", f"{duration_min} min", accent=c_details),
                        _decorated("Call Date & Time", call_date, accent=c_details),
                        _decorated("Total Cost", f"${total_cost:.4f}", accent=c_details),
                    ],
                ),
            ],
        },
        {
            "header": f"{e_summary} Call Summary",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{"textParagraph": {
                "text": f'<font color="{c_summary}">{narrative}</font>'
            }}],
        },
        {
            "header": f"{e_trans} Call Transcript",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{"textParagraph": {
                "text": f'<font color="{c_trans}">{_transcript_for_chat(session)}</font>'
            }}],
        },
        {
            "header": f"{e_steps} Next Steps & Actions",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{"textParagraph": {
                "text": f'<font color="{c_steps}">{actions_html}</font>'
            }}],
        },
        {
            "header": f"{e_meeting} Meeting",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [
                _two_col(
                    left=[
                        _decorated("Meeting Scheduled", meeting_scheduled, accent=c_meeting),
                        _decorated("Forward Msg To", getattr(summary, "forward_msg_to", None), accent=c_meeting),
                    ],
                    right=[
                        _decorated("Meeting Date/Time", meeting_dt_display, accent=c_meeting),
                        _decorated("Meeting Notes", getattr(summary, "meeting_notes", None), accent=c_meeting),
                    ],
                ),
            ],
        },
        {
            "header": f"{e_rec} Call Recording",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{
                "buttonList": {
                    "buttons": [
                        {
                            "text": "🎧 Listen to Call Recording",
                            "onClick": {"openLink": {"url": recording_url}} if recording_url else {"openLink": {"url": f"{_DASHBOARD_BASE}/{call_id}"}},
                            "disabled": not bool(recording_url),
                        },
                        {
                            "text": "📊 View in Dashboard",
                            "onClick": {"openLink": {"url": f"{_DASHBOARD_BASE}/{call_id}"}},
                        },
                    ]
                }
            }],
        },
        {
            "widgets": [{"textParagraph": {
                "text": f'<font color="{c_footer}"><i>Call ID: {call_id} · Generated by the bridge directly</i></font>'
            }}],
        },
    ]

    return {
        "cardsV2": [{
            "cardId": f"after-call-{call_id}",
            "card": {"header": header, "sections": sections},
        }]
    }


__all__ = ["post_chat_card"]
