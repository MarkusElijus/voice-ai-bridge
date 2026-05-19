"""Admin dashboard FastAPI router.

Phase 2 routes (Call Logs + Call Detail with inline edit):
    GET  /admin/                           — redirect to /admin/calls
    GET  /admin/calls                      — paginated, filtered list
    GET  /admin/calls/{id}                 — call detail
    GET  /admin/calls/{id}/edit/{field}    — render inline-edit form for one field
    POST /admin/calls/{id}/edit/{field}    — save edit, return re-rendered field
    GET  /admin/calls/{id}/view/{field}    — cancel edit, return view-mode field

Phase 3 routes (added below; gated on whether DB is configured):
    GET  /admin/assistants                 — assistant registry
    GET  /admin/assistants/{agent_id}      — prompt editor + history
    GET  /admin/settings                   — read-only env display
    GET  /admin/costs                      — costs charts (Chart.js)
"""

from __future__ import annotations

import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db
import storage
import tools as tools_module
from agents import (
    AGENT_ARIA,
    CT,
    chat_tool_defs_for_agent,
    get_agent_config,
    list_agent_configs,
    normalize_agent_id,
    tool_defs_for_agent,
    validate_tool_call,
)
from admin.auth import require_admin
from logging_config import log
from settings import settings


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _require_same_origin(request: Request) -> None:
    """CSRF defense for state-mutating POST routes.

    HTTP Basic auth alone leaves a window where a malicious site could trigger
    a cross-origin POST while the admin's browser still has cached credentials.
    We block when an Origin header is present and doesn't match the public
    HOSTNAME. Absent Origin (curl/programmatic clients) is allowed — those
    aren't the CSRF threat model.
    """
    origin = request.headers.get("origin")
    if not origin:
        return  # No browser Origin → allowed (programmatic clients)
    expected = settings.HOSTNAME.rstrip("/").lower()
    if origin.rstrip("/").lower() == expected:
        return
    # Also accept localhost development origins to avoid breaking local dev.
    if origin.startswith(("http://localhost:", "http://127.0.0.1:")):
        return
    log.warning("admin.cross_origin_post_blocked", origin=origin, expected=expected)
    raise HTTPException(status_code=403, detail="Cross-origin POST blocked")


# ---------------------------------------------------------------------------
# Field metadata used by both the read view and the inline-edit form
# ---------------------------------------------------------------------------

CALLER_STATUS_CHOICES = [
    "Buyer", "Seller", "Lender", "Real Estate Investor",
    "Owner", "Real Estate Agent", "Attorney", "Closing Agent",
    "Unknown", "Existing Client",
]
SERVICE_TYPE_CHOICES = [
    "Purchase Agreement", "Title Opinion", "Seller Representation",
    "Cash Closing", "Quit Claim Deed", "Title Clearing Affidavit",
    "Limited Power of Attorney", "Installment Contract",
    "Draft/Review Lease", "Will Package", "Durable Power of Attorney",
    "Entity Formation", "Platting Assistance", "Remote Online Notary",
    "Out of Scope",
]
CALL_OUTCOME_CHOICES = [
    "Needs follow-up by attorney", "Left Message",
    "Appointment scheduled, follow-up needed",
    "Provided Instructions", "No specific outcome",
]
FORWARD_MSG_TO_CHOICES = ["[Attorney Name]", "[Staff Member]", "[Staff Member]", "Team"]
YESNO_CHOICES = ["Yes", "No"]


@dataclass
class FieldSpec:
    name: str
    label: str
    kind: str  # "text" | "enum" | "bool" | "datetime" | "textarea"
    choices: list[str] | None = None


