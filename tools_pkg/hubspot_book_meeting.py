"""Book a 15-minute HubSpot meeting via a Make.com webhook.

xAI tool name: hubspot_book_meeting_v3

The Make.com scenario referenced by `HUBSPOT_MAKE_WEBHOOK` handles HubSpot
OAuth + the create-meeting API. Configure the scenario URL via `.env`.

The scenario was built against Vapi's webhook payload shape, where the tool
arguments live at `message.toolCalls[].function.arguments.*`. Every filter,
JSON template, and response in the scenario references that path. To stay
compatible without re-wiring Make.com, we wrap our flat args in the Vapi
envelope before POSTing.

A flat `{firstName, lastName, ...}` payload was the prior behavior; it caused
the scenario's `startTimeMillisUtc`-equality filter to silently match nothing
(field empty), so the iterator-aggregator dropped to length 0, the
"length >= 1" gate at module 12 blocked, no WebhookRespond fired, and Make
fell back to its default "Accepted" reply. Our caller saw "Accepted" and
reported "appointment booked" to the caller — but no contact was ever
created in HubSpot. Diagnosed 2026-05-05 from call QHWticUonTM.

Args (all required, additionalProperties: false):
  firstName, lastName, email, phone, description (str)
  startTimeMillisUtc (int)
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx

from logging_config import log
from settings import settings


async def handle(
    *,
    call_sid: str,
    call_id: str,
    firstName: str,
    lastName: str,
    email: str,
    phone: str,
    description: str,
    startTimeMillisUtc: int,
    **_: Any,
) -> dict[str, Any]:
    # Synthesize a Vapi-style tool_call_id so the scenario's WebhookRespond
    # (which echoes `{{1.message.toolCalls[].id}}`) has something to render.
    # We don't correlate by this id on our side — it's purely for Make.com's
    # template not to be empty.
    tool_call_id = "call_" + secrets.token_urlsafe(16)

    payload = {
        # Vapi envelope — every Make.com filter and JSON template in the
        # scenario references `{{1.message.toolCalls[].function.arguments.X}}`.
        "message": {
            "toolCalls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "hubspot_book_meeting_v3",
                        "arguments": {
                            "firstName": firstName,
                            "lastName": lastName,
                            "email": email,
                            "phone": phone,
                            "description": description,
                            "startTimeMillisUtc": int(startTimeMillisUtc),
                        },
                    },
                }
            ],
        },
        # Our own correlation (Make.com ignores extras at the top level).
        "callId": call_id,
        "callSid": call_sid,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.HUBSPOT_MAKE_WEBHOOK,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    if resp.status_code >= 400:
        log.error("hubspot_book.http_error", call_id=call_id, status=resp.status_code, body=resp.text[:500])
        return {"error": "booking_failed", "status": resp.status_code, "detail": resp.text[:500]}

    # Make.com's scenario responds with JSON when WebhookRespond fires — both
    # the success and failure routes return a structured body of the shape
    #   {"results": [{"toolCallId": "...", "result": "Meeting booked successfully"}]}
    # If neither WebhookRespond fires (filter blocks, unhandled error), Make
    # falls back to its default "Accepted" plain-text response. We surface
    # that as a warning so the model doesn't tell the caller their meeting
    # is booked when it isn't.
    raw_text = resp.text
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None

    if isinstance(body, dict):
        results = body.get("results")
        if isinstance(results, list) and results:
            result_text = (results[0] or {}).get("result", "")
            if isinstance(result_text, str) and "successfully" in result_text.lower():
                log.info("hubspot_book.ok", call_id=call_id, response=body)
                return {
                    "status": "booked",
                    "startTimeMillisUtc": int(startTimeMillisUtc),
                    "make_response": body,
                }
            # Make.com explicitly told us the booking failed.
            log.error("hubspot_book.make_failed", call_id=call_id, response=body)
            return {
                "error": "booking_failed",
                "detail": result_text or "make.com returned non-success result",
                "make_response": body,
            }

    # Default-Accepted path (or any other unexpected shape). Treat as
    # inconclusive — booking did NOT confirm. The model's prompt should
    # interpret this error and tell the caller we couldn't complete the
    # booking right now, rather than falsely confirming.
    log.error(
        "hubspot_book.no_confirmation",
        call_id=call_id,
        body_text=raw_text[:200],
        hint="Make.com scenario filter or upstream module silently dropped the request; check Make.com run logs.",
    )
    return {
        "error": "booking_unconfirmed",
        "detail": (
            "Make.com responded but did not confirm the booking. The scenario's "
            "filter or HubSpot call may have failed; check Make.com run logs."
        ),
        "make_response_raw": raw_text[:500],
    }
