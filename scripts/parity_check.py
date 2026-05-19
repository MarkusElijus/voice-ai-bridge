"""Per-scenario auto-evaluator for the Aria ↔ Vapi-Aria parity test.

After the operator places a test call, this script pulls the row by call_id and
checks the structural signals for the named scenario. It can't judge
subjective things like "Aria stayed polite" or "Spanish was natural" —
those still need a human ear on the recording. But it catches the easy
mechanical fails (wrong tool called, missing event, post-call didn't run,
disposition wrong) so the human ear only has to focus on the gray areas.

Usage:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/parity_check.py <call_id> <scenario>

    scenario is 1..10 matching docs/parity-test.md.

Exit code 0 = PASS (mechanical checks all green; subjective items flagged
                    for human review).
Exit code 1 = FAIL (one or more mechanical signals wrong).
Exit code 2 = USAGE / DB error.

Output is structured: per-check status (✓ / ✗ / ?) followed by a one-line
remediation hint for any ✗.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import db


@dataclass
class CheckResult:
    name: str
    passed: bool | None  # None = needs human review
    detail: str


def fmt(check: CheckResult) -> str:
    # ASCII markers — Windows cp1252 console can't encode ✓/✗/? glyphs
    # without explicit encoding overrides, and the runbook's grep-ability
    # is just as good with [PASS]/[FAIL]/[REVW].
    if check.passed is True:
        sym = "[PASS]"
    elif check.passed is False:
        sym = "[FAIL]"
    else:
        sym = "[REVW]"
    return f"  {sym} {check.name}: {check.detail}"


# --- Generic helpers -------------------------------------------------------

def _has_tool(tool_calls: list[dict], name: str) -> list[dict]:
    return [tc for tc in tool_calls if tc.get("name") == name]


def _tool_succeeded(tc: dict) -> bool:
    """A tool call is considered 'successful' if its error column is null/empty
    AND its output dict (if any) doesn't have error=True / a non-null string error."""
    if tc.get("error"):
        return False
    out = tc.get("output") or {}
    if isinstance(out, dict):
        e = out.get("error")
        if e is True:
            return False
        if isinstance(e, str) and e:
            return False
    return True


def _disposition(call: dict) -> str:
    return (call.get("disposition") or "").lower()


def _post_call_ran(call: dict) -> bool:
    """post_call.run is what writes ended_at + extracted fields. If those
    are present, post-call did fire."""
    return bool(call.get("ended_at"))


def _turn_count(call: dict, role: str) -> int:
    return sum(1 for t in (call.get("transcript_turns") or []) if t.get("role") == role)


def _assistant_text(call: dict) -> str:
    return " ".join(
        t.get("text", "")
        for t in (call.get("transcript_turns") or [])
        if t.get("role") == "assistant"
    )


# --- Per-scenario checks ---------------------------------------------------