_FIELD_ORDER: list[FieldSpec] = [
    FieldSpec("first_name",       "First name",        "text"),
    FieldSpec("last_name",        "Last name",         "text"),
    FieldSpec("caller_fullname",  "Full name",         "text"),
    FieldSpec("caller_email",     "Email",             "text"),
    FieldSpec("callback_number",  "Callback number",   "text"),
    FieldSpec("caller_status",    "Caller status",     "enum", CALLER_STATUS_CHOICES),
    FieldSpec("service_type",     "Service type",      "enum", SERVICE_TYPE_CHOICES),
    FieldSpec("forward_msg_to",   "Forward msg to",    "enum", FORWARD_MSG_TO_CHOICES),
    FieldSpec("call_outcome",     "Call outcome",      "enum", CALL_OUTCOME_CHOICES),
    FieldSpec("sms_meeting_link", "SMS meeting link",  "enum", YESNO_CHOICES),
    FieldSpec("meeting_scheduled","Meeting scheduled", "bool"),
    FieldSpec("meeting_datetime", "Meeting date/time", "datetime"),
    FieldSpec("meeting_notes",    "Meeting notes",     "textarea"),
]
_FIELD_BY_NAME = {f.name: f for f in _FIELD_ORDER}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_value(field: FieldSpec, value: Any) -> str:
    if value is None or value == "":
        return ""
    if field.kind == "bool":
        return "Yes" if value else "No"
    if field.kind == "datetime" and isinstance(value, datetime):
        return value.strftime("%a %b %d, %Y at %I:%M %p")
    return str(value)


def _coerce(field: FieldSpec, raw: str) -> Any:
    """Convert a form-posted string into the right typed value for the DB."""
    if raw == "":
        return None
    if field.kind == "bool":
        return raw == "true"
    if field.kind == "datetime":
        # `<input type="datetime-local">` produces YYYY-MM-DDTHH:MM (no tz)
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    if field.kind == "enum":
        if field.choices and raw not in field.choices:
            raise HTTPException(status_code=400, detail=f"Invalid value for {field.name}")
        return raw
    return raw


