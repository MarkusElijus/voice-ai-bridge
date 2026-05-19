"""End the call gracefully.

xAI tool name: end_call

Aria calls this when the conversation has reached a natural conclusion. The
bridge then asks Twilio to hang up the live call. The bot's closing line
("All set! ... Goodbye.") plays before the hangup because xAI streams the
audio before this tool is dispatched (function_call comes after speech).

Returns immediately; the actual Twilio update fires in the background so we
don't block xAI's event loop. Sets `session.end_requested = True` so the idle
watcher and bridge loops can wind down cleanly.
"""

from __future__ import annotations

import asyncio
from typing import Any

from twilio.rest import Client

from logging_config import log
from settings import settings


def _twilio_client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


async def handle(*, call_sid: str, call_id: str, **_: Any) -> dict[str, Any]:
    if not call_sid:
        return {"error": "missing_call_sid", "detail": "Bridge did not capture Twilio CallSid"}

    log.info("end_call.dispatched", call_id=call_id, call_sid=call_sid)

    # Schedule the hangup with a 5s delay. This gives the goodbye audio time
    # to actually play out on the caller's side (xAI bursts assistant audio
    # faster than wall-clock; a 3s "Goodbye, have a great day" only finishes
    # playing ~3-4s after we send the last delta), plus a small buffer so the
    # caller can squeeze in a final "thanks" without being cut off mid-word.
    # Mirrored in main.py's playground path (deferred end_requested).
    async def _delayed_hangup() -> None:
        try:
            await asyncio.sleep(5.0)
            client = _twilio_client()
            await asyncio.to_thread(client.calls(call_sid).update, status="completed")
            log.info("end_call.completed", call_id=call_id, call_sid=call_sid)
        except Exception as exc:  # noqa: BLE001
            log.exception("end_call.hangup_failed", call_id=call_id, call_sid=call_sid)

    asyncio.create_task(_delayed_hangup())
    return {"status": "ending"}
