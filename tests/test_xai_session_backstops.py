from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

import xai_session
from xai_session import (
    XaiVoiceSession,
    _build_repeat_caller_context,
    _looks_like_transfer_intent,
    _looks_like_voicemail_transfer_intent,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        return None


class TransferIntentTests(unittest.TestCase):
    def test_detects_voicemail_handoff_lines(self) -> None:
        self.assertTrue(_looks_like_transfer_intent("Connecting you to voicemail now."))
        self.assertTrue(_looks_like_transfer_intent("Sending you to voicemail now."))
        self.assertTrue(_looks_like_transfer_intent("Putting you through to voicemail now."))

    def test_does_not_treat_normal_intake_connection_language_as_transfer(self) -> None:
        self.assertFalse(
            _looks_like_transfer_intent(
                "Thanks for reaching out, Katherine. I can help get you connected "
                "with the right person. Is this property located in [state]?"
            )
        )
        self.assertFalse(
            _looks_like_transfer_intent(
                "Thanks, Katherine. I'll help get this set up for you."
            )
        )

    def test_detects_real_warm_transfer_line(self) -> None:
        self.assertTrue(
            _looks_like_transfer_intent(
                "I understand this is time-sensitive. Let me connect you with one "
                "of our real estate attorneys right now."
            )
        )

    def test_identifies_voicemail_transfer_intent(self) -> None:
        self.assertTrue(_looks_like_voicemail_transfer_intent("Connecting you to voicemail now."))
        self.assertFalse(_looks_like_voicemail_transfer_intent("Let me connect you with an attorney now."))


class TransferBackstopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_delay = xai_session.TRANSFER_BACKSTOP_DELAY_SECONDS
        xai_session.TRANSFER_BACKSTOP_DELAY_SECONDS = 0.01

    def tearDown(self) -> None:
        xai_session.TRANSFER_BACKSTOP_DELAY_SECONDS = self.original_delay

    async def test_sends_internal_cue_for_non_voicemail_transfer(self) -> None:
        ws = FakeWebSocket()
        session = XaiVoiceSession(call_id="call-test", instructions="")
        session.ws = ws
        session._transfer_intent_announced = True

        session._schedule_transfer_backstop(voicemail=False)
        await asyncio.sleep(0.05)

        self.assertEqual([msg["type"] for msg in ws.sent], ["conversation.item.create", "response.create"])
        cue = ws.sent[0]["item"]["content"][0]["text"].lower()
        for forbidden in ("tool", "function", "function_call", "emit", "invoke"):
            self.assertNotIn(forbidden, cue)
        self.assertFalse(session._transfer_intent_announced)

    async def test_forces_warm_transfer_when_callback_is_available(self) -> None:
        ws = FakeWebSocket()
        forced = 0

        async def force_warm() -> bool:
            nonlocal forced
            forced += 1
            return True

        session = XaiVoiceSession(
            call_id="call-test",
            instructions="",
            warm_transfer_backstop=force_warm,
        )
        session.ws = ws

        session._schedule_transfer_backstop(voicemail=False)
        await asyncio.sleep(0.05)

        self.assertEqual(forced, 1)
        self.assertTrue(session._transfer_action_seen)
        self.assertEqual(ws.sent, [])

    async def test_forces_voicemail_transfer_when_callback_is_available(self) -> None:
        ws = FakeWebSocket()
        forced = 0

        async def force_voicemail() -> bool:
            nonlocal forced
            forced += 1
            return True

        session = XaiVoiceSession(
            call_id="call-test",
            instructions="",
            voicemail_transfer_backstop=force_voicemail,
        )
        session.ws = ws

        session._schedule_transfer_backstop(voicemail=True)
        await asyncio.sleep(0.05)

        self.assertEqual(forced, 1)
        self.assertTrue(session._transfer_action_seen)
        self.assertEqual(ws.sent, [])

    async def test_cancels_backstop_when_transfer_action_arrives(self) -> None:
        ws = FakeWebSocket()
        session = XaiVoiceSession(call_id="call-test", instructions="")
        session.ws = ws

        session._schedule_transfer_backstop(voicemail=True)
        session._mark_transfer_action_seen()
        await asyncio.sleep(0.05)

        self.assertEqual(ws.sent, [])


class RepeatCallerContextTests(unittest.TestCase):
    def test_context_note_avoids_meta_language_and_phone_number(self) -> None:
        now = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)
        prev = {
            "id": "prior-call",
            "first_name": "Brad",
            "last_name": "Parker",
            "started_at": now - timedelta(days=1),
            "disposition": "transferred_voicemail",
            "service_type": "real estate closing",
            "caller_status": "seller",
            "call_outcome": "left voicemail",
        }

        context = _build_repeat_caller_context(prev, "+15555550100", now=now)

        self.assertIsNotNone(context)
        assert context is not None
        text = context.text.lower()
        self.assertIn("hi again, brad!", text)
        self.assertNotIn("+15555550100", context.text)
        for forbidden in (
            "stage direction",
            "stage-direction",
            "system-injected",
            "lookup",
            "instructions",
            "internal context",
            "compare",
            "caller id",
            "tool",
            "function",
            "function_call",
            "emit",
            "invoke",
        ):
            self.assertNotIn(forbidden, text)

    def test_message_follow_up_does_not_push_booking(self) -> None:
        now = datetime(2026, 5, 14, 21, 5, tzinfo=timezone.utc)
        prev = {
            "id": "prior-call",
            "first_name": "Aaron",
            "last_name": "Hubbard",
            "started_at": now - timedelta(hours=1),
            "disposition": "info_only",
            "service_type": "Title Opinion",
            "caller_status": "Attorney",
            "call_outcome": "Left Message",
            "forward_msg_to": "[Attorney Name]",
        }

        context = _build_repeat_caller_context(prev, "+15555550100", now=now)

        self.assertIsNotNone(context)
        assert context is not None
        text = context.text
        self.assertIn("called earlier about title opinion and reaching [Attorney Name]", text)
        self.assertIn("Message recipient: [Attorney Name]", text)
        self.assertNotIn("Would you like to set up that consultation today?", text)
        self.assertNotIn("+15555550100", text)


if __name__ == "__main__":
    unittest.main()