def _build_fields(call: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in _FIELD_ORDER:
        v = call.get(f.name)
        out.append({
            "name": f.name,
            "label": f.label,
            "kind": f.kind,
            "choices": f.choices,
            "value": v,
            "display": _format_value(f, v),
        })
    return out


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    """Common Jinja context."""
    return {
        "request": request,
        "env": settings.ENV,
        "build_id": None,
        **extra,
    }


def _json_safe_row(row: Any) -> dict[str, Any]:
    """Convert an asyncpg Record (or dict) into a JSON-serializable dict.

    asyncpg returns Postgres `timestamp`/`date` as Python datetime/date and
    `numeric` as Decimal — neither serializes via the stdlib json encoder
    (which is what Jinja's `tojson` filter and Chart.js consume). Convert
    timestamps to ISO 8601 strings (JS's `new Date(...)` parses them) and
    Decimal to float for numeric chart values.
    """
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _parse_since(label: str | None) -> tuple[datetime | None, str | None]:
    if not label or label == "all":
        return None, None
    now = datetime.now(timezone.utc)
    if label == "24h":
        return now - timedelta(hours=24), "24h"
    if label == "7d":
        return now - timedelta(days=7), "7 days"
    if label == "30d":
        return now - timedelta(days=30), "30 days"
    return None, None


async def _active_prompt_for_agent(agent_id: str) -> str:
    """Load the active prompt for an assistant, with its registry fallback."""
    config = get_agent_config(agent_id)
    content = await db.get_active_prompt(config.agent_id)
    if content:
        return content
    if config.prompt_fallback_path.exists():
        return config.prompt_fallback_path.read_text(encoding="utf-8")
    return ""


def _agent_query(agent_id: str, **extra: str) -> str:
    qs = {"agent_id": normalize_agent_id(agent_id), **extra}
    return urlencode(qs)


def _next_friday(today: date) -> date:
    days_until_friday = (4 - today.weekday()) % 7
    return today + timedelta(days=days_until_friday)


def _parse_friday_close_time(raw: str) -> clock_time:
    try:
        parsed = clock_time.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid close time") from exc
    parsed = parsed.replace(second=0, microsecond=0)
    if not (clock_time(8, 0) <= parsed <= clock_time(14, 0)):
        raise HTTPException(status_code=400, detail="Friday close time must be between 8:00 AM and 2:00 PM")
    return parsed


def _time_label(value: clock_time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------
# Routes — root + call list
# ---------------------------------------------------------------------------

@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/calls", status_code=302)


@router.get("/calls", response_class=HTMLResponse)
async def calls_list(
    request: Request,
    q: str | None = None,
    since: str = "30d",
    disposition: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    return await _render_calls_list(
        request, q=q, since=since, disposition=disposition, page=page,
        test_filter="exclude", view_name="calls", view_title="Call Logs",
        list_path="/admin/calls",
    )


@router.get("/calls/test", response_class=HTMLResponse)
async def calls_list_test(
    request: Request,
    q: str | None = None,
    since: str = "30d",
    disposition: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    """Dedicated view for in-browser voice playground test calls."""
    return await _render_calls_list(
        request, q=q, since=since, disposition=disposition, page=page,
        test_filter="only", view_name="test_calls", view_title="Test Calls",
        list_path="/admin/calls/test",
    )


async def _render_calls_list(
    request: Request,
    *,
    q: str | None,
    since: str,
    disposition: str | None,
    page: int,
    test_filter: str,
    view_name: str,
    view_title: str,
    list_path: str,
) -> HTMLResponse:
    page = max(1, page)
    page_size = 25
    since_dt, since_label = _parse_since(since)
    disposition_filter = [disposition] if disposition else None

    calls = await db.list_calls(
        limit=page_size,
        offset=(page - 1) * page_size,
        since=since_dt,
        disposition=disposition_filter,
        search=q,
        test_filter=test_filter,
    )
    total = await db.count_calls(
        since=since_dt,
        disposition=disposition_filter,
        search=q,
        test_filter=test_filter,
    )
    pages = max(1, math.ceil(total / page_size))

    qs = {"q": q or "", "since": since, "disposition": disposition or ""}
    qs_prev = urlencode({**qs, "page": max(1, page - 1)})
    qs_next = urlencode({**qs, "page": page + 1})

    template = "_calls_table.html" if request.headers.get("HX-Request") else "calls_list.html"
    return _TEMPLATES.TemplateResponse(request=request, name=template, context=_ctx(
        request,
        calls=calls,
        total=total,
        page=page,
        pages=pages,
        since=since,
        since_label=since_label,
        disposition=disposition,
        search=q,
        qs_prev=qs_prev,
        qs_next=qs_next,
        has_db=db.get_pool() is not None,
        view_name=view_name,
        view_title=view_title,
        list_path=list_path,
    ))


@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(request: Request, call_id: str) -> HTMLResponse:
    call = await db.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    tool_calls = await db.get_tool_calls(call_id)
    # Generate a fresh 1-hour signed URL for the recording so the audio
    # element on the detail page can stream it directly. We don't persist
    # the URL — Supabase Storage signs on each render, which keeps URLs
    # from being shareable/cacheable beyond the admin session.
    recording_url = None
    if call.get("recording_path"):
        recording_url = await storage.signed_url(call["recording_path"], expires_in=3600)
    return _TEMPLATES.TemplateResponse(request=request, name="call_detail.html", context=_ctx(
        request,
        call=call,
        fields=_build_fields(call),
        tool_calls=tool_calls,
        recording_url=recording_url,
    ))


# ---------------------------------------------------------------------------
# Inline field edit (HTMX)
# ---------------------------------------------------------------------------

@router.get("/calls/{call_id}/edit/{field_name}", response_class=HTMLResponse)
async def field_edit_form(request: Request, call_id: str, field_name: str) -> HTMLResponse:
    field = _FIELD_BY_NAME.get(field_name)
    if not field:
        raise HTTPException(status_code=404, detail="Unknown field")
    call = await db.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    field_data = {
        "name": field.name,
        "label": field.label,
        "kind": field.kind,
        "choices": field.choices,
        "value": call.get(field.name),
    }
    return _TEMPLATES.TemplateResponse(request=request, name="_field_edit.html", context=_ctx(request, call_id=call_id, field=field_data),
    )


@router.post(
    "/calls/{call_id}/edit/{field_name}",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_same_origin)],
)
async def field_edit_save(
    request: Request,
    call_id: str,
    field_name: str,
    value: str = Form(""),
) -> HTMLResponse:
    field = _FIELD_BY_NAME.get(field_name)
    if not field:
        raise HTTPException(status_code=404, detail="Unknown field")
    coerced = _coerce(field, value)
    ok = await db.patch_call(call_id, **{field.name: coerced})
    if not ok:
        # Either the row doesn't exist, or the DB rejected the value (constraint).
        # Re-render the edit form with the same value; logs already captured the why.
        log.warning("admin.field_edit_failed", call_id=call_id, field=field_name)
        field_data = {
            "name": field.name, "label": field.label, "kind": field.kind,
            "choices": field.choices, "value": coerced,
        }
        return _TEMPLATES.TemplateResponse(request=request, name="_field_edit.html", context=_ctx(request, call_id=call_id, field=field_data),
        )
    field_data = {
        "name": field.name,
        "label": field.label,
        "kind": field.kind,
        "value": coerced,
        "display": _format_value(field, coerced),
    }
    return _TEMPLATES.TemplateResponse(request=request, name="_field_view.html", context=_ctx(request, call_id=call_id, field=field_data),
    )


@router.get("/calls/{call_id}/view/{field_name}", response_class=HTMLResponse)
async def field_view(request: Request, call_id: str, field_name: str) -> HTMLResponse:
    """Cancel-edit endpoint — returns the read-only span back."""
    field = _FIELD_BY_NAME.get(field_name)
    if not field:
        raise HTTPException(status_code=404, detail="Unknown field")
    call = await db.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    v = call.get(field.name)
    field_data = {
        "name": field.name,
        "label": field.label,
        "kind": field.kind,
        "value": v,
        "display": _format_value(field, v),
    }
    return _TEMPLATES.TemplateResponse(request=request, name="_field_view.html", context=_ctx(request, call_id=call_id, field=field_data),
    )


# ---------------------------------------------------------------------------
# Phase 3 — Prompt editor
# ---------------------------------------------------------------------------

@router.get("/assistants", response_class=HTMLResponse)
async def assistants_view(request: Request) -> HTMLResponse:
    agents = list_agent_configs()
    prompt_lengths: dict[str, int] = {}
    prompt_counts: dict[str, int] = {}
    today_ct = datetime.now(CT).date()
    friday_default_date = _next_friday(today_ct)
    active_friday_override = await db.get_friday_early_close_override(today_ct)
    friday_overrides = await db.list_friday_early_close_overrides(today_ct)
    for agent in agents:
        prompt = await _active_prompt_for_agent(agent.agent_id)
        prompt_lengths[agent.agent_id] = len(prompt)
        prompt_counts[agent.agent_id] = len(await db.list_prompts(agent.agent_id))
    return _TEMPLATES.TemplateResponse(request=request, name="assistants.html", context=_ctx(
        request,
        active="assistants",
        agents=agents,
        prompt_lengths=prompt_lengths,
        prompt_counts=prompt_counts,
        tool_names={agent.agent_id: [t["name"] for t in tool_defs_for_agent(agent.agent_id)] for agent in agents},
        has_db=db.get_pool() is not None,
        today_ct=today_ct,
        friday_default_date=friday_default_date,
        active_friday_override=active_friday_override,
        friday_overrides=friday_overrides,
        time_label=_time_label,
    ))


@router.post("/assistants/routing/friday-early-close", dependencies=[Depends(_require_same_origin)])
async def friday_early_close_save(
    override_date: date = Form(...),
    close_time: str = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    if override_date.weekday() != 4:
        raise HTTPException(status_code=400, detail="Friday early close overrides must use a Friday date")
    parsed_close_time = _parse_friday_close_time(close_time)
    await db.upsert_friday_early_close_override(
        override_date=override_date,
        close_time=parsed_close_time,
        note=note.strip() or None,
    )
    return RedirectResponse(url="/admin/assistants", status_code=303)


@router.post("/assistants/routing/friday-early-close/clear", dependencies=[Depends(_require_same_origin)])
async def friday_early_close_clear(override_date: date = Form(...)) -> RedirectResponse:
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    await db.delete_friday_early_close_override(override_date)
    return RedirectResponse(url="/admin/assistants", status_code=303)


@router.get("/assistants/{agent_id}", response_class=HTMLResponse)
async def assistant_detail(request: Request, agent_id: str) -> HTMLResponse:
    agent_id = normalize_agent_id(agent_id)
    agent = get_agent_config(agent_id)
    prompt_content = await _active_prompt_for_agent(agent_id)
    history = await db.list_prompts(agent_id)
    tool_names = [t["name"] for t in tool_defs_for_agent(agent_id)]
    stub_tools = sorted(t for t in tool_names if t not in tools_module.PLAYGROUND_LIVE_TOOLS)
    return _TEMPLATES.TemplateResponse(request=request, name="assistant_detail.html", context=_ctx(
        request,
        active="assistants",
        agent=agent,
        prompt_content=prompt_content,
        history=history,
        tool_names=tool_names,
        stub_tools=stub_tools,
        has_db=db.get_pool() is not None,
    ))


@router.post(
    "/assistants/{agent_id}/prompt",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_same_origin)],
)
async def assistant_prompt_save(
    agent_id: str,
    content: str = Form(...),
    notes: str = Form(""),
    publish: str = Form(""),
) -> HTMLResponse:
    agent_id = normalize_agent_id(agent_id)
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    pid = await db.insert_prompt_draft(content=content, notes=notes or None, agent_id=agent_id)
    if publish == "true":
        await db.publish_prompt(pid)
    return RedirectResponse(url=f"/admin/assistants/{agent_id}", status_code=303)


@router.post("/assistants/{agent_id}/prompt/publish/{prompt_id}", dependencies=[Depends(_require_same_origin)])
async def assistant_prompt_publish(agent_id: str, prompt_id: int) -> RedirectResponse:
    agent_id = normalize_agent_id(agent_id)
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    history = await db.list_prompts(agent_id)
    if not any(v["id"] == prompt_id for v in history):
        raise HTTPException(status_code=404, detail="Prompt not found for this assistant")
    ok = await db.publish_prompt(prompt_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return RedirectResponse(url=f"/admin/assistants/{agent_id}", status_code=303)


@router.get("/prompt", response_class=RedirectResponse)
async def prompt_view() -> RedirectResponse:
    return RedirectResponse(url=f"/admin/assistants/{AGENT_ARIA}", status_code=302)


@router.post(
    "/prompt",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_same_origin)],
)
async def prompt_save(
    request: Request,
    content: str = Form(...),
    notes: str = Form(""),
    publish: str = Form(""),
) -> HTMLResponse:
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    pid = await db.insert_prompt_draft(content=content, notes=notes or None)
    if publish == "true":
        await db.publish_prompt(pid)
    return RedirectResponse(url=f"/admin/assistants/{AGENT_ARIA}", status_code=303)


