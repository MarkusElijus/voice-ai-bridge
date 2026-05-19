"""Supabase Postgres access layer.

Centralizes all DB operations for the bridge and (Phase 2) admin dashboard.
Uses asyncpg directly against the Supabase transaction pooler URL
(`postgres://...pooler.supabase.com:6543/postgres`).

The pool is initialized in `main.py`'s lifespan and accessed via `get_pool()`.
If `DATABASE_URL` is unset, the pool stays None and helpers no-op (logging
once) so the bridge can still run without a DB during local dev.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import asyncpg

from logging_config import log
from settings import settings


class _PromptCacheEntry(NamedTuple):
    content: str
    fetched_at: float  # epoch seconds


class FridayEarlyCloseOverride(NamedTuple):
    override_date: date
    close_time: clock_time
    note: str | None
    updated_at: datetime | None


_pool: asyncpg.Pool | None = None
# Cache active prompt per-agent so adding the outbound agent later doesn't share the assistant's cache entry.
_prompt_cache: dict[str, _PromptCacheEntry] = {}
_PROMPT_CACHE_TTL_SECONDS = 60.0
_PROMPT_FILE_FALLBACK = Path(__file__).parent / "prompts" / "aria_instructions.md"


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------

async def init_pool() -> None:
    """Create the asyncpg pool. Call from FastAPI lifespan startup."""
    global _pool
    if _pool is not None:
        return
    if not settings.DATABASE_URL:
        log.warning("db.no_database_url_configured")
        return
    _pool = await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.SUPABASE_DB_POOL_MIN,
        max_size=settings.SUPABASE_DB_POOL_MAX,
        # Statement cache is incompatible with the Supabase transaction pooler.
        statement_cache_size=0,
    )
    log.info(
        "db.pool_ready",
        min_size=settings.SUPABASE_DB_POOL_MIN,
        max_size=settings.SUPABASE_DB_POOL_MAX,
    )


async def close_pool() -> None:
    """Close the pool. Call from FastAPI lifespan shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool_closed")


def get_pool() -> asyncpg.Pool | None:
    """Return the active pool or None if DB is not configured."""
    return _pool


# ---------------------------------------------------------------------------
# Routing overrides
# ---------------------------------------------------------------------------

