"""Transfer the live call (voicemail or warm transfer to attorney).

xAI tool name: transferCall_v3

Args:
  destination (str, enum): +15555550101 (voicemail) or +15555550100 (attorney)
  reason (str, enum): "voicemail" | "warm_transfer_attorney"
  summary (str, optional): one-paragraph summary spoken to attorney before bridging

Warm transfer (`warm_transfer_attorney`):
  - Persist `summary` into calls.transfer_summary so the cross-machine LB
    can serve the whisper TwiML regardless of which Fly machine picks it up.
  - Generate TwiML <Dial><Number url="https://.../whisper/{call_id}"> so
    Twilio fetches a TwiML from the bridge when the attorney answers, plays
    the spoken summary to the attorney ONLY, then bridges in the caller.

Voicemail (`voicemail`):
  - Bare <Dial><Number>{voicemail}</Number></Dial> — voicemail box answers
    automatically and starts its own greeting + recording flow, no whisper.

This replaces the prior Vapi-era workaround of sending an OpenPhone SMS
heads-up before bridging — that workaround existed because Vapi could not
do warm transfers to non-SIP/PSTN destinations. Twilio handles PSTN warm
transfers natively via the <Number url=...> pattern.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import quoteattr

from twilio.rest import Client

import db
from logging_config import log
from settings import settings


_VOICEMAIL = settings.VOICEMAIL_NUMBER
_ATTORNEY = settings.ATTORNEY_TRANSFER_NUMBER


def _twilio_client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def _whisper_url(call_id: str) -> str:
    hostname = settings.HOSTNAME.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{hostname}/whisper/{call_id}"


async def handle(
    *,
    call_sid: str,
    call_id: str,
    destination: str,
    reason: str,
    summary: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    if destination not in {_VOICEMAIL, _ATTORNEY}:
        return {"error": "invalid_destination", "detail": f"Must be {_VOICEMAIL} or {_ATTORNEY}"}
    if reason not in {"voicemail", "warm_transfer_attorney"}:
        return {"error": "invalid_reason"}
    if reason == "warm_transfer_attorney" and destination != _ATTORNEY:
        return {"error": "destination_reason_mismatch"}
    if not call_sid:
        return {"error": "missing_call_sid", "detail": "Bridge did not capture Twilio CallSid"}

    # Warm transfer: stash the summary for /whisper to read back and embed
    # url= on <Number>. Voicemail: skip — the VM box plays its own greeting.
    if reason == "warm_transfer_attorney":
        clean_summary = (summary or "").strip()
        if clean_summary:
            try:
                await db.patch_call(call_id, transfer_summary=clean_summary)
            except Exception:  # noqa: BLE001
                # Non-fatal: if the persist fails, the whisper falls back to
                # a generic "incoming call" message rather than aborting the
                # transfer entirely.
                log.exception("transfer.summary_persist_failed", call_id=call_id)
        number_tag = (
            f'<Number url={quoteattr(_whisper_url(call_id))}>{destination}</Number>'
        )
    else:
        number_tag = f'<Number>{destination}</Number>'

    twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>'
        f'<Dial answerOnBridge="true" timeout="20">{number_tag}</Dial>'
        f'</Response>'
    )

    try:
        # Twilio SDK is sync; wrap in to_thread for non-blocking call.
        import asyncio
        client = _twilio_client()
        await asyncio.to_thread(client.calls(call_sid).update, twiml=twiml)
    except Exception as exc:  # noqa: BLE001
        log.exception("transfer.failed", call_id=call_id, dest=destination)
        return {"error": "transfer_failed", "detail": str(exc)}

    log.info(
        "transfer.dispatched",
        call_id=call_id,
        dest=destination,
        reason=reason,
        whisper=(reason == "warm_transfer_attorney"),
    )
    return {
        "status": "transferring",
        "destination": destination,
        "reason": reason,
        "summary_logged": bool(summary),
    }