@router.post("/prompt/publish/{prompt_id}", dependencies=[Depends(_require_same_origin)])
async def prompt_publish(prompt_id: int) -> RedirectResponse:
    if db.get_pool() is None:
        raise HTTPException(status_code=503, detail="DB not configured")
    ok = await db.publish_prompt(prompt_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return RedirectResponse(url=f"/admin/assistants/{AGENT_ARIA}", status_code=303)


# ---------------------------------------------------------------------------
# Phase 3 — Settings (read-only)
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request) -> HTMLResponse:
    rows = _settings_rows()
    return _TEMPLATES.TemplateResponse(request=request, name="settings.html", context=_ctx(request, rows=rows))


_SECRET_KEY_PAT = re.compile(
    r"(KEY|TOKEN|HASH|PASSWORD|SECRET|DATABASE_URL|WEBHOOK|_URL)$",
    re.IGNORECASE,
)


def _settings_rows() -> list[dict[str, Any]]:
    """Build a (key, value, secret?) view of the settings, with values masked."""
    out: list[dict[str, Any]] = []
    for name in sorted(settings.model_fields.keys()):
        v = getattr(settings, name)
        is_secret = bool(_SECRET_KEY_PAT.search(name))
        if v is None:
            display = "(unset)"
        elif is_secret and isinstance(v, str) and v:
            # Show first 4 + last 4 chars; mask the middle.
            display = f"{v[:4]}…{v[-4:]}" if len(v) > 12 else "********"
        else:
            display = str(v)
        out.append({"name": name, "value": display, "secret": is_secret})
    return out


