"""Send SMS via OpenPhone.

xAI tool name: send_sms_summary_openphone

Args:
  to (str, E.164, required)
  from (str, E.164, optional - defaults to OPENPHONE_DEFAULT_FROM)
  content (str, required)
"""

from __future__ import annotations

from typing import Any

import httpx

from logging_config import log
from settings import settings


OPENPHONE_API = "https://api.openphone.com/v1/messages"


async def handle(*, call_sid: str, call_id: str, to: str, content: str, **extra: Any) -> dict[str, Any]:
    sender = extra.get("from") or settings.OPENPHONE_DEFAULT_FROM

    if not _is_e164(to):
        return {"error": "invalid_to", "detail": "Recipient must be E.164 (e.g. +15551234567)"}
    if not _is_e164(sender):
        return {"error": "invalid_from", "detail": "Sender must be E.164"}
    if not content or len(content) > 1600:
        return {"error": "invalid_content", "detail": "content empty or > 1600 chars"}

    payload = {"to": [to], "from": sender, "content": content}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            OPENPHONE_API,
            headers={"Authorization": settings.OPENPHONE_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )

    if resp.status_code >= 400:
        log.error("openphone.failed", call_id=call_id, status=resp.status_code, body=resp.text[:500])
        return {"error": "openphone_failed", "status": resp.status_code, "detail": resp.text[:500]}

    body = resp.json()
    log.info("openphone.sent", call_id=call_id, message_id=body.get("data", {}).get("id"))
    return {"status": "sent", "message_id": body.get("data", {}).get("id"), "to": to}


def _is_e164(number: str | None) -> bool:
    if not number:
        return False
    return number.startswith("+") and number[1:].isdigit() and 8 <= len(number) <= 16