async def get_friday_early_close_override(override_date: date) -> FridayEarlyCloseOverride | None:
    """Return the early-close override for one Friday date, if configured."""
    if _pool is None:
        return None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select override_date, close_time, note, updated_at
                from friday_early_close_overrides
                where override_date = $1
                """,
                override_date,
            )
        if row is None:
            return None
        return FridayEarlyCloseOverride(
            override_date=row["override_date"],
            close_time=row["close_time"],
            note=row["note"],
            updated_at=row["updated_at"],
        )
    except Exception:  # noqa: BLE001
        log.exception("db.get_friday_early_close_override_failed", override_date=str(override_date))
        return None


async def list_friday_early_close_overrides(since: date, *, limit: int = 8) -> list[FridayEarlyCloseOverride]:
    """Return recent/upcoming Friday early-close overrides for the admin UI."""
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select override_date, close_time, note, updated_at
                from friday_early_close_overrides
                where override_date >= $1
                order by override_date asc
                limit $2
                """,
                since,
                limit,
            )
        return [
            FridayEarlyCloseOverride(
                override_date=row["override_date"],
                close_time=row["close_time"],
                note=row["note"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
    except Exception:  # noqa: BLE001
        log.exception("db.list_friday_early_close_overrides_failed", since=str(since))
        return []


async def upsert_friday_early_close_override(
    *,
    override_date: date,
    close_time: clock_time,
    note: str | None = None,
) -> None:
    """Create/update a one-day Friday early-close override."""
    if _pool is None:
        raise RuntimeError("DB not configured")
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            insert into friday_early_close_overrides (override_date, close_time, note)
            values ($1, $2, $3)
            on conflict (override_date) do update
            set close_time = excluded.close_time,
                note = excluded.note
            """,
            override_date,
            close_time,
            note,
        )


async def delete_friday_early_close_override(override_date: date) -> None:
    """Clear one Friday early-close override."""
    if _pool is None:
        raise RuntimeError("DB not configured")
    async with _pool.acquire() as conn:
        await conn.execute(
            "delete from friday_early_close_overrides where override_date = $1",
            override_date,
        )


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

async def insert_call_started(
    *,
    call_id: str,
    call_sid: str | None,
    caller_number: str | None,
    agent_id: str = "aria",
    is_test_call: bool = False,
) -> None:
    """Insert a row at call setup. Disposition starts as 'in_progress'.

    `is_test_call=True` is set by the in-browser voice playground so the row
    can be excluded from /admin/calls and /admin/costs by default. Production
    Twilio inbound calls leave it false.
    """
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                insert into calls (id, call_sid, caller_number, started_at, disposition, agent_id, is_test_call)
                values ($1, $2, $3, now(), 'in_progress', $4, $5)
                on conflict (id) do nothing
                """,
                call_id,
                call_sid,
                caller_number,
                agent_id,
                is_test_call,
            )
    except Exception:  # noqa: BLE001
        log.exception("db.insert_call_started_failed", call_id=call_id)


async def update_call_ended(call_id: str, **fields: Any) -> None:
    """Update a call row with end-of-call data.

    Accepts any subset of the calls-table column names. Unknown keys are ignored
    (defensive — schema-drift won't crash post_call).
    """
    if _pool is None:
        return
    if not fields:
        return

    allowed = _CALL_UPDATE_COLUMNS
    safe_fields = {k: v for k, v in fields.items() if k in allowed}
    if not safe_fields:
        log.warning("db.update_call_ended.no_valid_fields", call_id=call_id, requested=list(fields.keys()))
        return

    # JSONB columns need a `::jsonb` cast and the value JSON-encoded as text;
    # other columns get a normal $N placeholder.
    set_parts: list[str] = []
    values: list[Any] = []
    for i, (col, v) in enumerate(safe_fields.items()):
        if col in _CALL_JSONB_COLUMNS and v is not None:
            set_parts.append(f"{col} = ${i+2}::jsonb")
            values.append(json.dumps(v))
        else:
            set_parts.append(f"{col} = ${i+2}")
            values.append(v)
    set_clause = ", ".join(set_parts)

    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                f"update calls set {set_clause} where id = $1",
                call_id,
                *values,
            )
    except Exception:  # noqa: BLE001
        log.exception("db.update_call_ended_failed", call_id=call_id, fields=list(safe_fields.keys()))


_CALL_UPDATE_COLUMNS = frozenset({
    "ended_at", "duration_seconds", "ended_reason",
    "ended_by",
    "first_name", "last_name", "caller_fullname", "caller_email",
    "callback_number", "caller_status", "service_type",
    "forward_msg_to", "call_outcome", "sms_meeting_link",
    "meeting_scheduled", "meeting_datetime", "meeting_notes",
    "disposition", "interruption_count",
    "xai_input_tokens", "xai_output_tokens",
    "xai_input_audio_tokens", "xai_output_audio_tokens",
    "xai_cost_usd", "twilio_cost_usd",
    "transcript_caller", "transcript_bot",
    "transcript_turns",  # jsonb [{"role","text","ts_ms"}]
    "recording_path", "recording_duration_seconds",
    "recording_health", "recording_mismatch_seconds",
    # transferCall_v3 stashes its `summary` arg here right before issuing the
    # Twilio call.update, so the /whisper/{call_id} endpoint can read it back
    # and play it as a TwiML <Say> to the attorney. Cross-machine durability
    # (two Fly machines, LB-routed Twilio callback) is why this lives in the
    # DB rather than process-local memory.
    "transfer_summary",
    # NOTE: `is_test_call` is intentionally NOT in this allow-list. It's set
    # exactly once at insert_call_started; including it here would let a stray
    # post_call write flip a real call into the test bucket (or vice versa).
})


_CALL_JSONB_COLUMNS = frozenset({"transcript_turns"})


def _build_calls_filter(
    since: datetime | None,
    disposition: list[str] | None,
    search: str | None,
    agent_id: str | None = None,
    test_filter: str = "exclude",
) -> tuple[str, list[Any]]:
    """Build a WHERE clause + values list shared by list_calls and count_calls.

    `test_filter` controls in-browser voice-playground rows:
      - "exclude" (default): production only; hides is_test_call=true
      - "only":              dedicated test-calls page; shows only is_test_call=true
      - "include":           combined view (no filter)
    """
    where_parts: list[str] = []
    values: list[Any] = []
    if since is not None:
        values.append(since)
        where_parts.append(f"started_at >= ${len(values)}")
    if disposition:
        values.append(disposition)
        where_parts.append(f"disposition = any(${len(values)})")
    if search:
        values.append(f"%{search}%")
        idx = len(values)
        where_parts.append(
            f"(caller_fullname ilike ${idx} or caller_number ilike ${idx} or caller_email ilike ${idx})"
        )
    if agent_id is not None:
        values.append(agent_id)
        where_parts.append(f"agent_id = ${len(values)}")
    if test_filter == "exclude":
        where_parts.append("is_test_call = false")
    elif test_filter == "only":
        where_parts.append("is_test_call = true")
    # "include" → no filter
    where_sql = ("where " + " and ".join(where_parts)) if where_parts else ""
    return where_sql, values


async def list_calls(
    *,
    limit: int = 25,
    offset: int = 0,
    since: datetime | None = None,
    disposition: list[str] | None = None,
    search: str | None = None,
    agent_id: str | None = None,
    test_filter: str = "exclude",
) -> list[dict[str, Any]]:
    """Paginated call list for the dashboard. Returns most-recent first.

    `test_filter`: "exclude" (default) | "only" | "include" — see
    `_build_calls_filter` for semantics.
    """
    if _pool is None:
        return []

    where_sql, values = _build_calls_filter(since, disposition, search, agent_id, test_filter)
    values.append(limit)
    limit_idx = len(values)
    values.append(offset)
    offset_idx = len(values)

    sql = f"""
        select id, started_at, ended_at, duration_seconds,
               caller_fullname, caller_number, caller_status,
               service_type, call_outcome, disposition,
               meeting_scheduled, total_cost_usd
        from calls
        {where_sql}
        order by started_at desc
        limit ${limit_idx} offset ${offset_idx}
    """
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *values)
    return [dict(r) for r in rows]


