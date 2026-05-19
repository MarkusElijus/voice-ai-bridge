"""xAI Voice Agent WebSocket session.

One instance per inbound call. Connects to xAI realtime, sends session.update
with our audio format (PCMU passthrough) + Aria tools, then exposes:

- forward_caller_audio(b64_mulaw): caller -> xAI
- events(): async iterator of xAI server events for the bridge to react to
- send_function_output(call_id, output): return tool result
- request_response(): trigger model to respond (after tool result)
- greet(): inject "say hello" item + response.create (for inbound greeting)
- close(): tear down

Accumulates state for post_call.run().
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - zoneinfo missing in some envs
    _CT = None

import websockets

from recording import RecordingBuffer
from websockets.client import WebSocketClientProtocol

from logging_config import log
from settings import settings
from tools import AGENT_TOOL_DEFS as CLASSIC_AGENT_TOOL_DEFS


@dataclass
class ToolCallRecord:
    call_id: str
    name: str
    args: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def latency_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)


# Idle / silence handling
IDLE_TIMEOUT_SECONDS = 15
IDLE_MESSAGES = [
    "Are you still there?",
    "I'm still here if you need assistance.",
]


# Auto-end backstop — fires when Aria speaks a closing line but skips the
# end_call function. Three confirmed cases (calls QHWticUonTM 2026-05-05,
# jFqaHpAvAgc 2026-05-06, 4abkbiOst-U 2026-05-06) where the model said a
# textbook closing and never invoked end_call, leaving the line open until
# the caller hung up manually (the first two ended with "Goodbye.", the
# third was caller-declines-then-leaves and Aria closed with "Have a
# great day."). Tool-description prompting and the
# `<!-- patch:end-call-after-goodbye -->` system-prompt override haven't
# been enough on their own — this regex at the session layer is the
# deterministic backstop.
#
# Anchored at end-of-turn — requires the closing phrase to be the LAST
# substantive content (only `.`, `!`, or whitespace allowed after). A
# trailing `?` disqualifies (questions aren't closings: "Have a great day,
# is there anything else?" must NOT match). The set of closers below
# covers the customer-service closing lexicon the assistant's prompt produces.
_AUTO_END_TAIL_RE = re.compile(
    r"\b(?:"
    r"goodbye|good\s*bye|bye[\s-]?bye"
    r"|have a (?:great|nice|wonderful|good|lovely) (?:day|one|night|evening|weekend|time)"
    r"|take care(?:\s+now)?"
    r"|talk (?:to you )?(?:later|soon)"
    r"|enjoy (?:your|the rest of your) (?:day|night|evening|weekend)"
    r")\b[\.\!\s]*$",
    re.IGNORECASE,
)

# Verbal intent to terminate the call. Catches Aria announcing she's
# about to end the call when the model SHOULD have invoked end_call as a
# function_call but emitted only spoken text. Common pattern across
# scenario 1 / 4-retest / 6: "...Have a great day! I'll go ahead and end
# our call now." — the "Have a great day" phrase doesn't trigger
# _AUTO_END_TAIL_RE because it's no longer the absolute tail. We accept
# this as a closing signal regardless of position in the last 100 chars.
_AUTO_END_INTENT_RE = re.compile(
    r"\b(?:"
    r"(?:i'?ll|i will|i'?m going to|i am going to|let me|i can|going to|gonna)"
    r"\s+(?:go ahead and\s+)?(?:end|wrap\s*up|disconnect|close|finish|hang\s*up)"
    r"\s+(?:the|this|our|your)?\s*call"
    r"|(?:end|ending|wrapping\s*up|disconnecting|closing|finishing)\s+(?:the|this|our|your)\s+call"
    r"|(?:end|wrap\s*up|disconnect|close)\s+(?:the|this|our|your)\s+call\s+(?:now|here|for you|shortly)"
    r")",
    re.IGNORECASE,
)

# Model emits the tool name as inline XML text instead of a structured
# function_call. Surfaced in scenario 7 idle-give-up: assistant text
# included literal "<end_call/>". Strong terminal-intent signal — match
# anywhere in the response, not just the tail.
_AUTO_END_XML_RE = re.compile(r"<\s*/?\s*end[_-]call\s*/?\s*>", re.IGNORECASE)

AUTO_END_GRACE_SECONDS = 5.0  # let the goodbye audio play out before tearing down


TRANSFER_BACKSTOP_DELAY_SECONDS = 8.0
TRANSFER_BACKSTOP_CUE = (
    "[Internal cue - not for caller's ears. Several seconds have passed since "
    "you announced the transfer. The caller is waiting silently. Take the "
    "transfer action immediately now.]"
)


# Transfer-intent: Aria announced an imminent transfer ("I'll transfer you",
# "Let me connect you", "one moment while I get you connected", etc.). Used
# by the idle watcher to STOP firing "Are you still there?" prompts after
# the announcement — the caller is rightly silent waiting to be bridged, and
# idle prompts make Aria sound confused. Diagnosed from call Bb0TcMRAcL4
# (2026-05-12): boss got 3 idle prompts stacked on top of an unfired
# transferCall_v3. When the bridge provides a deterministic fallback callback
# it can dispatch the transfer directly; otherwise it sends one soft internal
# cue to the model as a last resort.
_TRANSFER_INTENT_RE = re.compile(
    r"\b(?:"
    r"i(?:'ll|'m|\s+am|\s+will)\s+(?:going\s+to\s+|about\s+to\s+|gonna\s+)?(?:transfer|connect|put)\s+you"
    r"|"
    r"let\s+me\s+(?:transfer|connect|put)\s+you"
    r"|"
    r"i'll\s+go\s+ahead\s+and\s+(?:transfer|connect)\s+you"
    r"|"
    r"one\s+moment\s+while\s+i\s+(?:transfer|connect|get|put)"
    r"|"
    r"transferring\s+you"
    r"|"
    r"sending\s+you\s+to\s+voicemail"
    r"|"
    r"putting\s+you\s+through(?:\s+to\s+voicemail)?"
    r"|"
    r"connecting\s+you\s+(?:now|with|to)"
    r")",
    re.IGNORECASE,
)


_DAY_ZERO_RE = re.compile(r"([A-Z][a-z]+) 0(\d)\b")  # "May 03" -> "May 3"
_HOUR_ZERO_RE = re.compile(r"\bat 0(\d):")  # "at 09:45" -> "at 9:45"


def _humanize_dt(dt: datetime) -> str:
    """Format a datetime as a short Central-Time friendly string for Aria
    to read aloud (used in repeat-caller references). Example:
    "Wednesday May 13 at 9:45 AM Central Time". Falls back to ISO if
    ZoneInfo is unavailable or the input isn't tz-aware. Uses portable
    %d/%I formats then strips leading zeros (Windows lacks the %-d glibc
    extension). Strips ONLY day-of-month and hour leading zeros — minute
    leading zero stays ("9:05" not "9:5")."""
    if _CT is None or dt.tzinfo is None:
        return dt.isoformat(timespec="minutes")
    local = dt.astimezone(_CT)
    raw = local.strftime("%A %B %d at %I:%M %p Central Time")
    raw = _DAY_ZERO_RE.sub(r"\1 \2", raw)
    raw = _HOUR_ZERO_RE.sub(r"at \1:", raw)
    return raw


def _looks_like_transfer_intent(text: str) -> bool:
    """True when Aria just spoke a phrase that ANNOUNCES a pending transfer.

    Catches the verbal announcement that should precede a transferCall_v3
    function_call. If the tool fires, Twilio detaches the Media Stream and
    this flag becomes moot. If the tool DOESN'T fire (model narrating only),
    the idle watcher reads this flag and stops sending "Are you still there?"
    prompts — they sound terrible when the caller is sitting silent waiting
    for a transfer that's already been announced.
    """
    if not text:
        return False
    return bool(_TRANSFER_INTENT_RE.search(text))


def _looks_like_voicemail_transfer_intent(text: str) -> bool:
    """True when the announced transfer is specifically to voicemail."""
    if not text:
        return False
    return "voicemail" in text.lower() and _looks_like_transfer_intent(text)


def _looks_like_closing(text: str) -> bool:
    """Return True if Aria just spoke a clear closing signal.

    Three signals (any one is enough):
    1. Goodbye phrase at the absolute tail of the response.
    2. Verbal announcement of intent to end the call ("I'll go ahead and
       end our call now"). Common when end_call function_call doesn't
       fire but the model verbally narrates the action.
    3. XML-style tool name as inline text ("<end_call/>"). Strong
       indication the model meant to invoke end_call but emitted it as
       text instead.
    """
    if not text:
        return False
    if _AUTO_END_XML_RE.search(text):
        return True
    tail = text[-100:]
    if _AUTO_END_TAIL_RE.search(tail):
        return True
    if _AUTO_END_INTENT_RE.search(tail):
        return True
    return False


@dataclass(frozen=True)
class RepeatCallerContext:
    text: str
    prev_first: str
    days_ago: int
    disposition: str


def _build_repeat_caller_context(
    prev: dict[str, Any],
    _caller_number: str | None,
    *,
    now: datetime | None = None,
) -> RepeatCallerContext | None:
    """Build the quiet continuity note injected before Aria greets.

    Keep this text low-risk if accidentally spoken: avoid implementation
    vocabulary and avoid exposing the actual phone number. The bridge already
    proved the phone-number relationship before calling this helper.
    """
    prev_first = (prev.get("first_name") or "").strip()
    if not prev_first:
        return None

    now = now or datetime.now(timezone.utc)
    started = prev.get("started_at")
    if isinstance(started, datetime):
        days_ago = max(0, (now - started).days)
    else:
        days_ago = -1

    disposition = (prev.get("disposition") or "unknown").lower()
    raw_service_type = (prev.get("service_type") or "").strip()
    service_type = raw_service_type or "their matter"
    service_phrase = raw_service_type.lower() if raw_service_type else service_type
    caller_status = prev.get("caller_status") or ""
    call_outcome = prev.get("call_outcome") or ""
    forward_msg_to = (prev.get("forward_msg_to") or "").strip()
    meeting_scheduled = bool(prev.get("meeting_scheduled"))
    meeting_dt = prev.get("meeting_datetime")
    sms_meeting_link = (prev.get("sms_meeting_link") or "").lower() == "yes"

    if disposition == "scheduled" and meeting_scheduled and isinstance(meeting_dt, datetime):
        if meeting_dt > now:
            suggested = (
                f"Oh hi again, {prev_first}! I see we have you booked for "
                f"{_humanize_dt(meeting_dt)}. What can I help with today?"
            )
        else:
            suggested = (
                f"Hi again, {prev_first}! How did your appointment go? "
                f"Is there anything else I can help with today?"
            )
    elif disposition == "scheduled" and meeting_scheduled:
        suggested = (
            f"Hi again, {prev_first}! I see we got you booked for a consultation "
            f"last time about {service_phrase}. What can I help with today?"
        )
    elif disposition == "transferred_attorney":
        suggested = (
            f"Hi again, {prev_first}! Last time we had you on with an attorney about "
            f"{service_phrase}. Are you following up, or is this about something new?"
        )
    elif disposition == "transferred_voicemail":
        suggested = (
            f"Hi again, {prev_first}! Last time we sent you over to leave a message. "
            f"Did you connect with the team, or is there anything I can help with today?"
        )
    elif disposition == "info_only" and (
        forward_msg_to
        or "message" in call_outcome.lower()
        or "follow-up" in call_outcome.lower()
    ):
        recipient = forward_msg_to or "the team"
        suggested = (
            f"Hi again, {prev_first}! I see you called earlier about {service_phrase} "
            f"and reaching {recipient}. Are you following up on that, or is there "
            f"something else I can help with today?"
        )
    elif disposition == "info_only" and sms_meeting_link:
        suggested = (
            f"Hi again, {prev_first}! Last time we texted you a scheduling link for "
            f"{service_phrase}. Did you get a chance to book, or would you like to set "
            f"up that consultation now?"
        )
    elif disposition == "info_only":
        suggested = (
            f"Hi again, {prev_first}! Last time we talked about {service_phrase} but "
            f"didn't get a chance to book. Would you like to set up that consultation today?"
        )
    elif disposition == "abandoned":
        suggested = (
            f"Hi again, {prev_first}! Looks like we got disconnected last time when we "
            f"were discussing {service_phrase}. Want to pick up where we left off?"
        )
    else:
        suggested = (
            f"Hi again, {prev_first}! I see we spoke recently. "
            f"What can I help with today?"
        )

    ts_phrase = f"{days_ago} day{'s' if days_ago != 1 else ''} ago" if days_ago >= 0 else "recently"
    full_name = f"{prev_first}{' ' + (prev.get('last_name') or '') if prev.get('last_name') else ''}".strip()
    facts = [
        f"Prior caller name: {full_name}",
        f"Called: {ts_phrase}",
    ]
    if service_type:
        facts.append(f"Service type: {service_type}")
    if caller_status:
        facts.append(f"Caller status: {caller_status}")
    facts.append(f"Disposition: {disposition}")
    if call_outcome:
        facts.append(f"Outcome: {call_outcome}")
    if forward_msg_to:
        facts.append(f"Message recipient: {forward_msg_to}")
    if meeting_scheduled and isinstance(meeting_dt, datetime):
        facts.append(f"Meeting scheduled: {_humanize_dt(meeting_dt)}")
    if sms_meeting_link:
        facts.append("SMS scheduling link was sent")

    text = (
        "[Continuity note for Aria - background only]\n\n"
        "This call comes from the same phone number as a recent call. Start with the "
        "normal greeting and ask for the caller's full name.\n\n"
        "Recent-call facts:\n  - " + "\n  - ".join(facts) + "\n\n"
        f"Suggested line after the caller says the first name {prev_first} or a common nickname:\n"
        f"  \"{suggested}\"\n\n"
        "Use:\n"
        f"- If the caller's first name is {prev_first} or a common nickname, say the suggested line once after their name.\n"
        "- If that same caller asks whether you remember the earlier call, answer from the recent-call facts instead of saying you cannot see prior calls.\n"
        "- If the caller gives a different first name, continue normal intake and say nothing about the earlier call.\n"
        "- Do not mention this note, phone history, records, or earlier-call details unless the name fits.\n"
        "- Use only the facts listed above.\n"
    )

    return RepeatCallerContext(
        text=text,
        prev_first=prev_first,
        days_ago=days_ago,
        disposition=disposition,
    )


@dataclass
class XaiVoiceSession:
    call_id: str
    instructions: str
    voice: str = settings.XAI_VOICE
    caller_number: str | None = None
    agent_id: str = "aria"
    tool_defs: list[dict[str, Any]] = field(default_factory=lambda: deepcopy(CLASSIC_AGENT_TOOL_DEFS))
    voicemail_transfer_backstop: Callable[[], Awaitable[bool]] | None = None
    warm_transfer_backstop: Callable[[], Awaitable[bool]] | None = None

    ws: WebSocketClientProtocol | None = None
    stream_sid: str | None = None
    session_ready: bool = False
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    bot_transcript_chunks: list[str] = field(default_factory=list)
    caller_transcript_chunks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    interruption_count: int = 0
    final_disposition: str | None = None

    # Chronological per-turn transcript for the Vapi-style chat view in the
    # admin dashboard. Each entry: {"role": "caller"|"assistant", "text": str, "ts_ms": int}.
    # ts_ms is milliseconds from `started_at`. Caller turns are appended on
    # `input_audio_transcription.completed`; assistant turns flush on
    # `response.done` after accumulating from `output_audio_transcript.delta`.
    transcript_turns: list[dict[str, Any]] = field(default_factory=list)
    # Per-response_id assistant-delta buffers. xAI can emit deltas for two
    # concurrent responses (e.g. an idle-prompt response we created racing the
    # auto-response from caller speech) and they interleave on the wire. A
    # single shared buffer would concatenate both into one mangled turn — fix
    # is to bucket deltas by `response_id` and flush each on its own
    # `response.done`. Diagnosed from call wUN0vYWH0Xg (2026-05-06): one
    # transcript entry held "Are you still there?... Thanks, Dan—let me..."
    # spliced together character-by-character.
    _assistant_buffers: dict[str, list[str]] = field(default_factory=dict)

    # Token usage accumulated from xAI `response.done` events (one per response turn).
    # The xAI realtime API mirrors OpenAI Realtime's usage shape: input/output token
    # totals plus a per-modality breakdown (audio/text). We sum across turns.
    xai_input_tokens: int = 0
    xai_output_tokens: int = 0
    xai_input_audio_tokens: int = 0
    xai_output_audio_tokens: int = 0
    xai_input_text_tokens: int = 0
    xai_output_text_tokens: int = 0

    # Idle watcher state
    last_activity_at: float = field(default_factory=time.time)
    idle_message_idx: int = 0
    idle_consecutive_prompts: int = 0
    _idle_task: asyncio.Task | None = None
    end_requested: bool = False  # set when end_call tool fires
    # Bytes of μ-law audio xAI has burst to us for the current assistant turn.
    # Used at `response.done` to project last_activity_at forward to the
    # wall-clock moment the assistant's audio will FINISH PLAYING in the caller's
    # ear (xAI bursts faster than 1×; raw response.done would mean idle
    # starts counting while the caller still has seconds of audio queued).
    _assistant_audio_bytes_in_turn: int = 0
    # True when the most-recent response.create came from `_send_idle_prompt`.
    # Tells the response.done handler NOT to reset idle_consecutive_prompts —
    # otherwise the "give up after 2 idle prompts" guard never trips because
    # the assistant's idle-prompt audio itself zeroes the counter.
    _pending_idle_response: bool = False

    # Speech / response in-flight flags driven by xAI events. The idle watcher
    # gates on both: it must NOT inject an "Are you still there?" prompt while
    # the caller is mid-utterance (would talk over them), and it must NOT
    # create a second response.create while a response is already streaming
    # (would race the auto-response from caller speech and produce interleaved
    # deltas).
    _speech_in_progress: bool = False
    _response_in_progress: bool = False

    # Auto-end backstop. Set to True once we've scheduled the goodbye-driven
    # teardown so we don't fire it twice if the model speaks two closing
    # turns. The backstop fires when an assistant turn ends with "goodbye"
    # but the model didn't invoke end_call — see _AUTO_END_TAIL_RE comment.
    _auto_end_scheduled: bool = False

    # Idle-watcher suppression after Aria announces a transfer. Set to
    # True when an assistant turn matches _TRANSFER_INTENT_RE. The watcher
    # then stops firing "Are you still there?" prompts so the caller isn't
    # nagged while waiting for the announced transfer to actually happen.
    # If the transfer action lands, Twilio detaches and the watcher dies
    # naturally. If it doesn't land within TRANSFER_BACKSTOP_DELAY_SECONDS,
    # the bridge can hard-force known transfer paths. If no hard-force
    # callback is available, it falls back to one internal cue.
    _transfer_intent_announced: bool = False
    _transfer_action_seen: bool = False
    _transfer_backstop_sent: bool = False
    _transfer_backstop_task: asyncio.Task | None = None

    # ID of the response currently streaming (set on response.created, cleared
    # on response.done/cancelled/failed). The BargeInController reads this to
    # tell barge-in apart from xAI's auto-reply: if the response that was in
    # progress when speech_started fired has ENDED and a NEW response started
    # before the debounce-timer expires, that new response is xAI replying to
    # the caller's speech — cancelling it would silence the assistant's normal turn.
    # Diagnosed from call vFIqBTyEPeY (2026-05-07).
    _active_response_id: str | None = None

    # Wall-clock timestamp when the currently-playing assistant audio is
    # projected to finish in the caller's ear. xAI bursts assistant audio
    # bytes faster than 1x wall-clock, so the bridge can't tell when audio
    # has actually played by watching response.done. We maintain this
    # timestamp incrementally on every output_audio.delta event and use it
    # in `send_function_output` to delay `response.create` until the
    # previous turn's audio has played out — without this delay xAI starts
    # generating the next response while the prior turn is still audible,
    # producing overlapping speech (per xAI docs best-practices section).
    _audio_playback_ends_at: float = 0.0

    # Diagnostic: log the shape of the first `response.done` event we see so
    # we can confirm where xAI puts usage (`response.usage` vs top-level).
    _logged_response_done_shape: bool = False

    # Per-call audio recording (μ-law passthrough; encoded to stereo WAV at end).
    recording: RecordingBuffer = field(default_factory=RecordingBuffer)
    end_source: str | None = None

    # Optional live consumer for transcript turns. The Twilio production path
    # leaves this None and `transcript_turns` is read once at end-of-call by
    # post_call.run. The voice playground sets this to an asyncio.Queue so its
    # WS handler can forward each turn to the browser as it's flushed —
    # producing a Vapi-style live chat-bubble view alongside the audio.
    # Each item put on the queue is the same dict appended to transcript_turns:
    #   {"role": "caller"|"assistant", "text": str, "ts_ms": int}
    transcript_queue: asyncio.Queue[dict[str, Any]] | None = None

    async def connect(self) -> None:
        url = f"{settings.XAI_REALTIME_URL}?model={settings.XAI_MODEL}"
        log.info("xai.connecting", call_id=self.call_id, url=url)
        self.ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {settings.XAI_API_KEY}"},
            max_size=None,
            ping_interval=20,
        )
        log.info("xai.connected", call_id=self.call_id)

        # Wait briefly for conversation.created, then send session.update.
        # We don't strictly need to wait — sending session.update right away works,
        # but waiting matches the cookbook pattern.
        first = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
        first_event = json.loads(first)
        log.info("xai.first_event", call_id=self.call_id, type=first_event.get("type"))

        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": self.voice,
                "instructions": self.instructions,
                # Vapi-parity startSpeakingPlan — silence_duration_ms is xAI's
                # "wait this long of caller silence before declaring the turn
                # over and letting Aria respond." Mirrors Vapi's
                # startSpeakingPlan.waitSeconds (default 500 ms).
                #
                # threshold + prefix_padding_ms set per xAI docs best-practice
                # audit (2026-05-07). xAI's defaults (0.85, 333 ms) are very
                # conservative — observed in calls vFIqBTyEPeY / uIvOEVDMs54
                # that the default threshold causes ~2 s lag before
                # speech_started fires, which makes barge-in perceptibly
                # broken even when the bridge logic is correct.
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": settings.XAI_VAD_THRESHOLD,
                    "prefix_padding_ms": settings.XAI_VAD_PREFIX_PADDING_MS,
                    "silence_duration_ms": settings.START_SPEAKING_WAIT_MS,
                },
                "audio": {
                    "input":  {"format": {"type": "audio/pcmu"}},
                    "output": {"format": {"type": "audio/pcmu"}},
                },
                "tools": self.tool_defs,
            },
        }))
        log.info("xai.session_update.sent", call_id=self.call_id)

    async def greet(self) -> None:
        """Trigger an inbound greeting. Called once after Twilio 'start' event.

        We don't dictate the greeting text — the system prompt's "Opening" section
        already specifies the assistant's exact greeting verbatim. We just nudge the model
        to begin speaking.
        """
        if self.ws is None:
            return
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "(The call has just connected. Begin with your standard opening.)"}],
            },
        }))
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def forward_caller_audio(self, b64_mulaw: str) -> None:
        """Twilio media -> xAI input_audio_buffer.append. Gated on session_ready."""
        if not self.session_ready or self.ws is None:
            return
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64_mulaw,
        }))

    async def cancel_response(self) -> None:
        """Tell xAI to stop generating the in-flight response.

        Sending only the transport-side `clear` (Twilio "clear" event /
        playground "{type:clear}" frame) drains the local playback queue but
        does NOT stop xAI from continuing to burst the rest of the assistant's
        response down the WebSocket. xAI bursts faster than 1x wall-clock —
        ~5 more seconds of assistant audio can arrive in <1 second of
        post-clear WS frames, get decoded, and re-fill the worklet queue.
        Result: caller barges in, hears ~5 seconds of Aria continuing
        anyway. Diagnosed from call wehzFIQOzi8 (2026-05-07).

        `response.cancel` is the OpenAI-realtime-style event xAI accepts to
        abort an in-flight response. We follow it with our existing
        response.cancelled handler clearing per-response buffers + the
        response-in-progress flag.

        Idempotent: bails if no response is currently streaming, so a
        spurious barge-in trigger after Aria already finished doesn't
        send a malformed cancel.
        """
        if self.ws is None or not self._response_in_progress:
            return
        try:
            await self.ws.send(json.dumps({"type": "response.cancel"}))
            log.info("xai.response_cancel.sent", call_id=self.call_id)
        except Exception:  # noqa: BLE001
            log.exception("xai.response_cancel_failed", call_id=self.call_id)

    async def send_function_output(self, call_id: str, output: dict[str, Any]) -> None:
        """Send a tool's result to xAI and request the next response.

        Per xAI docs best-practices: "If your client immediately sends
        conversation.item.create (with the function result) followed by
        response.create, the server starts generating the next response
        right away — even if the client is still playing audio from the
        previous turn. This causes overlapping audio."

        We submit the function_call_output immediately (no wait — xAI
        accepts these any time), then sleep until the projected end of
        the prior turn's audio playback before sending response.create.
        Capped at 10 s as a safety stop in case the projection drifts
        (e.g., very long Aria turn + very fast tool — never observed
        in practice but cheap insurance against a stuck call).
        """
        if self.ws is None:
            return
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output),
            },
        }))
        # Wait for the prior turn's audio to actually finish playing in the
        # caller's ear. _audio_playback_ends_at is the wall-clock projection
        # we maintain on every output_audio.delta event. Without this wait
        # xAI immediately overlaps the next response on top of the still-
        # playing audio (e.g. "Let me check..." over "The soonest available
        # appointment is..."). Cap at 10 s as a safety stop.
        wait_s = min(10.0, max(0.0, self._audio_playback_ends_at - time.time()))
        if wait_s > 0:
            log.info(
                "xai.send_function_output.waiting_for_playback",
                call_id=self.call_id,
                wait_s=round(wait_s, 2),
            )
            await asyncio.sleep(wait_s)
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded xAI server events. Updates internal state as side effect."""
        if self.ws is None:
            return
        async for raw in self.ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("xai.malformed_event", call_id=self.call_id, raw=raw[:200])
                continue

            etype = event.get("type")
            if etype == "session.updated":
                self.session_ready = True
                self._bump_activity()
                # Start idle watcher only after session is ready (avoids early-fire during handshake)
                if self._idle_task is None:
                    self._idle_task = asyncio.create_task(self._idle_watcher())
                log.info("xai.session_ready", call_id=self.call_id)
            elif etype == "response.output_audio_transcript.delta":
                # Note: xAI's grok-voice-think-fast-1.0 emits
                # `response.output_audio_transcript.delta` (NOT
                # `response.audio_transcript.delta`, which is the OpenAI
                # Realtime spec). See CLAUDE.md "Local quirks" and
                # tool_call_roundtrip.py (skill, patched 2026-04-27).
                delta = event.get("delta", "")
                if delta:
                    self.bot_transcript_chunks.append(delta)
                    rid = event.get("response_id") or "_unknown"
                    self._assistant_buffers.setdefault(rid, []).append(delta)
            elif etype == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    self.caller_transcript_chunks.append(transcript)
                    turn = {
                        "role": "caller",
                        "text": transcript,
                        "ts_ms": int((time.time() - self.started_at) * 1000),
                    }
                    self.transcript_turns.append(turn)
                    self._publish_turn(turn)
            elif etype == "input_audio_buffer.speech_started":
                self.interruption_count += 1
                self._speech_in_progress = True
                self._bump_activity()
            elif etype == "input_audio_buffer.speech_stopped":
                self._speech_in_progress = False
                self._bump_activity()
            elif etype == "response.created":
                # Aria is about to speak. Update the wall-clock activity
                # timestamp so the idle watcher doesn't fire mid-utterance,
                # but DO NOT reset idle_consecutive_prompts — that counter
                # tracks "how many idle prompts in a row without caller
                # engagement", and the assistant's own speech (including the idle
                # prompts themselves) doesn't count as caller engagement.
                # Resetting it here was a bug: the watcher would fire prompt
                # #1, this handler ran, idle_consecutive_prompts went 1->0,
                # next idle interval fired prompt #2 from a counter of 1
                # instead of 2, and the "give up after 2" guard never
                # tripped — call looped forever.
                self._response_in_progress = True
                self._active_response_id = (event.get("response") or {}).get("id")
                self.last_activity_at = time.time()
                # Belt-and-suspenders: clear any per-turn audio bytes that
                # might have leaked past the previous response.done (rare,
                # but cheap insurance). New turn starts at 0.
                self._assistant_audio_bytes_in_turn = 0
                # Reset the projected playback-end timestamp to "now" — any
                # previous projection from a prior turn is stale. As deltas
                # arrive in this new turn, the timestamp advances.
                self._audio_playback_ends_at = time.time()
            elif etype == "response.output_audio.delta":
                # Track total μ-law bytes for THIS assistant turn so response.done
                # can predict when playback will actually finish in the caller's
                # ear (vs when xAI finishes generating — those are seconds apart).
                # base64 length / 4 * 3 = decoded bytes (close enough; padding
                # rounding loses at most 2 bytes per delta = inaudible).
                delta = event.get("delta", "")
                if delta:
                    decoded_bytes = (len(delta) * 3) // 4
                    self._assistant_audio_bytes_in_turn += decoded_bytes
                    # Advance the projected playback-end as audio arrives.
                    # 8 kHz μ-law = 8000 bytes/sec → bytes/8000 seconds.
                    # Used by send_function_output to wait for the prior
                    # turn's audio to play out before requesting the next
                    # response (per xAI docs best-practices: avoid audio
                    # overlap during tool calls).
                    self._audio_playback_ends_at += decoded_bytes / 8000.0
            elif etype == "response.function_call_arguments.done":
                if event.get("name") == "transferCall_v3":
                    self._mark_transfer_action_seen()
            elif etype == "response.done":
                self._response_in_progress = False
                # NB: do NOT clear _active_response_id immediately. xAI bursts
                # the entire response audio in ~1 s of WS frames, then fires
                # response.done — but the caller is still HEARING the assistant's
                # audio play out at 1x wall-clock from the browser/Twilio
                # buffer for several more seconds. If the caller barges in
                # during that playback window, the BargeInController's gate
                # would (wrongly) see "Aria is silent" and skip the clear.
                # Instead, schedule the clear at the projected end of audio
                # playback so the gate stays "active" exactly as long as the
                # caller is hearing Aria. Diagnosed from call uIvOEVDMs54
                # (2026-05-07): caller spoke at 7.75s while greeting audio
                # ran 4.5-13.5s; bridge thought Aria was silent at 7.75s
                # because response.done already fired at 5.7s.
                self._accumulate_usage(event)
                # Project last_activity_at forward to estimated playback-end
                # wall-clock. xAI bursts assistant audio bytes faster than 1×;
                # response.done fires while the caller still has seconds of
                # the assistant's audio queued. If we used time.time() here, the idle
                # watcher would start counting silence while Aria is audibly
                # still talking, and fire the "are you still there?" prompt
                # only ~4s after Aria finished from the caller's perspective.
                # 8 kHz μ-law = 8000 bytes/sec.
                audio_seconds = self._assistant_audio_bytes_in_turn / 8000.0
                self.last_activity_at = time.time() + audio_seconds
                self._assistant_audio_bytes_in_turn = 0
                # Defer clearing _active_response_id until projected playback
                # finishes — see the response.done comment block above for why.
                rid_to_clear = self._active_response_id
                if rid_to_clear is not None:
                    asyncio.create_task(
                        self._clear_active_response_id_after(audio_seconds, rid_to_clear)
                    )
                # Reset the consecutive-idle-prompts counter ONLY when this
                # response.done is for a normal (caller-driven) Aria turn.
                # If it's the response.done for an idle prompt that we just
                # injected, KEEP the counter so the "give up after 2 prompts"
                # guard in _idle_watcher actually trips — otherwise the assistant's
                # own "Are you still there?" speech zeroes the counter and
                # we'd loop forever asking idle prompts.
                if self._pending_idle_response:
                    self._pending_idle_response = False
                else:
                    self.idle_consecutive_prompts = 0
                # Flush the per-response_id buffer as one chronological turn.
                # Two responses can stream concurrently (server auto-response
                # racing an explicit response.create from us); the buffers
                # dict keeps their deltas separate so neither turn ends up
                # mangled like wUN0vYWH0Xg's t=171.59s entry. response.done
                # fires once per assistant turn (twice when a tool call
                # happens — first for tool-emitting turn, then for final-
                # answer turn). Empty buffers are skipped so the tool-
                # emitting turn doesn't add a blank entry.
                rid = (event.get("response") or {}).get("id") or "_unknown"
                chunks = self._assistant_buffers.pop(rid, None)
                if chunks:
                    text = "".join(chunks).strip()
                    if text:
                        turn = {
                            "role": "assistant",
                            "text": text,
                            "ts_ms": int((time.time() - self.started_at) * 1000),
                        }
                        self.transcript_turns.append(turn)
                        self._publish_turn(turn)
                        # Auto-end backstop. If Aria just spoke a closing
                        # line ("…Goodbye.") but the model skipped invoking
                        # end_call, schedule the teardown ourselves so the
                        # call doesn't sit open with a silent line. Idempotent
                        # via _auto_end_scheduled — two consecutive goodbye
                        # turns won't double-schedule.
                        if not self._auto_end_scheduled and _looks_like_closing(text):
                            self._auto_end_scheduled = True
                            log.info(
                                "xai.auto_end.scheduled",
                                call_id=self.call_id,
                                grace_s=AUTO_END_GRACE_SECONDS,
                                tail=text[-60:],
                            )
                            asyncio.create_task(self._auto_end_after_grace())
                        # Transfer-intent suppression for the idle watcher.
                        # Set once per call; once Aria has announced a
                        # transfer the watcher stops adding "Are you still
                        # there?" prompts on top.
                        if not self._transfer_intent_announced and _looks_like_transfer_intent(text):
                            self._transfer_intent_announced = True
                            is_voicemail_transfer = _looks_like_voicemail_transfer_intent(text)
                            log.info(
                                "xai.transfer_intent.announced",
                                call_id=self.call_id,
                                voicemail=is_voicemail_transfer,
                                tail=text[-80:],
                            )
                            self._schedule_transfer_backstop(voicemail=is_voicemail_transfer)
            elif etype in ("response.cancelled", "response.failed"):
                # Treat these the same as response.done from the watcher's
                # perspective: Aria is no longer mid-utterance, so the
                # in-progress flag clears. Discard any partial deltas — they
                # never reached the caller's ear.
                self._response_in_progress = False
                self._active_response_id = None
                # Audio playback also aborted at this moment (the bridge
                # already drained its playout queue via the clear before
                # the cancel) — reset the projection so the next
                # send_function_output doesn't sleep on a stale timestamp.
                self._audio_playback_ends_at = time.time()
                rid = (event.get("response") or {}).get("id") or "_unknown"
                self._assistant_buffers.pop(rid, None)
                log.info("xai.response_terminated", call_id=self.call_id, type=etype, response_id=rid)
            elif etype == "error":
                log.error("xai.error", call_id=self.call_id, error=event.get("error"))

            yield event

    # ------------------------------------------------------------------
    # Idle watcher — fires Vapi-style "Are you still there?" prompts
    # ------------------------------------------------------------------

    def _bump_activity(self) -> None:
        self.last_activity_at = time.time()
        self.idle_consecutive_prompts = 0  # caller is engaged; reset escalation

    def _publish_turn(self, turn: dict[str, Any]) -> None:
        """Push a flushed transcript turn onto the optional live queue.

        Uses `put_nowait` so the events() loop can't be back-pressured by a
        slow consumer — a stuck browser must not stall xAI event processing.
        QueueFull is logged and dropped: the turn is already in
        `transcript_turns` and will land in the DB at end-of-call.
        """
        if self.transcript_queue is None:
            return
        try:
            self.transcript_queue.put_nowait(turn)
        except asyncio.QueueFull:
            log.warning("xai.transcript_queue_full", call_id=self.call_id, role=turn.get("role"))

    def mark_end_source(self, source: str) -> None:
        """Record the first clear reason the bridge saw for call teardown."""
        if self.end_source is None:
            self.end_source = source

    def _mark_transfer_action_seen(self) -> None:
        """Record that xAI produced the transfer action for this call."""
        if not self._transfer_action_seen:
            log.info("xai.transfer_action.seen", call_id=self.call_id)
        self._transfer_action_seen = True
        current_task = asyncio.current_task()
        if (
            self._transfer_backstop_task is not None
            and not self._transfer_backstop_task.done()
            and self._transfer_backstop_task is not current_task
        ):
            self._transfer_backstop_task.cancel()
        self._transfer_backstop_task = None

    def _schedule_transfer_backstop(self, *, voicemail: bool) -> None:
        if self._transfer_action_seen or self._transfer_backstop_sent:
            return
        if self._transfer_backstop_task is not None and not self._transfer_backstop_task.done():
            return
        log.info(
            "xai.transfer_backstop.scheduled",
            call_id=self.call_id,
            delay_s=TRANSFER_BACKSTOP_DELAY_SECONDS,
            voicemail=voicemail,
        )
        self._transfer_backstop_task = asyncio.create_task(
            self._send_transfer_backstop_after_delay(voicemail=voicemail)
        )

    async def _send_transfer_backstop_after_delay(self, *, voicemail: bool) -> None:
        try:
            await asyncio.sleep(TRANSFER_BACKSTOP_DELAY_SECONDS)
            if self.ws is None or self.end_requested or self._transfer_action_seen:
                return

            # Wait briefly if the caller or Aria is mid-turn. The cue should
            # arrive during the silent waiting gap, not on top of live speech.
            for _ in range(6):
                if self.ws is None or self.end_requested or self._transfer_action_seen:
                    return
                if not self._speech_in_progress and not self._response_in_progress:
                    break
                await asyncio.sleep(1.0)
            else:
                log.info("xai.transfer_backstop.skipped_busy", call_id=self.call_id)
                self._transfer_intent_announced = False
                return

            if self.ws is None or self.end_requested or self._transfer_action_seen:
                return

            self._transfer_backstop_sent = True
            if voicemail and self.voicemail_transfer_backstop is not None:
                ok = await self.voicemail_transfer_backstop()
                if ok:
                    self._mark_transfer_action_seen()
                    log.info("xai.transfer_backstop.voicemail_forced", call_id=self.call_id)
                    return
                log.warning("xai.transfer_backstop.voicemail_force_failed", call_id=self.call_id)
            elif not voicemail and self.warm_transfer_backstop is not None:
                ok = await self.warm_transfer_backstop()
                if ok:
                    self._mark_transfer_action_seen()
                    log.info("xai.transfer_backstop.warm_forced", call_id=self.call_id)
                    return
                log.warning("xai.transfer_backstop.warm_force_failed", call_id=self.call_id)

            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": TRANSFER_BACKSTOP_CUE,
                    }],
                },
            }))
            await self.ws.send(json.dumps({"type": "response.create"}))
            log.info("xai.transfer_backstop.cue_sent", call_id=self.call_id)
            self._transfer_intent_announced = False
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("xai.transfer_backstop.failed", call_id=self.call_id)
        finally:
            if self._transfer_backstop_task is asyncio.current_task():
                self._transfer_backstop_task = None

    def _accumulate_usage(self, event: dict[str, Any]) -> None:
        """Sum token usage from a `response.done` event.

        xAI puts a `usage` object at BOTH the event top level AND nested under
        `response`. Empirically (2026-04-30 verification call) the nested one
        is an empty {} while the top-level one carries the actual token
        counts — the opposite of OpenAI Realtime. Pick whichever location has
        non-zero tokens; on first event log both shapes so we can confirm.
        """
        nested = event.get("response", {}).get("usage") if isinstance(event.get("response"), dict) else None
        top = event.get("usage")

        def _has_tokens(u: Any) -> bool:
            return isinstance(u, dict) and bool(u.get("input_tokens") or u.get("output_tokens") or u.get("total_tokens"))

        if _has_tokens(nested):
            usage = nested
            picked = "nested"
        elif _has_tokens(top):
            usage = top
            picked = "top"
        else:
            usage = nested if isinstance(nested, dict) else top
            picked = "none"

        if not self._logged_response_done_shape:
            log.info(
                "xai.response_done.shape",
                call_id=self.call_id,
                top_keys=sorted(event.keys()),
                response_keys=sorted((event.get("response") or {}).keys()) if isinstance(event.get("response"), dict) else None,
                nested_usage_keys=sorted(nested.keys()) if isinstance(nested, dict) else None,
                top_usage_keys=sorted(top.keys()) if isinstance(top, dict) else None,
                picked=picked,
            )
            self._logged_response_done_shape = True

        if not isinstance(usage, dict):
            return
        self.xai_input_tokens += int(usage.get("input_tokens") or 0)
        self.xai_output_tokens += int(usage.get("output_tokens") or 0)
        in_details = usage.get("input_token_details") or {}
        out_details = usage.get("output_token_details") or {}
        self.xai_input_audio_tokens += int(in_details.get("audio_tokens") or 0)
        self.xai_input_text_tokens += int(in_details.get("text_tokens") or 0)
        self.xai_output_audio_tokens += int(out_details.get("audio_tokens") or 0)
        self.xai_output_text_tokens += int(out_details.get("text_tokens") or 0)

    async def _idle_watcher(self) -> None:
        """Inject an idle prompt after IDLE_TIMEOUT_SECONDS of inactivity.

        After 2 consecutive idle prompts with no caller response, speak a
        brief goodbye and signal end_requested so the bridge tears the call
        down. Without this, the call would loop forever asking "are you
        still there?" even though the caller has clearly disengaged.

        Gates on `_speech_in_progress` and `_response_in_progress`. xAI VAD
        only emits speech_started/speech_stopped on sustained silence
        boundaries (>= silence_duration_ms = 500ms), so a caller talking for
        20 seconds straight without 500ms pauses produces no events to bump
        last_activity_at — the watcher would otherwise fire mid-sentence.
        And firing while Aria is generating a response creates a second
        concurrent response that interleaves at the delta level (call
        wUN0vYWH0Xg, t=171.59s).
        """
        try:
            while self.ws is not None:
                await asyncio.sleep(2.0)  # check every 2s
                if self.end_requested or self.ws is None:
                    return
                # Caller is mid-utterance — we know they're engaged even
                # though no per-tick events are firing to bump last_activity_at.
                if self._speech_in_progress:
                    self.last_activity_at = time.time()
                    continue
                # Aria is mid-response — firing now would race the in-flight
                # response and produce interleaved deltas.
                if self._response_in_progress:
                    self.last_activity_at = time.time()
                    continue
                # Transfer has been announced — caller is rightly silent
                # waiting to be bridged. Don't nag with "are you still
                # there?" prompts. If transferCall_v3 fires, the ws closes
                # and we exit this loop. If it doesn't fire, the call sits
                # quiet (better than today's stacked-idle-prompt failure
                # mode, diagnosed in Bb0TcMRAcL4 2026-05-12).
                if self._transfer_intent_announced:
                    self.last_activity_at = time.time()
                    continue
                idle_for = time.time() - self.last_activity_at
                if idle_for >= IDLE_TIMEOUT_SECONDS:
                    if self.idle_consecutive_prompts >= 2:
                        log.info("xai.idle.giving_up", call_id=self.call_id)
                        await self._send_idle_giveup()
                        return
                    await self._send_idle_prompt()
                    # Move on to the next message in the rotation
                    self.idle_message_idx = (self.idle_message_idx + 1) % len(IDLE_MESSAGES)
                    self.idle_consecutive_prompts += 1
                    self.last_activity_at = time.time()  # don't immediately re-fire
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("xai.idle_watcher_failed", call_id=self.call_id)

    async def _send_idle_prompt(self) -> None:
        if self.ws is None:
            return
        message = IDLE_MESSAGES[self.idle_message_idx]
        log.info("xai.idle.prompt", call_id=self.call_id, message=message)
        # Mark this so response.done knows the next assistant turn is the
        # idle prompt itself and should NOT reset idle_consecutive_prompts.
        # (If we reset, the "give up after 2 prompts" guard never trips.)
        self._pending_idle_response = True
        # The model historically embellished the idle line with extra "Take
        # your time—I'm happy to help" / follow-up offers. Reframe as a stage
        # direction with explicit negative constraints (no prefix, no suffix,
        # no follow-up question), and place the exact line on its own
        # paragraph so the model treats it as a script cue rather than a
        # paraphrasable sentiment.
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        f"[Silence cue — caller has been quiet for {IDLE_TIMEOUT_SECONDS}s. "
                        "Speak ONLY the following sentence verbatim, then stop and listen. "
                        "Do NOT add a greeting, do NOT offer further help, do NOT ask a "
                        "question, do NOT prefix or suffix it with anything else.]\n\n"
                        f"{message}"
                    ),
                }],
            },
        }))
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def inject_repeat_caller_context(self, prev: dict[str, Any]) -> None:
        """Inject previous-call context so Aria can reference the prior
        thread only after the caller confirms their name.

        Called once at session start, immediately after the Twilio `start`
        event surfaces caller_number and we've found a recent call via
        db.get_recent_call_by_number(). The note is sent as a
        `conversation.item.create` (role=user, type=input_text) so it lands
        in the assistant's conversation history before the greeting fires.

        Critical guard: the prompt rules tell Aria to ONLY reference the
        prior call AFTER the caller's stated first name matches the prior
        caller's first name. If they don't match (shared phone, forwarded
        call, new owner of recycled number) Aria treats it as a
        fresh call and never leaks the prior context.

        Safe to skip: if ws is None or the inject fails, the call proceeds
        as a normal fresh call.
        """
        if self.ws is None or not prev:
            return
        try:
            context = _build_repeat_caller_context(prev, self.caller_number)
            if context is None:
                return

            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": context.text}],
                },
            }))
            log.info(
                "xai.repeat_caller.injected",
                call_id=self.call_id,
                prev_call_id=prev.get("id"),
                prev_first_name=context.prev_first,
                days_ago=context.days_ago,
                disposition=context.disposition,
            )
        except Exception:  # noqa: BLE001
            # Non-fatal: a failure here just means the call proceeds without
            # repeat-caller continuity, which is the same as before this feature.
            log.exception("xai.repeat_caller.inject_failed", call_id=self.call_id)

    async def _clear_active_response_id_after(self, delay: float, rid: str) -> None:
        """Clear `_active_response_id` after the projected audio playback ends.

        Called from the `response.done` handler. xAI bursts assistant audio
        bytes faster than 1x wall-clock; response.done fires while the
        caller still has `delay` seconds of audio queued in the browser /
        Twilio buffer. The barge-in gate uses `_active_response_id` to
        decide whether a caller speech_started event is a true barge-in —
        which it IS during the playback window, even though xAI is done
        sending. Clearing immediately would cause the gate to drop those
        events and the caller would talk over Aria without her stopping.
        """
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # Only clear if it's still the same response (a NEW response.created
            # would have overwritten _active_response_id — don't trample that).
            if self._active_response_id == rid:
                self._active_response_id = None
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("xai.clear_active_response_id_failed", call_id=self.call_id)

    async def _auto_end_after_grace(self) -> None:
        """Wait for the goodbye audio to play out, then trip end_requested.

        Mirrors the 5 s post-goodbye delay used by `end_call.handle` and the
        deferred-end branch in `main.py`. Setting `end_requested = True` is
        enough to terminate the call on both paths: the playground bridge
        closes the browser WS, and the production bridge closes the Twilio
        media-stream WS — which causes the surrounding TwiML
        `<Connect><Stream/></Connect>` to complete and the PSTN call to end.
        """
        try:
            await asyncio.sleep(AUTO_END_GRACE_SECONDS)
            if self.end_requested:
                # Some other path (model finally invoked end_call, idle
                # giveup, etc.) already requested the end — nothing to do.
                return
            log.info("xai.auto_end.firing", call_id=self.call_id)
            self.mark_end_source("auto_end")
            self.end_requested = True
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("xai.auto_end.failed", call_id=self.call_id)

    async def _send_idle_giveup(self) -> None:
        """Speak a brief goodbye and tear the call down.

        Fires after `idle_consecutive_prompts >= 2`. Sends a stage-direction
        prompt asking the model to say a single goodbye line, waits for the
        audio to finish playing on the caller's end (estimated from the
        burst-byte projection in response.done), then sets end_requested
        so the bridge teardown path closes the underlying transport WS.
        """
        if self.ws is None:
            return
        log.info("xai.idle.goodbye", call_id=self.call_id)
        # Reset audio-bytes counter so response.done's projection is fresh.
        self._assistant_audio_bytes_in_turn = 0
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        "[Silence cue — caller has not responded after two prompts. "
                        "Speak ONLY the following sentence verbatim, then stop. "
                        "Do NOT add greetings, do NOT offer further help, do NOT "
                        "ask a question.]\n\n"
                        "It seems we got disconnected. Have a great day. Goodbye."
                    ),
                }],
            },
        }))
        await self.ws.send(json.dumps({"type": "response.create"}))

        # Wait long enough for the audio to actually play out on the caller's
        # side. xAI bursts assistant audio faster than 1× wall-clock; we let
        # `_assistant_audio_bytes_in_turn` accumulate during the response and
        # project the playback-end. 6s is a comfortable upper bound for the
        # short goodbye line above (~3s of speech) plus jitter buffer.
        await asyncio.sleep(6.0)

        log.info("xai.idle.end_requested", call_id=self.call_id)
        self.mark_end_source("idle_timeout")
        self.end_requested = True

    async def close(self) -> None:
        self.ended_at = time.time()
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._idle_task = None
        if self._transfer_backstop_task is not None:
            self._transfer_backstop_task.cancel()
            try:
                await self._transfer_backstop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._transfer_backstop_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("xai.close_failed", call_id=self.call_id, exc=str(exc))
            self.ws = None
        log.info(
            "xai.closed",
            call_id=self.call_id,
            duration_s=self.ended_at - self.started_at,
            interruptions=self.interruption_count,
        )

    @property
    def bot_transcript(self) -> str:
        return "".join(self.bot_transcript_chunks).strip()

    @property
    def caller_transcript(self) -> str:
        return " ".join(self.caller_transcript_chunks).strip()
