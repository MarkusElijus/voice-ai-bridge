from __future__ import annotations

import unittest

from recording import RecordingBuffer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingBufferTests(unittest.TestCase):
    def test_channels_are_anchored_to_elapsed_time_not_other_channel_length(self) -> None:
        clock = FakeClock()
        recording = RecordingBuffer(clock=clock)

        recording.append_assistant(b"\x7f" * 800)
        clock.now = 30.0
        recording.append_caller(b"\x7f" * 800)
        clock.now = 120.0
        recording.append_assistant(b"\x7f" * 800)

        self.assertGreaterEqual(recording.duration_seconds(), 120.0)
        self.assertGreaterEqual(recording.duration_seconds(min_duration_seconds=185.0), 185.0)


if __name__ == "__main__":
    unittest.main()