async def count_calls(
    *,
    since: datetime | None = None,
    disposition: list[str] | None = None,
    search: str | None = None,
    agent_id: str | None = None,
    test_filter: str = "exclude",
) -> int:
    """Return the total count matching the same filter list_calls uses."""
    if _pool is None:
        return 0
    where_sql, values = _build_calls_filter(since, disposition, search, agent_id, test_filter)
    sql = f"select count(*) from calls {where_sql}"
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *values) or 0


async def get_call(call_id: str) -> dict[str, Any] | None:
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("select * from calls where id = $1", call_id)
    if row is None:
        return None
    record = dict(row)
    # asyncpg returns jsonb columns as text by default; decode so templates can
    # iterate/attribute-access them (e.g. transcript_turns -> [{"role", "text", "ts_ms"}]).
    for col in _CALL_JSONB_COLUMNS:
        v = record.get(col)
        if isinstance(v, str):
            try:
                record[col] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return record


async def get_recent_call_by_number(
    caller_number: str,
    *,
    exclude_id: str,
    days: int = 30,
) -> dict[str, Any] | None:
    """Look up the most recent prior call from this phone number for the
    repeat-caller continuity feature.

    Filters:
      - `caller_number = $1` (the Twilio From number on the new inbound call)
      - exclude the current call's `id` so we don't reference ourselves
      - within the last `days` (default 30 — most real-estate matters bounce
        back within that window; older calls are usually unrelated threads)
      - skip `is_test_call=true` rows so internal test traffic doesn't get
        surfaced as "remember last time we talked..."
      - skip rows with NULL first_name (extraction failed or call abandoned
        before Aria could collect identity — no useful reference)
      - skip rows still in `in_progress` (orphans like bSl5HIQl4w0 2026-05-12)

    Returns the single most-recent matching row as a dict, or None.
    """
    if _pool is None or not caller_number:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, started_at, ended_at, duration_seconds,
                   first_name, last_name, caller_fullname,
                   caller_status, service_type, call_outcome, disposition,
                   meeting_scheduled, meeting_datetime, sms_meeting_link,
                   forward_msg_to, caller_email, callback_number
            from calls
            where caller_number = $1
              and id != $2
              and started_at >= $3
              and is_test_call = false
              and first_name is not null
              and (disposition is null or disposition != 'in_progress')
            order by started_at desc
            limit 1
            """,
            caller_number,
            exclude_id,
            cutoff,
        )
    return dict(row) if row else None


async def patch_call(call_id: str, **fields: Any) -> bool:
    """Inline edit from the dashboard (manually correct a structured field).

    Returns True on a single-row update, False on no-op or error. We swallow DB
    errors here so a constraint violation (e.g. invalid enum value posted from
    the dashboard) doesn't bubble an asyncpg traceback to the user.
    """
    safe_fields = {k: v for k, v in fields.items() if k in _CALL_UPDATE_COLUMNS}
    if not safe_fields or _pool is None:
        return False
    set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(safe_fields.keys()))
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                f"update calls set {set_clause} where id = $1",
                call_id,
                *safe_fields.values(),
            )
    except Exception:  # noqa: BLE001
        log.exception("db.patch_call_failed", call_id=call_id, fields=list(safe_fields.keys()))
        return False
    return result.endswith(" 1")


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

async def insert_tool_call(
    *,
    call_id: str,
    name: str,
    args: dict[str, Any],
    output: dict[str, Any] | None,
    error: str | None,
    started_at: float,
    finished_at: float | None,
) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                insert into tool_calls
                  (call_id, name, args, output, error, started_at, finished_at, latency_ms)
                values ($1, $2, $3::jsonb, $4::jsonb, $5, to_timestamp($6), to_timestamp($7), $8)
                """,
                call_id,
                name,
                json.dumps(args or {}),
                json.dumps(output) if output is not None else None,
                error,
                started_at,
                finished_at,
                int((finished_at - started_at) * 1000) if finished_at else None,
            )
    except Exception:  # noqa: BLE001
        log.exception("db.insert_tool_call_failed", call_id=call_id, tool=name)