# ---------------------------------------------------------------------------
# Phase 3 — Costs
# ---------------------------------------------------------------------------

@router.get("/costs", response_class=HTMLResponse)
async def costs_view(request: Request) -> HTMLResponse:
    pool = db.get_pool()
    if pool is None:
        return _TEMPLATES.TemplateResponse(
            request=request, name="costs.html",
            context=_ctx(request, has_db=False, daily=[], monthly=[], totals={}),
        )

    async with pool.acquire() as conn:
        # is_test_call rows (in-browser voice playground) are excluded from
        # every aggregation so playground tokens don't inflate the dashboard.
        daily = await conn.fetch(
            """
            select date_trunc('day', started_at) as day,
                   coalesce(sum(xai_cost_usd), 0)    as xai,
                   coalesce(sum(twilio_cost_usd), 0) as twilio,
                   count(*)                          as call_count
            from calls
            where started_at >= now() - interval '30 days'
              and is_test_call = false
            group by 1
            order by 1
            """
        )
        monthly = await conn.fetch(
            """
            select date_trunc('month', started_at) as month,
                   coalesce(sum(total_cost_usd), 0) as total,
                   count(*)                         as call_count
            from calls
            where started_at >= now() - interval '6 months'
              and is_test_call = false
            group by 1
            order by 1
            """
        )
        totals = await conn.fetchrow(
            """
            select
              coalesce(sum(case when started_at >= now() - interval '1 day'  then total_cost_usd end), 0) as today,
              coalesce(sum(case when started_at >= now() - interval '7 days' then total_cost_usd end), 0) as week,
              coalesce(sum(case when started_at >= now() - interval '30 days' then total_cost_usd end), 0) as month,
              count(*)                                  as all_calls,
              coalesce(avg(total_cost_usd), 0)::float8  as avg_per_call
            from calls
            where is_test_call = false
            """
        )

    return _TEMPLATES.TemplateResponse(request=request, name="costs.html", context=_ctx(
        request,
        has_db=True,
        daily=[_json_safe_row(r) for r in daily],
        monthly=[_json_safe_row(r) for r in monthly],
        totals=_json_safe_row(totals) if totals else {},
    ))


