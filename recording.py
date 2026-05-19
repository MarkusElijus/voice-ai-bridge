"""Per-call audio recording: buffer caller + assistant audio, emit stereo WAV.

Both Twilio and xAI use μ-law / 8 kHz / 1 channel for telephony audio. We
keep two parallel byte buffers (one per speaker) — appended to as audio
arrives, sample-aligned by clock time at end of call. The final artifact is
a 2-channel WAV with caller on the **left** channel and Aria on the **right**,
matching how Vapi presents stereo recordings.

Memory cost is modest: μ-law is 1 byte per sample, 8 kHz = 8 KB/s per
channel = ~480 KB / minute / channel. A 10-minute call holds ~10 MB across
both channels, well under the 1 GB Fly VM ceiling.

Why μ-law passthrough: Twilio sends μ-law and xAI accepts/emits μ-law
natively — no transcoding occurs in the bridge. We only convert to 16-bit
PCM at the very end so that the resulting WAV is universally playable
(some browsers don't render μ-law-coded WAV).
"""

from __future__ import annotations

import array
import audioop
import io
import time
import wave
from dataclasses import dataclass, field
from typing import Callable

# Both Twilio and xAI use 8 kHz μ-law for the call leg. If that ever
# changes (e.g. xAI adds 16 kHz PCM), update SAMPLE_RATE + the decode
# step in finalize().
SAMPLE_RATE = 8000


@dataclass
class RecordingBuffer:
    """In-memory μ-law audio buffer for a single call, two channels.

    The bridge appends to `caller_ulaw` whenever Twilio sends inbound media
    and to `assistant_ulaw` whenever xAI emits `response.output_audio.delta`.

    **Wall-clock alignment.** xAI bursts a turn's audio bytes much faster
    than 1x, while callers can be silent for long stretches. We therefore
    pad each channel to the elapsed monotonic call time before appending new
    bytes. This keeps the assistant's responses and the caller's later utterances at
    the positions where they happened, even when one side sends no audio for
    a while.

    Pad-on-finalize is still applied as a safety net for the residual
    asymmetry at end-of-call (e.g. caller frames continuing to arrive after
    the assistant's last delta).
    """

    clock: Callable[[], float] = time.monotonic
    caller_ulaw: bytearray = field(default_factory=bytearray)
    assistant_ulaw: bytearray = field(default_factory=bytearray)
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    def _elapsed_bytes(self) -> int:
        return max(0, int((self.clock() - self._started_at) * SAMPLE_RATE))

    @staticmethod
    def _pad_to(channel: bytearray, target_len: int) -> None:
        if len(channel) < target_len:
            channel.extend(b"\xff" * (target_len - len(channel)))

    def append_caller(self, ulaw_bytes: bytes) -> None:
        if ulaw_bytes:
            self._pad_to(self.caller_ulaw, self._elapsed_bytes())
            self.caller_ulaw.extend(ulaw_bytes)

    def append_assistant(self, ulaw_bytes: bytes) -> None:
        if not ulaw_bytes:
            return
        self._pad_to(self.assistant_ulaw, self._elapsed_bytes())
        self.assistant_ulaw.extend(ulaw_bytes)

    def is_empty(self) -> bool:
        return not self.caller_ulaw and not self.assistant_ulaw

    def duration_seconds(self, *, min_duration_seconds: float | None = None) -> float:
        n = max(len(self.caller_ulaw), len(self.assistant_ulaw))
        if min_duration_seconds is not None:
            n = max(n, int(min_duration_seconds * SAMPLE_RATE))
        return n / SAMPLE_RATE  # 1 byte per μ-law sample at 8 kHz

    def finalize(self, *, min_duration_seconds: float | None = None) -> bytes:
        """Produce a stereo 16-bit PCM WAV (caller=L, assistant=R).

        Steps:
          1. Pad shorter channel with μ-law silence (0xFF) so both buffers
             are equal length. WAV interleaves samples per-frame, so a
             length mismatch would shift one speaker forward in time.
          2. Decode each μ-law buffer to 16-bit signed PCM via
             stdlib `audioop.ulaw2lin`. (audioop is deprecated in 3.13;
             when we move past 3.12 we'll swap to the `audioop-lts`
             backport — the API is identical.)
          3. Interleave L/R samples (2 bytes per sample × 2 channels =
             4 bytes per frame) and write a WAV header.
        """
        n = max(len(self.caller_ulaw), len(self.assistant_ulaw))
        if min_duration_seconds is not None:
            n = max(n, int(min_duration_seconds * SAMPLE_RATE))
        if n == 0:
            return b""

        caller = bytes(self.caller_ulaw).ljust(n, b"\xff")
        assistant = bytes(self.assistant_ulaw).ljust(n, b"\xff")

        # μ-law (1 byte/sample) -> 16-bit signed PCM (2 bytes/sample)
        caller_pcm = audioop.ulaw2lin(caller, 2)
        assistant_pcm = audioop.ulaw2lin(assistant, 2)

        # Interleave L/R via array module slice-assign (compiled C, ~100x
        # faster than a Python for-loop over each sample). 'h' = signed
        # 16-bit, native byte order (matches what audioop.ulaw2lin emits
        # and what wave.writeframes expects).
        l_arr = array.array("h")
        l_arr.frombytes(caller_pcm)
        r_arr = array.array("h")
        r_arr.frombytes(assistant_pcm)
        stereo = array.array("h", b"\x00\x00" * (2 * len(l_arr)))
        stereo[0::2] = l_arr  # left channel = caller
        stereo[1::2] = r_arr  # right channel = Aria

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(stereo.tobytes())
        return buf.getvalue()