async def get_tool_calls(call_id: str) -> list[dict[str, Any]]:
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from tool_calls where call_id = $1 order by started_at asc",
            call_id,
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        for col in ("args", "output"):
            v = rec.get(col)
            if isinstance(v, str):
                try:
                    rec[col] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

async def get_active_prompt(agent_id: str = "aria") -> str:
    """Return the currently active system prompt for the given agent.

    1. Try the in-memory cache (60s TTL, keyed per-agent).
    2. Try the `prompts` table (is_active=true and agent_id=$1).
    3. Fall back to the registered on-disk prompt file for first boot.
    """
    now = time.time()
    cached = _prompt_cache.get(agent_id)
    if cached and (now - cached.fetched_at) < _PROMPT_CACHE_TTL_SECONDS:
        return cached.content

    if _pool is not None:
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select content from prompts where is_active = true and agent_id = $1 limit 1",
                    agent_id,
                )
            if row and row["content"]:
                _prompt_cache[agent_id] = _PromptCacheEntry(content=row["content"], fetched_at=now)
                return row["content"]
        except Exception:  # noqa: BLE001
            log.exception("db.get_active_prompt_failed", agent_id=agent_id)

    prompt_path = _PROMPT_FILE_FALLBACK
    try:
        from agents import get_agent_config  # Local import avoids a module-load cycle.
        prompt_path = get_agent_config(agent_id).prompt_fallback_path
    except Exception:  # noqa: BLE001
        prompt_path = _PROMPT_FILE_FALLBACK

    if prompt_path.exists():
        # Don't cache the file fallback. If the DB was just temporarily down,
        # we want the next call (potentially seconds later) to retry the DB
        # rather than serving stale file content for the full TTL window.
        return prompt_path.read_text(encoding="utf-8")

    log.error("db.no_prompt_anywhere", agent_id=agent_id)
    return ""


def invalidate_prompt_cache(agent_id: str | None = None) -> None:
    """Force the next get_active_prompt() to re-fetch (call after publish).

    Pass agent_id="aria" to drop just one cache entry; pass None to wipe all.
    """
    if agent_id is None:
        _prompt_cache.clear()
    else:
        _prompt_cache.pop(agent_id, None)


async def list_prompts(agent_id: str | None = None) -> list[dict[str, Any]]:
    if _pool is None:
        return []
    if agent_id is None:
        sql = (
            "select id, agent_id, is_active, notes, created_at, length(content) as content_length "
            "from prompts order by created_at desc"
        )
        args: tuple[Any, ...] = ()
    else:
        sql = (
            "select id, agent_id, is_active, notes, created_at, length(content) as content_length "
            "from prompts where agent_id = $1 order by created_at desc"
        )
        args = (agent_id,)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def insert_prompt_draft(content: str, notes: str | None = None, agent_id: str = "aria") -> int:
    if _pool is None:
        raise RuntimeError("DB not configured")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "insert into prompts (content, notes, is_active, agent_id) values ($1, $2, false, $3) returning id",
            content,
            notes,
            agent_id,
        )
    return row["id"]


async def publish_prompt(prompt_id: int) -> bool:
    """Atomically deactivate all prompts (for that prompt's agent) and activate the given one."""
    if _pool is None:
        return False
    async with _pool.acquire() as conn, conn.transaction():
        # Look up which agent this prompt belongs to so the deactivate scope matches.
        target_agent: str | None = await conn.fetchval(
            "select agent_id from prompts where id = $1",
            prompt_id,
        )
        if target_agent is None:
            return False
        await conn.execute(
            "update prompts set is_active = false where is_active = true and agent_id = $1",
            target_agent,
        )
        result = await conn.execute(
            "update prompts set is_active = true where id = $1",
            prompt_id,
        )
    invalidate_prompt_cache(target_agent)
    return result.endswith(" 1")
