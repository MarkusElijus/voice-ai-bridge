"""Backfill structured summary fields for calls that lost them to a transient
xAI 5xx during post_call extraction.

The original failure: jFqaHpAvAgc 2026-05-06 — xAI returned a single 503 on the
chat-completions request, the (then) no-retry path silently bailed, and the
call landed in the DB with full transcript + recording but null
first_name/last_name/caller_status/service_type/etc. The 2026-05-06 retry fix
in `post_call._extract_structured` prevents recurrence; this script repairs
records that were already lost before the fix landed.

Usage:

    # Backfill a single call by id
    PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_summary.py jFqaHpAvAgc

    # Dry-run: show what would change without writing
    PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_summary.py --dry-run jFqaHpAvAgc

    # Backfill every call where caller_status IS NULL but a transcript exists
    PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_summary.py --all

    # --all + --dry-run to preview the entire batch
    PYTHONPATH=. .venv/Scripts/python.exe scripts/backfill_summary.py --all --dry-run

Idempotency: by default, calls that already have a non-null `caller_status`
are skipped (they're already extracted). Pass `--force` to re-run anyway.

Safety: failures are isolated per-call — one bad record does not abort the
batch. The script logs each call's outcome and exits non-zero if ANY call
failed extraction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import db
from logging_config import configure_logging, log
from post_call import _extract_structured, _soft_truncate_extracted  # noqa: F401  (kept for future hand-edits)
from settings import settings


# Fields the extractor populates. We diff these to decide whether to update,
# and write only the non-null ones (so we don't blow away a hand-edited
# field with a fresh null from the LLM).
EXTRACTED_FIELDS = (
    "first_name",
    "last_name",
    "caller_fullname",
    "caller_email",
    "callback_number",
    "caller_status",
    "service_type",
    "forward_msg_to",
    "call_outcome",
    "sms_meeting_link",
    "meeting_scheduled",
    "meeting_datetime",
    "meeting_notes",
)


def _format_transcript_from_db(call_row: dict[str, Any], tool_calls: list[dict[str, Any]]) -> str:
    """Mirror post_call._format_transcript but read from DB columns instead
    of an in-memory XaiVoiceSession. Same shape so the extraction prompt is
    identical."""
    parts: list[str] = []
    caller = call_row.get("transcript_caller")
    bot = call_row.get("transcript_bot")
    if caller:
        parts.append(f"Caller said overall:\n{caller}")
    if bot:
        parts.append(f"Aria said overall:\n{bot}")
    if tool_calls:
        tools_summary = "; ".join(
            f"{tc.get('name')}({json.dumps(tc.get('args') or {})[:120]})"
            for tc in tool_calls
        )
        parts.append(f"Tool calls during call: {tools_summary}")
    return "\n\n".join(parts)


async def _list_candidate_call_ids() -> list[str]:
    """Return call_ids with transcript content but null caller_status — i.e.
    likely victims of the pre-retry-fix transient-failure bug.

    Limits to 200 in one batch to keep blast radius small; rerun if more.
    """
    pool = db.get_pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id from calls
            where caller_status is null
              and (transcript_caller is not null or transcript_bot is not null)
              and (transcript_caller <> '' or transcript_bot <> '')
            order by started_at desc
            limit 200
            """
        )
    return [r["id"] for r in rows]


async def _backfill_one(call_id: str, *, force: bool, dry_run: bool) -> str:
    """Process a single call. Returns one of: 'updated', 'skipped', 'no_data',
    'extraction_failed', 'no_change'."""
    call = await db.get_call(call_id)
    if not call:
        log.error("backfill.not_found", call_id=call_id)
        return "extraction_failed"

    if not force and call.get("caller_status") is not None:
        log.info("backfill.already_extracted", call_id=call_id, caller_status=call.get("caller_status"))
        return "skipped"

    tool_calls = await db.get_tool_calls(call_id)
    transcript = _format_transcript_from_db(call, tool_calls)
    if not transcript.strip():
        log.warning("backfill.empty_transcript", call_id=call_id)
        return "no_data"

    extracted = await _extract_structured(transcript)
    if extracted is None:
        log.error("backfill.extraction_failed", call_id=call_id, transcript_chars=len(transcript))
        return "extraction_failed"

    # Compose the update: only non-null extracted fields, and only fields
    # currently null in the DB (or all fields if --force).
    updates: dict[str, Any] = {}
    for field in EXTRACTED_FIELDS:
        new = getattr(extracted, field, None)
        if new is None:
            continue
        if not force and call.get(field) is not None:
            continue
        updates[field] = new

    if not updates:
        log.info("backfill.no_change", call_id=call_id, extracted_field_count=len(extracted.model_fields_set))
        return "no_change"

    log.info(
        "backfill.will_update",
        call_id=call_id,
        fields={k: (v if not isinstance(v, str) else v[:60]) for k, v in updates.items()},
    )
    if dry_run:
        return "updated"

    await db.update_call_ended(call_id, **updates)
    log.info("backfill.updated", call_id=call_id, field_count=len(updates))
    return "updated"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("call_id", nargs="?", help="A specific call id to backfill.")
    g.add_argument("--all", action="store_true",
                   help="Backfill every call with transcript content but null caller_status.")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract and overwrite even if caller_status is already set.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing to the DB.")
    args = parser.parse_args()

    configure_logging(settings.LOG_LEVEL)
    await db.init_pool()

    try:
        if args.all:
            ids = await _list_candidate_call_ids()
            if not ids:
                print("No candidate calls (every call already has caller_status populated).")
                return 0
            print(f"Found {len(ids)} candidate call(s) to backfill.")
        else:
            ids = [args.call_id]

        results: dict[str, list[str]] = {}
        for cid in ids:
            outcome = await _backfill_one(cid, force=args.force, dry_run=args.dry_run)
            results.setdefault(outcome, []).append(cid)

        # Summary
        print()
        print("=== summary ===")
        for outcome, cids in sorted(results.items()):
            print(f"  {outcome:20s}: {len(cids)}")
            for cid in cids[:5]:
                print(f"      {cid}")
            if len(cids) > 5:
                print(f"      ... +{len(cids) - 5} more")
        if args.dry_run:
            print()
            print("(dry-run — no DB writes performed)")

        return 1 if results.get("extraction_failed") else 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
