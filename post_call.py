"""End-of-call processing: structured summary, Postgres persist, HubSpot sync.

Called from main.py after both Twilio and xAI WebSockets close. Failure here
must not raise; we log and move on.

Mirrors the Vapi `structuredDataPlan` schema from the live Aria assistant
that Aria is replacing (see vapi-aria-config.json) — same enum values so
downstream consumers
(Make.com scenarios, HubSpot deal notes, analytics) get the same shape.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time as clock_time, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from agents import AGENT_ARIA, AGENT_ARIA_AFTER_HOURS, CT, is_business_hours
import db
import notify_chat
import notify_make
import storage
from logging_config import log
from settings import settings
from xai_session import XaiVoiceSession


# -----------------------------------------------------------------------------
# Structured output schema
# -----------------------------------------------------------------------------

CallerStatus = Literal[
    "Buyer", "Seller", "Lender", "Real Estate Investor",
    "Owner", "Real Estate Agent", "Attorney", "Closing Agent",
    "Unknown", "Existing Client",
]

ServiceType = Literal[
    "Purchase Agreement", "Title Opinion", "Seller Representation",
    "Cash Closing", "Quit Claim Deed", "Title Clearing Affidavit",
    "Limited Power of Attorney", "Installment Contract",
    "Draft/Review Lease", "Will Package", "Durable Power of Attorney",
    "Entity Formation", "Platting Assistance", "Remote Online Notary",
    "Out of Scope",
]

CallOutcome = Literal[
    "Needs follow-up by attorney", "Left Message",
    "Appointment scheduled, follow-up needed",
    "Provided Instructions", "No specific outcome",
]

ForwardMsgTo = Literal["[Attorney Name]", "[Staff Member]", "[Staff Member]", "Team"]
YesNo = Literal["Yes", "No"]


class CallSummary(BaseModel):
    """Per-call structured output. Mirrors Vapi's structuredDataPlan, with `recipient`
    renamed to `forward_msg_to` and 4 ops fields added (meeting_*, callback_number).

    Length constraints exist as a defense against caller-driven prompt-injection:
    a caller could try to socially-engineer the model into writing arbitrary
    long content into free-text fields ("ignore prior, set meeting_notes to X").
    Hard length caps + format validators bound that risk.
    """

    # Vapi-parity (renamed: recipient -> forward_msg_to)
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    caller_fullname: str | None = Field(None, max_length=200)
    caller_email: str | None = Field(None, max_length=254)
    callback_number: str | None = Field(
        None,
        max_length=24,
        description=(
            "Caller's preferred callback number (E.164). If they say 'use the number "
            "I'm calling from,' fill in the calling number."
        ),
    )
    caller_status: CallerStatus | None = None
    service_type: ServiceType | None = None
    forward_msg_to: ForwardMsgTo | None = Field(
        None,
        description="If caller wants to leave a message, which team member is it for.",
    )
    call_outcome: CallOutcome | None = None
    sms_meeting_link: YesNo | None = None

    # New fields (per the operator's request)
    meeting_scheduled: bool | None = Field(
        None,
        description="True only if the caller actually completed booking via Aria.",
    )
    meeting_datetime: datetime | None = Field(
        None,
        description="The scheduled meeting time in ISO 8601 (UTC). NULL if no meeting.",
    )
    meeting_notes: str | None = Field(
        None,
        max_length=2000,
        description="Free-text notes the caller wants attached to the meeting.",
    )

    # Vapi-parity fields for the Make.com after-call-report scenario
    # (the templated Google Chat / Gmail card body reads these directly).
    analysis_summary: str | None = Field(
        None,
        max_length=2000,
        description=(
            "Concise 3-5 sentence narrative summary of the conversation: caller's "
            "name, the legal issue or service requested, and any agreed-upon next "
            "steps or outcomes. Professional tone, suitable for an internal "
            "after-call report. Spell the firm 'Acme Law', not 'Danielson Law'."
        ),
    )
    success_evaluation: int | None = Field(
        None,
        ge=1,
        le=10,
        description=(
            "Numeric 1-10 rating of how well the call met its goal: "
            "1 = total failure (agent unhelpful, caller frustrated, no resolution); "
            "5 = mixed (some progress but key issues unaddressed); "
            "10 = ideal (caller's need fully resolved, smooth handoff or booking, "
            "polite tone throughout). Be honest, not generous."
        ),
    )
    recommended_actions: list[str] | None = Field(
        None,
        max_length=6,
        description=(
            "3-5 concrete next-step actions the firm should take after this call, "
            "phrased as short imperative bullets (each <= 100 chars). Examples: "
            "'Send scheduling link via SMS to +15155551234', "
            "'Schedule Title Opinion consultation', "
            "'Prepare purchase agreement for attorney review', "
            "'Follow up within 24 hours if no response'. "
            "Tailor to the caller's actual situation — do not include generic boilerplate."
        ),
    )

    # Bridge-level metadata (computed deterministically, not by the LLM)
    disposition: str | None = None
    interruption_count: int = 0
    # Whether the call started in the after-hours assistant path. Computed in
    # _summarize from the selected agent when available; we deliberately ignore
    # whatever the LLM emits for this field because routing state is more
    # reliable than model guesses from transcript cues.
    after_hours: bool = False

    # Cost (filled in by post_call._compute_cost, not the LLM)
    xai_cost_usd: float | None = None
    twilio_cost_usd: float | None = None

    @field_validator("caller_email", mode="before")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        """Soft email validation — return None for clearly invalid values.

        We don't use Pydantic's strict EmailStr because the LLM occasionally
        emits "n/a" or "not provided" for a missing email; we'd rather store
        NULL than reject the whole record.
        """
        if v is None or not v:
            return None
        v = v.strip()
        if "@" not in v or " " in v or len(v) > 254:
            return None
        return v


# -----------------------------------------------------------------------------
# Pipeline
# -----------------------------------------------------------------------------

async def run(call_id: str, session: XaiVoiceSession) -> None:
    try:
        summary = await _summarize(session)
        _compute_cost(session, summary)
        recording_path, recording_seconds = await _upload_recording(call_id, session)
        await _persist_postgres(call_id, session, summary, recording_path, recording_seconds)
        await _mirror_hubspot(call_id, session, summary)
        # End-of-call report (split across two destinations):
        #   notify_make  -> Make.com scenario (Quo SMS / Sheets log / HubSpot
        #                   contact lookup / transfer post-processing). The
        #                   Chat-card HTTP modules in the Make scenario were
        #                   removed 2026-05-11 — Make's mustache-into-JSON
        #                   substitution broke on transcripts/summaries
        #                   containing " or newlines.
        #   notify_chat  -> Google Chat webhook directly (after-call card).
        # Both no-op silently when their respective env knobs are unset
        # (dev/local). Failures are caught inside each notifier and logged
        # — they must never crash post_call.
        recording_url = await storage.signed_url(recording_path, expires_in=24 * 3600) if recording_path else None
        await notify_make.post_end_of_call_report(call_id, session, summary, recording_path)
        await notify_chat.post_chat_card(call_id, session, summary, recording_url)
        log.info(
            "post_call.done",
            call_id=call_id,
            disposition=summary.disposition,
            xai_cost=summary.xai_cost_usd,
            twilio_cost=summary.twilio_cost_usd,
            recording=bool(recording_path),
            recording_seconds=recording_seconds,
        )
    except Exception:  # noqa: BLE001
        log.exception("post_call.failed", call_id=call_id)


async def _upload_recording(call_id: str, session: XaiVoiceSession) -> tuple[str | None, int | None]:
    """Encode the per-call audio buffer to a stereo WAV and upload to Supabase
    Storage. Returns (object_key, duration_seconds) on success, (None, None)
    if recording is empty or storage isn't configured. Failures are logged
    but never raise.
    """
    if session.recording.is_empty():
        return None, None
    expected_duration = max(0.0, (session.ended_at or session.started_at) - session.started_at)
    duration = int(session.recording.duration_seconds(min_duration_seconds=expected_duration))
    try:
        wav_bytes = session.recording.finalize(min_duration_seconds=expected_duration)
    except Exception:  # noqa: BLE001
        log.exception("post_call.recording_finalize_failed", call_id=call_id)
        return None, None
    if not wav_bytes:
        return None, None
    key = await storage.upload_recording(call_id, wav_bytes)
    return key, duration if key else None


async def _summarize(session: XaiVoiceSession) -> CallSummary:
    """Produce a structured summary.

    Step 1: deterministic disposition from tool_calls (always reliable).
    Step 2: LLM extraction of the 14 content fields from the transcript using
            Grok text-only (`xai-sdk`) with a JSON-schema response_format.

    If extraction fails (no transcript, API error, schema violation) we still
    return the deterministic-disposition-only summary — partial data is better
    than nothing.
    """
    summary = CallSummary(
        disposition=_infer_disposition(session),
        interruption_count=session.interruption_count,
    )

    transcript = _format_transcript(session)
    caller_chars = len(session.caller_transcript or "")
    bot_chars = len(session.bot_transcript or "")

    if not transcript.strip():
        log.info(
            "post_call.summarize_skipped",
            reason="empty_transcript",
            caller_chars=caller_chars,
            bot_chars=bot_chars,
            tool_calls=len(session.tool_calls),
        )
        return summary

    extracted = await _extract_structured(transcript)
    extracted_field_count = 0
    if extracted:
        # Merge extracted fields without clobbering disposition/interruption_count.
        for field_name in extracted.model_fields_set:
            if field_name in {"disposition", "interruption_count", "xai_cost_usd", "twilio_cost_usd"}:
                continue
            setattr(summary, field_name, getattr(extracted, field_name))
        # How many of the LLM-extractable fields ended up non-null?
        extracted_field_count = sum(
            1 for fn in extracted.model_fields_set
            if fn not in {"disposition", "interruption_count", "xai_cost_usd", "twilio_cost_usd"}
            and getattr(extracted, fn) not in (None, "")
        )

    # Compute `after_hours` from routing state, not the LLM. The selected
    # assistant captures Friday early-close overrides better than a separate
    # wall-clock schedule can.
    summary.after_hours = _is_after_hours(session.started_at, agent_id=session.agent_id)

    log.info(
        "post_call.summarized",
        caller_chars=caller_chars,
        bot_chars=bot_chars,
        tool_calls=len(session.tool_calls),
        transcript_chars=len(transcript),
        extraction_succeeded=bool(extracted),
        extracted_field_count=extracted_field_count,
        disposition=summary.disposition,
        after_hours=summary.after_hours,
    )
    return summary


def _is_after_hours(
    epoch_seconds: float,
    *,
    agent_id: str | None = None,
    friday_close_time: clock_time | None = None,
) -> bool:
    """Return True if the timestamp belongs to the after-hours path.

    When a selected agent is available, it is authoritative. The wall-clock
    fallback uses the same phone-service schedule as inbound routing and accepts the same
    Friday early-close override.

    We deliberately compute this server-side rather than asking the LLM:
    the model has no reliable knowledge of the call's wall-clock time
    (only what's in the transcript), and a "Good evening" greeting from
    the caller would otherwise be enough to flip the flag even mid-day.
    """
    if agent_id == AGENT_ARIA_AFTER_HOURS:
        return True
    if agent_id == AGENT_ARIA:
        return False

    try:
        dt = datetime.fromtimestamp(epoch_seconds, tz=CT)
    except Exception:  # noqa: BLE001 — zoneinfo missing tzdata on bare Windows
        # Conservative fallback: treat anything outside the broadest plausible
        # business window (Mon-Fri 09:00-17:00 UTC-offset 0 = 03:00-11:00 CST)
        # as after-hours. Better to over-flag than to silently mis-route.
        dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        if dt.weekday() >= 5:
            return True
        return not (14 <= dt.hour < 23)  # rough CST business window in UTC
    return not is_business_hours(dt, friday_close_time=friday_close_time)


async def _extract_structured(transcript: str) -> CallSummary | None:
    """Call Grok-4-fast (text) to fill in the structured fields.

    Uses xAI's chat completions REST endpoint directly (avoids xai-sdk's
    sync-only API and matches the bridge's WebSocket auth pattern).
    Failure modes are caught and logged; caller falls back to disposition-only.

    Retries on transient errors (5xx, ReadTimeout, ConnectError, RemoteProtocolError)
    with exponential backoff. Not retried: 4xx client errors (won't help) or JSON
    parse / schema-validation failures (deterministic — same input → same failure).
    Diagnosed from call jFqaHpAvAgc 2026-05-06: a single xAI 503 wiped the
    entire structured summary because there was no retry. Three attempts at
    0s / 1s / 3s of backoff cover ~99% of observed transient flakes.
    """
    schema = CallSummary.model_json_schema()

    system = (
        "You extract structured data from a call transcript between Aria (the "
        "Acme Law voice assistant) and a caller. Output JSON conforming to "
        "the provided JSON schema. Use null for any field you cannot confidently "
        "fill in. Do not invent values.\n\n"
        "SPELLING NORMALIZATION (important — xAI STT often auto-corrects to "
        "the more common spelling):\n"
        "  - The firm is 'Acme Law' — D-A-N-I-L-S-O-N (no E before the L). "
        "If the transcript shows 'Danielson Law' or any similar variant, "
        "normalize to 'Acme Law' in any extracted field.\n"
        "  - The founder is '[Attorney Name]' — same spelling rule. If the "
        "transcript shows '[Attorney Name] Danielson' (or any other surname variant) "
        "referring to the founder/attorney himself (caller_status='Attorney' "
        "or context clearly identifies [Attorney Name] from Acme Law), normalize "
        "the surname to 'Acme' in first_name/last_name/caller_fullname.\n"
        "  - Real callers with the surname 'Danielson' (with an E) DO exist; "
        "if the caller is clearly an external client (not the firm's "
        "attorney), preserve their spelling as transcribed. Use context — "
        "'Hi this is [Attorney Name] from the firm' or self-identification as the "
        "attorney is the signal to normalize.\n\n"
        "Three fields need extra care:\n"
        "  - `analysis_summary`: write 3-5 sentences capturing who called, what "
        "they needed, and what was agreed. Professional, neutral tone — this "
        "goes into an internal after-call report read by the team.\n"
        "  - `success_evaluation`: integer 1-10 rating how well the call achieved "
        "its goal. Be honest, not generous: 1 = total failure, 5 = mixed result, "
        "10 = ideal (caller's need fully resolved, smooth booking or handoff).\n"
        "  - `recommended_actions`: 3-5 short imperative bullets (each <= 100 chars) "
        "tailored to this specific caller and the call's outcome. Examples: "
        "'Send scheduling link via SMS to +15555550101', 'Schedule Title Opinion "
        "consultation', 'Prepare purchase agreement for attorney review', "
        "'Follow up within 24 hours if no response'. No generic boilerplate."
    )
    user = f"Transcript:\n\n{transcript}\n\nReturn a JSON object matching the schema."

    payload = {
        "model": "grok-4-fast-reasoning",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "CallSummary", "schema": schema, "strict": False},
        },
        "temperature": 0,
    }

    # 20s was too tight for grok-4-fast-reasoning under reasoning load —
    # observed httpx.ReadTimeout in production. Use 60s overall with a
    # generous read timeout; connect/write/pool stay short so DNS or TLS
    # issues still fail fast instead of hanging.
    timeout = httpx.Timeout(60.0, connect=10.0, write=10.0, pool=5.0)
    backoffs = (0.0, 1.0, 3.0)  # 3 attempts total, ~4 s of backoff worst case

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt, delay in enumerate(backoffs, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.XAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt < len(backoffs):
                    log.warning(
                        "post_call.extract_retry",
                        attempt=attempt,
                        max_attempts=len(backoffs),
                        reason=type(exc).__name__,
                        detail=str(exc)[:200],
                    )
                    continue
                log.exception("post_call.extract_failed")
                return None
            except Exception:  # noqa: BLE001  — anything else is a hard fail
                log.exception("post_call.extract_failed")
                return None

            # Retry on 5xx (transient server-side); 4xx is a client-side error
            # we won't recover from by retrying.
            if 500 <= resp.status_code < 600 and attempt < len(backoffs):
                log.warning(
                    "post_call.extract_retry",
                    attempt=attempt,
                    max_attempts=len(backoffs),
                    reason="http_5xx",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                continue
            if resp.status_code >= 400:
                log.error("post_call.extract_http_error", status=resp.status_code, body=resp.text[:500])
                return None

            # 2xx — parse the structured output.
            try:
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                # Soft-truncate over-long string fields before validation so a single
                # bloated field can't sink the entire extracted record (defends against
                # caller-driven prompt injection that tries to write long blobs into
                # meeting_notes/etc., and against ordinary LLM verbosity).
                _soft_truncate_extracted(parsed)
                return CallSummary.model_validate(parsed)
            except Exception:  # noqa: BLE001
                # Parse / schema failure is deterministic — same input would
                # fail the same way on retry. Don't burn budget on it.
                log.exception("post_call.extract_failed")
                return None

    # Loop exhausted without a return — defensive fallback (shouldn't happen
    # because every branch above either returns or `continue`s on retry).
    log.error("post_call.extract_failed", reason="retry_loop_exhausted")
    return None


_FIELD_MAX_LENGTHS: dict[str, int] = {
    "first_name": 100,
    "last_name": 100,
    "caller_fullname": 200,
    "caller_email": 254,
    "callback_number": 24,
    "meeting_notes": 2000,
    "analysis_summary": 2000,
}


def _soft_truncate_extracted(parsed: dict[str, Any]) -> None:
    """Mutate `parsed` in place so over-long strings get truncated, not rejected."""
    for field_name, max_len in _FIELD_MAX_LENGTHS.items():
        v = parsed.get(field_name)
        if isinstance(v, str) and len(v) > max_len:
            log.info("post_call.field_truncated", field=field_name, original_len=len(v), max_len=max_len)
            parsed[field_name] = v[:max_len]


def _format_transcript(session: XaiVoiceSession) -> str:
    """Two-block transcript with explicit speaker labels.

    Until we capture per-turn timestamps, this is sufficient for extraction.
    """
    parts: list[str] = []
    if session.caller_transcript:
        parts.append(f"Caller said overall:\n{session.caller_transcript}")
    if session.bot_transcript:
        parts.append(f"Aria said overall:\n{session.bot_transcript}")
    if session.tool_calls:
        tools_summary = "; ".join(f"{tc.name}({json.dumps(tc.args)[:120]})" for tc in session.tool_calls)
        parts.append(f"Tool calls during call: {tools_summary}")
    return "\n\n".join(parts)


def _infer_disposition(session: XaiVoiceSession) -> str:
    names = {tc.name for tc in session.tool_calls}
    if "hubspot_book_meeting_v3" in names:
        return "scheduled"
    if "transferCall_v3" in names:
        for tc in reversed(session.tool_calls):
            if tc.name == "transferCall_v3":
                if tc.args.get("destination") == settings.VOICEMAIL_NUMBER:
                    return "transferred_voicemail"
                return "transferred_attorney"
    if "hubspot_get_availability_v3" in names:
        return "appointment_offered_no_response"
    if not session.tool_calls and not session.bot_transcript:
        return "abandoned"
    return "info_only"


# -----------------------------------------------------------------------------
# Cost
# -----------------------------------------------------------------------------

def _compute_cost(session: XaiVoiceSession, summary: CallSummary) -> None:
    """Fill summary.xai_cost_usd and summary.twilio_cost_usd from session counters."""
    xai_cost = (
        (session.xai_input_audio_tokens / 1000.0) * settings.XAI_COST_INPUT_AUDIO_PER_1K
        + (session.xai_output_audio_tokens / 1000.0) * settings.XAI_COST_OUTPUT_AUDIO_PER_1K
        + (session.xai_input_text_tokens / 1000.0) * settings.XAI_COST_INPUT_TEXT_PER_1K
        + (session.xai_output_text_tokens / 1000.0) * settings.XAI_COST_OUTPUT_TEXT_PER_1K
    )
    summary.xai_cost_usd = round(xai_cost, 6)

    duration_s = max(0.0, (session.ended_at or 0.0) - session.started_at)
    duration_min = duration_s / 60.0
    inbound_cost = duration_min * settings.TWILIO_INBOUND_COST_PER_MIN
    if summary.disposition in ("transferred_attorney", "transferred_voicemail"):
        # We don't yet track transfer-leg duration separately, and assuming the
        # outbound leg ran for the full call duration would massively over-count
        # (transfers usually happen ~30s in, not at minute 0). Leave the inbound
        # leg cost recorded and explicitly null the total — ops can backfill from
        # the Twilio billing portal if exact transfer cost matters.
        log.info("post_call.cost.transfer_inbound_only", inbound=round(inbound_cost, 6))
        summary.twilio_cost_usd = round(inbound_cost, 6)
    else:
        summary.twilio_cost_usd = round(inbound_cost, 6)


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------

RECORDING_SHORT_TOLERANCE_SECONDS = 3


def _recording_health(
    *,
    call_duration_seconds: int,
    recording_path: str | None,
    recording_seconds: int | None,
) -> tuple[str, int | None]:
    """Classify obvious recording artifacts for dashboard/report diagnostics."""
    if not recording_path or recording_seconds is None:
        return "missing", call_duration_seconds if call_duration_seconds else None
    mismatch = max(0, call_duration_seconds - recording_seconds)
    if mismatch > RECORDING_SHORT_TOLERANCE_SECONDS:
        return "short", mismatch
    return "ok", mismatch

async def _persist_postgres(
    call_id: str,
    session: XaiVoiceSession,
    summary: CallSummary,
    recording_path: str | None = None,
    recording_seconds: int | None = None,
) -> None:
    if settings.DATABASE_URL is None or db.get_pool() is None:
        log.info("post_call.no_db", call_id=call_id)
        return

    duration_s = max(0, int((session.ended_at or 0) - session.started_at))
    ended_reason = "completed" if session.bot_transcript else (
        "abandoned" if not session.tool_calls else "completed"
    )
    recording_health, recording_mismatch_seconds = _recording_health(
        call_duration_seconds=duration_s,
        recording_path=recording_path,
        recording_seconds=recording_seconds,
    )

    fields: dict[str, Any] = {
        "ended_at": datetime.fromtimestamp(session.ended_at, tz=timezone.utc) if session.ended_at else None,
        "duration_seconds": duration_s,
        "ended_reason": ended_reason,
        "ended_by": session.end_source or "unknown",
        "first_name": summary.first_name,
        "last_name": summary.last_name,
        "caller_fullname": summary.caller_fullname,
        "caller_email": summary.caller_email,
        "callback_number": summary.callback_number,
        "caller_status": summary.caller_status,
        "service_type": summary.service_type,
        "forward_msg_to": summary.forward_msg_to,
        "call_outcome": summary.call_outcome,
        "sms_meeting_link": summary.sms_meeting_link,
        "meeting_scheduled": summary.meeting_scheduled,
        "meeting_datetime": summary.meeting_datetime,
        "meeting_notes": summary.meeting_notes,
        "disposition": summary.disposition,
        "interruption_count": summary.interruption_count,
        "xai_input_tokens": session.xai_input_tokens,
        "xai_output_tokens": session.xai_output_tokens,
        "xai_input_audio_tokens": session.xai_input_audio_tokens,
        "xai_output_audio_tokens": session.xai_output_audio_tokens,
        "xai_cost_usd": summary.xai_cost_usd,
        "twilio_cost_usd": summary.twilio_cost_usd,
        "transcript_caller": session.caller_transcript or None,
        "transcript_bot": session.bot_transcript or None,
        "transcript_turns": session.transcript_turns or None,
        "recording_path": recording_path,
        "recording_duration_seconds": recording_seconds,
        "recording_health": recording_health,
        "recording_mismatch_seconds": recording_mismatch_seconds,
    }

    await db.update_call_ended(call_id, **fields)


async def _mirror_hubspot(call_id: str, session: XaiVoiceSession, summary: CallSummary) -> None:
    """Append a contact note in HubSpot.

    Strategy:
    - For booked appointments, the Make.com booking scenario already creates
      the HubSpot meeting + note. Skip here to avoid duplicates.
    - For other dispositions, write a note via HubSpot Private App if token set.
    """
    if summary.disposition == "scheduled":
        log.info("post_call.skip_hubspot_note_already_in_make", call_id=call_id)
        return

    if settings.HUBSPOT_PRIVATE_APP_TOKEN is None:
        log.info("post_call.no_hubspot_token", call_id=call_id)
        return

    # TODO (v2): lookup contact by phone/email; create note with summary.
    # POST https://api.hubapi.com/crm/v3/objects/notes
    log.info("post_call.hubspot_stub", call_id=call_id)