# ---------------------------------------------------------------------------
# Playground — type-test Aria without placing a Twilio call.
# Uses xAI's OpenAI-compatible chat completions API + the active prompt + the
# real tools.dispatch_tool_playground path. Side-effect tools are stubbed.
# ---------------------------------------------------------------------------

_PLAYGROUND_MODEL = "grok-4-fast-reasoning"
_PLAYGROUND_MAX_TOOL_TURNS = 8


class _PlaygroundChatRequest(BaseModel):
    # Cap the history so a runaway client (or stale tab) can't blow up token spend
    # or stall the 60s xAI request inside the tool-call loop.
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    user_text: str = Field(min_length=1, max_length=8000)
    agent_id: str = Field(default=AGENT_ARIA, max_length=80)


@router.get("/playground", response_class=HTMLResponse)
async def playground_view(request: Request, tab: str = "chat", agent_id: str = AGENT_ARIA) -> HTMLResponse:
    agent_id = normalize_agent_id(agent_id)
    selected_agent = get_agent_config(agent_id)
    prompt_content = await _active_prompt_for_agent(agent_id)
    tool_names = [t["name"] for t in tool_defs_for_agent(agent_id)]
    stub_tools = sorted(t for t in tool_names if t not in tools_module.PLAYGROUND_LIVE_TOOLS)
    # Only "voice" is treated as a non-default tab; anything else collapses to "chat".
    pg_tab = "voice" if tab == "voice" else "chat"
    agents = list_agent_configs()
    return _TEMPLATES.TemplateResponse(
        request=request, name="playground.html",
        context=_ctx(
            request,
            agents=agents,
            selected_agent=selected_agent,
            agent_qs=_agent_query(agent_id),
            chat_qs=_agent_query(agent_id),
            voice_qs=_agent_query(agent_id, tab="voice"),
            active_prompt=prompt_content,
            tool_names=tool_names,
            stub_tools=stub_tools,
            pg_tab=pg_tab,
        ),
    )


