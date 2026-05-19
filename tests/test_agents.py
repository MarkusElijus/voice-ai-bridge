from __future__ import annotations

import unittest
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

import db
from agents import (
    AGENT_ARIA,
    AGENT_ARIA_AFTER_HOURS,
    agent_for_datetime,
    get_agent_config,
    tool_defs_for_agent,
    validate_tool_call,
)


CT = ZoneInfo("America/Chicago")


def ct(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CT)


class BusinessHoursAgentSelectionTests(unittest.TestCase):
    def test_selects_daytime_during_monday_through_thursday_window(self) -> None:
        self.assertEqual(agent_for_datetime(ct(2026, 5, 18, 8, 0)), AGENT_ARIA)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 18, 16, 29)), AGENT_ARIA)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 19, 12, 0)), AGENT_ARIA)

    def test_selects_after_hours_at_monday_through_thursday_close(self) -> None:
        self.assertEqual(agent_for_datetime(ct(2026, 5, 18, 7, 59)), AGENT_ARIA_AFTER_HOURS)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 18, 16, 30)), AGENT_ARIA_AFTER_HOURS)

    def test_selects_daytime_for_short_friday_window(self) -> None:
        self.assertEqual(agent_for_datetime(ct(2026, 5, 22, 8, 0)), AGENT_ARIA)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 22, 13, 59)), AGENT_ARIA)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 22, 14, 0)), AGENT_ARIA_AFTER_HOURS)

    def test_friday_early_close_override_routes_after_hours_from_override_time(self) -> None:
        early_close = time(12, 0)

        self.assertEqual(
            agent_for_datetime(ct(2026, 5, 22, 11, 59), friday_close_time=early_close),
            AGENT_ARIA,
        )
        self.assertEqual(
            agent_for_datetime(ct(2026, 5, 22, 12, 0), friday_close_time=early_close),
            AGENT_ARIA_AFTER_HOURS,
        )
        self.assertEqual(
            agent_for_datetime(ct(2026, 5, 22, 13, 0), friday_close_time=early_close),
            AGENT_ARIA_AFTER_HOURS,
        )

    def test_friday_early_close_override_does_not_affect_other_weekdays(self) -> None:
        early_close = time(12, 0)

        self.assertEqual(
            agent_for_datetime(ct(2026, 5, 21, 13, 0), friday_close_time=early_close),
            AGENT_ARIA,
        )
        self.assertEqual(
            agent_for_datetime(ct(2026, 5, 23, 9, 0), friday_close_time=early_close),
            AGENT_ARIA_AFTER_HOURS,
        )

    def test_weekends_are_after_hours(self) -> None:
        self.assertEqual(agent_for_datetime(ct(2026, 5, 23, 9, 0)), AGENT_ARIA_AFTER_HOURS)
        self.assertEqual(agent_for_datetime(ct(2026, 5, 24, 15, 0)), AGENT_ARIA_AFTER_HOURS)


class AgentToolPolicyTests(unittest.TestCase):
    def test_after_hours_can_schedule_book_voicemail_and_end(self) -> None:
        config = get_agent_config(AGENT_ARIA_AFTER_HOURS)
        self.assertIn("hubspot_get_availability_v3", config.enabled_tools)
        self.assertIn("hubspot_book_meeting_v3", config.enabled_tools)
        self.assertIn("transferCall_v3", config.enabled_tools)
        self.assertIn("end_call", config.enabled_tools)

        voicemail_args = {"destination": "+15555550101", "reason": "voicemail"}
        self.assertIsNone(validate_tool_call(AGENT_ARIA_AFTER_HOURS, "transferCall_v3", voicemail_args))

    def test_after_hours_cannot_warm_transfer_or_send_sms_summary(self) -> None:
        warm_args = {
            "destination": "+15555550100",
            "reason": "warm_transfer_attorney",
            "summary": "Urgent real estate call.",
        }
        warm_error = validate_tool_call(AGENT_ARIA_AFTER_HOURS, "transferCall_v3", warm_args)
        sms_error = validate_tool_call(AGENT_ARIA_AFTER_HOURS, "send_sms_summary_openphone", {
            "to": "+15555550100",
            "content": "summary",
        })

        self.assertIsNotNone(warm_error)
        assert warm_error is not None
        self.assertEqual(warm_error["error"], "tool_not_allowed")
        self.assertIsNotNone(sms_error)
        assert sms_error is not None
        self.assertEqual(sms_error["error"], "tool_not_allowed")

    def test_after_hours_tool_defs_do_not_advertise_sms_summary(self) -> None:
        names = [tool["name"] for tool in tool_defs_for_agent(AGENT_ARIA_AFTER_HOURS)]
        self.assertNotIn("send_sms_summary_openphone", names)
        self.assertIn("hubspot_get_availability_v3", names)
        self.assertIn("hubspot_book_meeting_v3", names)
        self.assertIn("transferCall_v3", names)

    def test_after_hours_transfer_tool_advertises_voicemail_only(self) -> None:
        transfer = next(
            tool for tool in tool_defs_for_agent(AGENT_ARIA_AFTER_HOURS)
            if tool["name"] == "transferCall_v3"
        )
        destination = transfer["parameters"]["properties"]["destination"]
        reason = transfer["parameters"]["properties"]["reason"]

        self.assertEqual(destination["enum"], ["+15555550101"])
        self.assertEqual(reason["enum"], ["voicemail"])
        self.assertNotIn("attorney", transfer["description"].lower())
        self.assertNotIn("+15555550100", transfer["description"])


class PromptFallbackTests(unittest.TestCase):
    def test_daytime_prompt_is_slim_and_contains_core_rules(self) -> None:
        content = asyncio.run(db.get_active_prompt(AGENT_ARIA))

        self.assertLess(len(content), 18_000)
        for required in (
            "You are Aria",
            "Warm Transfer For Qualified Urgent Real Estate Calls",
            "Voicemail And Messages",
            "Initial consultations are free",
            "destination `+15555550100`",
            "destination `+15555550101`",
        ):
            self.assertIn(required, content)

    def test_after_hours_prompt_loads_from_registered_file_fallback(self) -> None:
        content = asyncio.run(db.get_active_prompt(AGENT_ARIA_AFTER_HOURS))

        self.assertIn("Our office is currently closed", content)
        self.assertIn("aria_after_hours", content)

    def test_after_hours_sms_link_fallback_collects_contact_one_field_at_a_time(self) -> None:
        content = asyncio.run(db.get_active_prompt(AGENT_ARIA_AFTER_HOURS))

        self.assertIn("What's the best number to text the scheduling link to?", content)
        self.assertIn("Do not ask for phone number and email in the same turn.", content)
        self.assertIn("After the text number is confirmed or declined", content)


if __name__ == "__main__":
    unittest.main()