def check_1_schedule_consult(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    out: list[CheckResult] = []
    avail = _has_tool(tool_calls, "hubspot_get_availability_v3")
    out.append(CheckResult(
        "hubspot_get_availability_v3 called",
        len(avail) >= 1,
        f"called {len(avail)}x (expected >=1)",
    ))
    book = _has_tool(tool_calls, "hubspot_book_meeting_v3")
    out.append(CheckResult(
        "hubspot_book_meeting_v3 called",
        len(book) >= 1,
        f"called {len(book)}x (expected >=1)",
    ))
    if book:
        out.append(CheckResult(
            "hubspot_book_meeting_v3 succeeded",
            _tool_succeeded(book[0]),
            f"output={book[0].get('output')}",
        ))
    out.append(CheckResult(
        "meeting_scheduled flag set in post-call extraction",
        bool(call.get("meeting_scheduled")),
        f"meeting_scheduled={call.get('meeting_scheduled')}",
    ))
    out.append(CheckResult(
        "post_call.run fired",
        _post_call_ran(call),
        f"ended_at={call.get('ended_at')}",
    ))
    return out


def check_2_urgent_transfer(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    out: list[CheckResult] = []
    sms = _has_tool(tool_calls, "send_sms_summary_openphone")
    transfer = _has_tool(tool_calls, "transferCall_v3")
    out.append(CheckResult(
        "send_sms_summary_openphone skipped",
        len(sms) == 0,
        f"called {len(sms)}x",
    ))
    out.append(CheckResult(
        "transferCall_v3 called",
        len(transfer) >= 1,
        f"called {len(transfer)}x",
    ))
    if transfer:
        args = transfer[0].get("args") or {}
        dest = args.get("destination")
        summary = (args.get("summary") or "").strip()
        out.append(CheckResult(
            "transfer destination is attorney number +15555550100",
            dest == "+15555550100",
            f"got destination={dest!r}",
        ))
        out.append(CheckResult(
            "transfer summary is populated for whisper",
            bool(summary),
            f"summary_chars={len(summary)}",
        ))
    out.append(CheckResult(
        "disposition reflects attorney transfer",
        "transfer" in _disposition(call) or "attorney" in _disposition(call),
        f"disposition={_disposition(call)!r}",
    ))
    return out


def check_3_voicemail(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    transfer = _has_tool(tool_calls, "transferCall_v3")
    out = [CheckResult(
        "transferCall_v3 called",
        len(transfer) >= 1,
        f"called {len(transfer)}x",
    )]
    if transfer:
        dest = (transfer[0].get("args") or {}).get("destination")
        out.append(CheckResult(
            "transfer destination is voicemail +15555550101",
            dest == "+15555550101",
            f"got destination={dest!r}",
        ))
    return out


def check_4_spanish(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    # Firm policy as of 2026-05-07: Aria speaks ONLY English. If a caller
    # opens in Spanish (or any non-English language), Aria must stay in
    # English, deliver a polite-decline script, and end the call. Engaging
    # in Spanish is a FAIL, not a pass.
    text = _assistant_text(call).lower()
    spanish_markers = [" que ", " para ", " usted ", " gracias", " si ",
                       " hola", "necesita", " puedo ", " dias ", " cita ",
                       " bienvenido", " ayudar", " comprar", " casa "]
    spanish_hits = sum(1 for m in spanish_markers if m in f" {text} ")
    decline_signal = any(p in text for p in (
        "only speak english", "only english", "speak english", "english-speaking",
        "only able to assist english", "only able to help english",
    ))
    name_set = {tc.get("name") for tc in tool_calls}
    return [
        CheckResult(
            "assistant text stays in English (no Spanish vocabulary)",
            spanish_hits == 0,
            f"spanish_hits={spanish_hits} (0 expected; nonzero = policy violation)",
        ),
        CheckResult(
            "polite-decline script delivered (English-only mention)",
            decline_signal,
            "expected phrase like 'I only speak English' / 'only able to assist English-speaking clients'",
        ),
        CheckResult(
            "end_call invoked (or auto-end backstop fired)",
            "end_call" in name_set or _auto_end_signal(call),
            "expected end_call tool call OR a goodbye that triggers the auto-end backstop",
        ),
        CheckResult(
            "manual review",
            None,
            "listen to recording - Aria stays in English the whole time, tone is polite not dismissive, ends within 1-2 turns",
        ),
    ]


def _auto_end_signal(call: dict) -> bool:
    """Best-effort: when end_call tool isn't invoked, the auto-end backstop
    fires on a goodbye phrase in the final assistant turn. Recognising that
    pattern is good-enough proof of termination intent."""
    turns = call.get("transcript_turns") or []
    for t in reversed(turns):
        if t.get("role") == "assistant":
            tail = (t.get("text") or "").lower()[-80:]
            return any(p in tail for p in ("goodbye", "have a great day", "take care"))
    return False


def check_5_interrupt(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    # Mechanical signals are limited — barge-in is observable mainly in the
    # caller's perception. We can check that the assistant produced multiple
    # turns (i.e., didn't lock up after being interrupted).
    return [
        CheckResult(
            "assistant produced >=2 turns (didn't lock up after interrupt)",
            _turn_count(call, "assistant") >= 2,
            f"assistant_turns={_turn_count(call, 'assistant')}",
        ),
        CheckResult(
            "caller produced >=1 turn",
            _turn_count(call, "caller") >= 1,
            f"caller_turns={_turn_count(call, 'caller')}",
        ),
        CheckResult(
            "manual review",
            None,
            "did Aria stop within ~1 s of your interrupt? did she resume naturally?",
        ),
    ]


def check_6_hubspot_503(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    avail = _has_tool(tool_calls, "hubspot_get_availability_v3")
    out: list[CheckResult] = []
    out.append(CheckResult(
        "hubspot_get_availability_v3 called",
        len(avail) >= 1,
        f"called {len(avail)}x",
    ))
    if avail:
        # Should have FAILED gracefully (output.error truthy)
        first = avail[0]
        output = first.get("output") or {}
        out.append(CheckResult(
            "hubspot_get_availability_v3 returned error (HUBSPOT_FORCE_503)",
            (output.get("error") is True) or bool(first.get("error")),
            f"output={output}",
        ))
    text = _assistant_text(call).lower()
    apology_markers = ["sorry", "apologize", "having trouble", "unable", "experiencing"]
    out.append(CheckResult(
        "Aria apologized / acknowledged the failure in voice",
        any(m in text for m in apology_markers),
        f"matched markers={[m for m in apology_markers if m in text]}",
    ))
    out.append(CheckResult(
        "manual review",
        None,
        "did Aria offer the SMS-link fallback or take a callback? compare to Vapi behavior",
    ))
    return out


def check_7_long_silence(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    text = _assistant_text(call).lower()
    idle_markers = ["are you still there", "still here if you need"]
    return [
        CheckResult(
            "Aria emitted at least one idle prompt",
            any(m in text for m in idle_markers),
            f"matched={[m for m in idle_markers if m in text]}",
        ),
        CheckResult(
            "post_call ran (idle watcher didn't crash)",
            _post_call_ran(call),
            f"ended_at={call.get('ended_at')}",
        ),
    ]


def check_8_hostile(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    text = _assistant_text(call).lower()
    forbidden = ["fuck", "shit", "stupid", "idiot", "shut up"]
    return [
        CheckResult(
            "no profanity / ad-hominem from Aria",
            not any(w in text for w in forbidden),
            f"matched={[w for w in forbidden if w in text]}",
        ),
        CheckResult(
            "Aria produced <=6 turns (didn't argue)",
            _turn_count(call, "assistant") <= 6,
            f"assistant_turns={_turn_count(call, 'assistant')}",
        ),
        CheckResult(
            "post_call ran (graceful end)",
            _post_call_ran(call),
            f"ended_at={call.get('ended_at')}",
        ),
        CheckResult(
            "manual review",
            None,
            "tone professional; no attempt to argue back",
        ),
    ]


def check_9_hangup_mid_tool(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    # If the tester hung up mid-book_meeting, the tool call may be missing OR
    # have started_at but no finished_at. Either case: we want post_call
    # to have still fired.
    return [
        CheckResult(
            "post_call.run fired despite mid-tool hangup",
            _post_call_ran(call),
            f"ended_at={call.get('ended_at')} (the critical signal)",
        ),
        CheckResult(
            "at least one tool call recorded",
            len(tool_calls) >= 1,
            f"tool_calls={len(tool_calls)}",
        ),
    ]


def check_10_concurrent(call: dict, tool_calls: list[dict]) -> list[CheckResult]:
    # Per-call check is identical to scenario 1 baseline; the concurrency
    # itself has to be verified by inspecting BOTH call_ids. This script
    # only sees one at a time. Print a hint for the second one.
    return [
        CheckResult(
            "post_call.run fired",
            _post_call_ran(call),
            f"ended_at={call.get('ended_at')}",
        ),
        CheckResult(
            "manual review (concurrent-call check)",
            None,
            "run this script against BOTH call_ids; verify both have post_call ran "
            "AND distinct caller transcripts (no audio crossover into the wrong call)",
        ),
    ]


CHECKS = {
    1: check_1_schedule_consult,
    2: check_2_urgent_transfer,
    3: check_3_voicemail,
    4: check_4_spanish,
    5: check_5_interrupt,
    6: check_6_hubspot_503,
    7: check_7_long_silence,
    8: check_8_hostile,
    9: check_9_hangup_mid_tool,
    10: check_10_concurrent,
}

SCENARIO_NAMES = {
    1: "New caller schedules a consult",
    2: "Urgent closing → warm transfer",
    3: "Caller wants voicemail",
    4: "Spanish caller",
    5: "Mid-sentence interrupt",
    6: "HubSpot 503 mid-availability",
    7: "Long silence (10s)",
    8: "Hostile caller",
    9: "Hangup mid-tool-call",
    10: "Two concurrent calls",
}


async def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: parity_check.py <call_id> <scenario_number>", file=sys.stderr)
        return 2
    call_id, scenario_str = sys.argv[1], sys.argv[2]
    try:
        scenario = int(scenario_str)
    except ValueError:
        print(f"scenario must be an integer 1..10, got {scenario_str!r}", file=sys.stderr)
        return 2
    if scenario not in CHECKS:
        print(f"scenario must be 1..10, got {scenario}", file=sys.stderr)
        return 2

    await db.init_pool()
    try:
        call = await db.get_call(call_id)
        if call is None:
            print(f"ERROR: call_id={call_id!r} not found", file=sys.stderr)
            return 2
        tool_calls = await db.get_tool_calls(call_id)
    finally:
        await db.close_pool()

    print(f"\n=== Scenario {scenario}: {SCENARIO_NAMES[scenario]} ===")
    print(f"call_id:        {call_id}")
    print(f"started_at:     {call.get('started_at')}")
    print(f"duration_s:     {call.get('duration_seconds')}")
    print(f"is_test_call:   {call.get('is_test_call')}")
    print(f"agent_id:       {call.get('agent_id')}")
    print(f"disposition:    {call.get('disposition')}")
    print(f"tool_calls:     {len(tool_calls)} {[tc.get('name') for tc in tool_calls]}")
    print(f"transcript_turns: caller={_turn_count(call, 'caller')} assistant={_turn_count(call, 'assistant')}")
    print()

    results = CHECKS[scenario](call, tool_calls)
    for r in results:
        print(fmt(r))

    failures = [r for r in results if r.passed is False]
    review = [r for r in results if r.passed is None]
    passes = [r for r in results if r.passed is True]
    print()
    print(f"Mechanical: {len(passes)} pass / {len(failures)} fail")
    if review:
        print(f"Manual review needed: {len(review)} item(s) — listen to the recording or read the transcript on /admin/calls/{call_id}")
    if failures:
        print("\nVERDICT: FAIL -- fix the [FAIL] items above and re-run the scenario.")
        return 1
    if review:
        print("\nVERDICT: MECHANICAL PASS -- confirm the [REVW] items by ear/eye, then mark scenario complete.")
        return 0
    print("\nVERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