@router.post(
    "/playground/chat",
    dependencies=[Depends(_require_same_origin)],
)
async def playground_chat(payload: _PlaygroundChatRequest) -> dict[str, Any]:
    agent_id = normalize_agent_id(payload.agent_id)
    agent = get_agent_config(agent_id)
    system_prompt = await _active_prompt_for_agent(agent_id)
    convo: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    convo.extend(payload.messages)
    convo.append({"role": "user", "content": payload.user_text})

    call_id = "pg_" + secrets.token_urlsafe(6)
    log.info(
        "playground.turn_start",
        call_id=call_id,
        agent_id=agent_id,
        agent_label=agent.label,
        history_len=len(payload.messages),
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        for turn in range(_PLAYGROUND_MAX_TOOL_TURNS):
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _PLAYGROUND_MODEL,
                    "messages": convo,
                    "tools": chat_tool_defs_for_agent(agent_id),
                    "tool_choice": "auto",
                },
            )
            if resp.status_code != 200:
                # Log the upstream body for debugging; return a generic 502 so the
                # raw xAI error (which may include quota/tier info) doesn't leak.
                log.warning("playground.xai_error", call_id=call_id, status=resp.status_code, body=resp.text[:500])
                raise HTTPException(status_code=502, detail="xAI API error — see server logs")
            choice = resp.json()["choices"][0]["message"]
            convo.append(choice)
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                break
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args_json = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_json or "{}")
                except json.JSONDecodeError:
                    args = {}
                policy_error = validate_tool_call(agent_id, name, args)
                output = policy_error or await tools_module.dispatch_tool_playground(
                    name, args_json, call_id=call_id,
                )
                convo.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(output),
                })
        else:
            # Loop hit the cap with the model still asking for tool calls. Append
            # a synthetic assistant message so the UI doesn't render a dangling
            # tool-call row with no resolution.
            log.warning("playground.tool_loop_capped", call_id=call_id, turns=_PLAYGROUND_MAX_TOOL_TURNS)
            convo.append({
                "role": "assistant",
                "content": f"[Playground: tool-call limit reached after {_PLAYGROUND_MAX_TOOL_TURNS} turns. Reset the chat or simplify the request.]",
            })

    # Strip the system message before returning — the client doesn't need it back.
    return {"messages": convo[1:]}


@router.get("/playground/voice/recording/{call_id}")
async def playground_voice_recording(call_id: str) -> JSONResponse:
    """Resolve a fresh signed URL for a just-finished playground recording.

    The browser polls this after WS hangup until `ready=true`, then mounts
    `<audio src=url>` for inline listen-back. We restrict to `is_test_call=true`
    rows so this endpoint can't be used as a backdoor to production recording
    URLs (those go through `/admin/calls/{id}` which renders the URL inline
    rather than handing it out as JSON).
    """
    call = await db.get_call(call_id)
    if call is None or not call.get("is_test_call"):
        raise HTTPException(status_code=404, detail="Test call not found")
    path = call.get("recording_path")
    if not path:
        # Upload happens inside post_call.run after WS close — the browser
        # polls for a few seconds while that's in flight.
        return JSONResponse({"ready": False})
    url = await storage.signed_url(path, expires_in=3600)
    if not url:
        # Storage configured but signing failed — surface as not-ready so the
        # poller can retry; the call_detail page is the durable fallback.
        return JSONResponse({"ready": False})
    return JSONResponse({"ready": True, "url": url})
