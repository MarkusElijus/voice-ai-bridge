"""Fetch HubSpot meeting availability.

xAI tool name: hubspot_get_availability_v3

Returns both Option A (soonest slot) and Option B (first slot on a different
day from A) in a single response so the agent can offer two options without
a second tool call.

Note: the Vapi function declares `dayOffset` as a parameter but never uses it.
The model is expected to call this tool ONCE and read both options from the
response. The Vapi prompt's "Step 1 / Step 2 / call again with dayOffset=1"
instruction is a parity quirk that survives because the model adapts.

Response shape:
  {
    "error": false,
    "mode":  "initial",
    "options": [
      {"optionId": "A", "startTimeMillisUtc": <int>, "spokenTime": "..."},
      {"optionId": "B", "startTimeMillisUtc": <int>, "spokenTime": "..."}  // optional
    ]
  }

If neither monthOffset 0 nor 1 has slots:
  {"error": true, "message": "No availability found in the current or next month."}
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from logging_config import log
from settings import settings


TIMEZONE = "America/Chicago"
TZ_CENTRAL = ZoneInfo(TIMEZONE)
HUBSPOT_BASE = "https://api.hubapi.com/scheduler/v3/meetings/meeting-links/book/availability-page"


async def handle(*, call_sid: str, call_id: str, **_: Any) -> dict[str, Any]:
    """Return Option A (+ optional Option B) — both in one response, like Vapi does."""
    # Parity-test injection: force a 503-equivalent error response so we can
    # exercise scenario 6 of docs/parity-test.md without actually breaking
    # HubSpot's API or relying on flaky network conditions. Default off; flip
    # in .env to HUBSPOT_FORCE_503=true and restart uvicorn for the test.
    if settings.HUBSPOT_FORCE_503:
        log.warning("availability.force_503", call_id=call_id)
        return {
            "error": True,
            "message": "scheduling_unavailable: simulated HubSpot 503 (HUBSPOT_FORCE_503)",
        }

    # Path 1: Make.com fallback (only if explicitly configured and no Private App token)
    if not settings.HUBSPOT_PRIVATE_APP_TOKEN and settings.HUBSPOT_AVAILABILITY_MAKE_WEBHOOK:
        return await _via_make(call_id=call_id)

    # Path 2: Direct HubSpot Private App (the live Vapi code tool's exact behavior)
    if not settings.HUBSPOT_PRIVATE_APP_TOKEN:
        log.warning("availability.no_path", call_id=call_id)
        return {"error": True, "message": "scheduling_unavailable: no HubSpot token configured"}

    return await _via_hubspot(call_id=call_id)


# -----------------------------------------------------------------------------
# Direct HubSpot path — verbatim port of the Vapi JS code
# -----------------------------------------------------------------------------

async def _via_hubspot(*, call_id: str) -> dict[str, Any]:
    # Try this month first; if no slots, try next month.
    result = await _fetch_availability(month_offset=0, call_id=call_id)
    if result.get("error"):
        return result

    availabilities = result.get("availabilities", [])

    if not availabilities:
        result = await _fetch_availability(month_offset=1, call_id=call_id)
        if result.get("error"):
            return result
        availabilities = result.get("availabilities", [])

    if not availabilities:
        return {"error": True, "message": "No availability found in the current or next month."}

    # Sort ascending by startMillisUtc
    availabilities.sort(key=lambda s: s["startMillisUtc"])

    option_a = availabilities[0]
    option_a_date_key = _date_key(option_a["startMillisUtc"])

    # Option B = first slot whose local date is strictly after Option A's date
    option_b: dict[str, Any] | None = None
    for slot in availabilities:
        if _date_key(slot["startMillisUtc"]) > option_a_date_key:
            option_b = slot
            break

    options = [{
        "optionId": "A",
        "startTimeMillisUtc": option_a["startMillisUtc"],
        "spokenTime": _format_spoken(option_a["startMillisUtc"]),
    }]
    if option_b is not None:
        options.append({
            "optionId": "B",
            "startTimeMillisUtc": option_b["startMillisUtc"],
            "spokenTime": _format_spoken(option_b["startMillisUtc"]),
        })

    return {"error": False, "mode": "initial", "options": options}


async def _fetch_availability(*, month_offset: int, call_id: str) -> dict[str, Any]:
    """GET HubSpot meeting-link availability page. Returns dict with `availabilities` or `error`."""
    url = (
        f"{HUBSPOT_BASE}/{settings.HUBSPOT_MEETING_LINK_PATH}"
        f"?timezone={TIMEZONE}&monthOffset={month_offset}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.HUBSPOT_PRIVATE_APP_TOKEN}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code >= 400:
        log.error("hubspot.availability_failed", call_id=call_id, status=resp.status_code, month_offset=month_offset)
        return {"error": True, "message": f"HubSpot API error ({resp.status_code})"}

    data = resp.json()
    by_duration = (
        data.get("linkAvailability", {})
        .get("linkAvailabilityByDuration", {})
    )
    if not by_duration:
        return {"error": True, "message": "No duration collections found."}

    first_key = next(iter(by_duration))
    availabilities = by_duration.get(first_key, {}).get("availabilities", [])
    return {"error": False, "availabilities": availabilities}


# -----------------------------------------------------------------------------
# Make.com fallback (kept for ops flexibility)
# -----------------------------------------------------------------------------

async def _via_make(*, call_id: str) -> dict[str, Any]:
    payload = {"callId": call_id}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(settings.HUBSPOT_AVAILABILITY_MAKE_WEBHOOK, json=payload)
    if resp.status_code >= 400:
        log.error("availability.make_failed", call_id=call_id, status=resp.status_code, body=resp.text[:500])
        return {"error": True, "message": f"availability webhook error ({resp.status_code})"}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"error": True, "message": "make_returned_non_json", "raw": resp.text[:500]}


# -----------------------------------------------------------------------------
# Time formatting (matches the Vapi JS Intl.DateTimeFormat output)
# -----------------------------------------------------------------------------

def _format_spoken(ms: int) -> str:
    """Mirror Vapi JS:
        new Date(ms).toLocaleString('en-US', {
          timeZone: 'America/Chicago',
          weekday: 'long', month: 'long', day: 'numeric',
          hour: 'numeric', minute: '2-digit', hour12: true
        }) + ' Central Time'

    Example output: "Friday, April 26 at 10:00 AM Central Time"
    """
    local = dt.datetime.fromtimestamp(ms / 1000, tz=TZ_CENTRAL)
    # Hour/day without leading zero — Windows uses %#, Unix uses %-
    import os
    hour = local.strftime("%#I" if os.name == "nt" else "%-I")
    day = local.strftime("%#d" if os.name == "nt" else "%-d")
    weekday = local.strftime("%A")
    month = local.strftime("%B")
    minute = local.strftime("%M")
    am_pm = local.strftime("%p")
    # JS Intl with these options renders: "Friday, April 26 at 10:00 AM"
    return f"{weekday}, {month} {day} at {hour}:{minute} {am_pm} Central Time"


def _date_key(ms: int) -> str:
    """Local date in 'en-CA' / 'YYYY-MM-DD' for cross-day comparison."""
    return dt.datetime.fromtimestamp(ms / 1000, tz=TZ_CENTRAL).strftime("%Y-%m-%d")
