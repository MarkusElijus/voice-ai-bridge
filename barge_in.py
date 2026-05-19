"""Vapi-parity barge-in controller.

Vapi's `stopSpeakingPlan` exposes three knobs:
    voiceSeconds   — caller must speak for this long before assistant yields
    backoffSeconds — refractory window after a clear, ignore further VAD fires
    numWords       — caller must say this many words before assistant yields

We implement voiceSeconds and backoffSeconds. NumWords is deferred — xAI
realtime only emits a full caller transcript on
`conversation.item.input_audio_transcription.completed` (firing AFTER the
caller pauses), not word-level deltas during speech, so word-counting
during an in-progress turn isn't possible without a separate ASR layer.

Background: the previous implementation cleared the assistant's playback queue
the instant xAI's server VAD fired `input_audio_buffer.speech_started`.
A single 200 ms "okay" was enough to cut her off mid-word. This module
debounces that fire-cancel-fire pattern by scheduling the clear and
cancelling it if `speech_stopped` arrives before the threshold elapses.

Threading model: single asyncio loop per call. The pending-clear is an
asyncio.Task; we cancel it on `on_speech_stopped`. The backoff window is
a wall-clock check against `_last_clear_at`. No locks needed under the
single-threaded event loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from logging_config import log


class BargeInController:
    """Debounce + backoff for caller-driven playback clears.

    Args:
        voice_seconds:   how long caller speech must persist before we
                         actually clear playback. Higher = more patient.
        backoff_seconds: after a clear actually fires, ignore further
                         `on_speech_started` calls for this duration so
                         VAD oscillation can't re-clear.
        send_clear:      async callable that emits the clear frame to
                         the underlying transport (Twilio: `event:clear`,
                         playground: `{type:"clear"}`).
        call_id:         for log correlation.
        get_active_response_id:
                         callable returning the response_id Aria is
                         currently streaming, or None if she's silent.
                         The controller uses this to distinguish a true
                         barge-in (caller talks while Aria is speaking)
                         from xAI's auto-reply to a normal caller turn —
                         we only fire the clear+cancel for the FIRST case.
    """

    def __init__(
        self,
        *,
        voice_seconds: float,
        backoff_seconds: float,
        send_clear: Callable[[], Awaitable[None]],
        call_id: str,
        get_active_response_id: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._voice_seconds = max(0.0, float(voice_seconds))
        self._backoff_seconds = max(0.0, float(backoff_seconds))
        self._send_clear = send_clear
        self._call_id = call_id
        self._get_active_response_id = get_active_response_id
        self._pending: asyncio.Task[None] | None = None
        self._last_clear_at: float = 0.0
        # response_id Aria was streaming at the moment speech_started
        # fired. _maybe_clear re-checks against the current active id at
        # fire time; if they differ, xAI ended the original response and
        # started a new one (its reply to the caller's speech) — don't
        # cancel that. Captured-as-None means "Aria was silent at
        # speech_started", so we drop the barge-in entirely.
        self._target_response_id: str | None = None

    def on_speech_started(self) -> None:
        """xAI's `input_audio_buffer.speech_started` arrived.

        Schedule the playback clear. If a clear was already issued within
        the backoff window, drop this event (refractory). If a pending
        clear is already in flight (rare — duplicate VAD fire), do
        nothing rather than stack another timer. If Aria wasn't speaking
        at the moment of speech_started, this is a normal caller turn (not
        a barge-in) — drop and let xAI's auto-reply proceed undisturbed.
        """
        now = time.monotonic()
        if now - self._last_clear_at < self._backoff_seconds:
            return  # in backoff
        if self._pending is not None and not self._pending.done():
            return  # already armed
        target = self._get_active_response_id()
        if target is None:
            # Aria is silent. Caller speaking now is a normal turn-take,
            # not a barge-in. xAI will auto-reply via its server VAD; we
            # must NOT cancel that reply.
            log.info("barge_in.skipped_no_active_response", call_id=self._call_id)
            return
        self._target_response_id = target
        self._pending = asyncio.create_task(self._maybe_clear())

    def on_speech_stopped(self) -> None:
        """xAI's `input_audio_buffer.speech_stopped` arrived.

        If a clear was armed but hasn't fired yet, the caller's speech
        ended before reaching the voice_seconds threshold — cancel it.
        Aria keeps talking, no interruption. This is exactly the case
        where the caller said a brief acknowledgement ("okay", "uh-huh")
        and didn't actually mean to barge in.
        """
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            self._pending = None

    async def _maybe_clear(self) -> None:
        try:
            if self._voice_seconds > 0:
                await asyncio.sleep(self._voice_seconds)
            # Re-check that the response captured at speech_started is STILL
            # the one streaming. If xAI finished it and started a new one in
            # the debounce window, the new response is xAI's reply to the
            # caller's speech — cancelling it would silence the assistant's normal
            # turn. Diagnosed from call vFIqBTyEPeY (2026-05-07).
            current = self._get_active_response_id()
            if current != self._target_response_id:
                log.info(
                    "barge_in.skipped_response_changed",
                    call_id=self._call_id,
                    target=self._target_response_id,
                    current=current,
                )
                return
            await self._send_clear()
            self._last_clear_at = time.monotonic()
        except asyncio.CancelledError:
            # Caller paused before voice_seconds — do nothing, Aria keeps going.
            pass
        except Exception:  # noqa: BLE001
            # Don't let a transport failure (browser closed mid-clear, etc.)
            # crash the from_xai loop. Log and move on.
            log.exception("barge_in.send_clear_failed", call_id=self._call_id)
        finally:
            # Symmetric lifecycle: clear the handle so on_speech_stopped /
            # on_speech_started can see "no pending" cleanly. Without this,
            # _pending always points at the last completed task — harmless
            # GC-wise but obscures the state machine.
            self._pending = None
            self._target_response_id = None
