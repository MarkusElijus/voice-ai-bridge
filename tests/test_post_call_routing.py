from __future__ import annotations

import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from agents import AGENT_ARIA, AGENT_ARIA_AFTER_HOURS
from post_call import _infer_disposition, _is_after_hours
from xai_session import ToolCallRecord, XaiVoiceSession


CT = ZoneInfo("America/Chicago")


def epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=CT).timestamp()


class PostCallAfterHoursRoutingTests(unittest.TestCase):
    def test_after_hours_report_flag_can_follow_selected_agent(self) -> None:
        friday_1pm = epoch(2026, 5, 22, 13, 0)

        self.assertFalse(_is_after_hours(friday_1pm, agent_id=AGENT_ARIA))
        self.assertTrue(_is_after_hours(friday_1pm, agent_id=AGENT_ARIA_AFTER_HOURS))

    def test_after_hours_report_flag_uses_friday_override_when_agent_not_available(self) -> None:
        friday_noon = epoch(2026, 5, 22, 12, 0)

        self.assertTrue(_is_after_hours(friday_noon, friday_close_time=time(12, 0)))


class PostCallDispositionTests(unittest.TestCase):
    def test_availability_without_booking_is_appointment_offered_no_response(self) -> None:
        session = XaiVoiceSession(call_id="call-test", instructions="")
        session.bot_transcript_chunks.append(
            "The soonest available appointment is Wednesday at 9:30 AM. Does that work for you?"
        )
        session.tool_calls.append(
            ToolCallRecord(
                call_id="tool-call",
                name="hubspot_get_availability_v3",
                args={},
                output={"error": False, "options": []},
            )
        )

        self.assertEqual(_infer_disposition(session), "appointment_offered_no_response")


if __name__ == "__main__":
    unittest.main()
