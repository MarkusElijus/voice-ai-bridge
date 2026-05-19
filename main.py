"""FastAPI entry point for the Aria bridge (Vapi-Aria replacement).

Endpoints:
  POST /twiml                    - Twilio webhook for inbound voice; returns <Connect><Stream>
  WS   /media-stream/{call_id}   - Twilio Media Stream <-> xAI realtime bridge
  GET  /health                   - liveness probe
  POST /session                  - (optional) ephemeral xAI token for browser clients

Run locally:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from xml.sax.saxutils import quoteattr

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from twilio.request_validator import RequestValidator

from agents import (
    AGENT_ARIA,
    CT,
    agent_for_datetime,
    get_agent_config,
    normalize_agent_id,
    tool_defs_for_agent,
    validate_tool_call,
)
from admin.auth import verify_admin_basic
from admin.router import router as admin_router
from barge_in import BargeInController
import db
import dev_dns
import post_call
from logging_config import configure_logging, log
from settings import settings
from tools import dispatch_tool
from xai_session import ToolCallRecord, XaiVoiceSession


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL)
    log.info("startup", env=settings.ENV, port=settings.PORT, model=settings.XAI_MODEL)
    # Auto-applies a Cloudflare DNS override only for hosts the running loop's
    # resolver can't reach (e.g. api.hubapi.com behind Mullvad VPN content
    # filtering). No-op on Fly.io / unfiltered DNS — production unchanged.
    await dev_dns.apply_if_needed()
    await db.init_pool()
    yield
    await db.close_pool()
    log.info("shutdown")


app = FastAPI(lifespan=lifespan)
twilio_validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)

# Cap concurrent in-browser playground voice sessions across the whole process.
# Auth alone provides per-attempt brake (bcrypt ~100 ms), but once authenticated
# an attacker (or a single curious admin opening many tabs) can burn xAI credits
# and exhaust the bridge by holding many sockets open in parallel. Three is the
# expected concurrent ceiling: operator + tester + one tester. New attempts beyond
# that get a clean 4429 close. Acquire happens AFTER auth + Origin checks so
# rejected attempts don't consume slots.
_VOICE_PG_MAX_CONCURRENT = 3
_voice_pg_semaphore = asyncio.Semaphore(_VOICE_PG_MAX_CONCURRENT)

_TRANSFER_SUMMARY_MAX_CHARS = 420

# Admin dashboard at /admin/*
app.include_router(admin_router)
app.mount(
    "/admin/static",
    StaticFiles(directory=str(Path(__file__).parent / "admin" / "static")),
    name="admin_static",
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "env": settings.ENV,
        "model": settings.XAI_MODEL,
        "xai_api_key": "configured" if settings.XAI_API_KEY else "missing",
    }


# ---------------------------------------------------------------------------
# Twilio inbound
# ---------------------------------------------------------------------------

@app.post("/twiml")
async def twiml(request: Request) -> Response:
    if settings.TWILIO_VALIDATE_SIGNATURE:
        await _validate_twilio_signature(request)

    # Twilio's Media Stream `start` event does NOT include From/To natively;
    # they have to be propagated via <Parameter> children of <Stream>, which
    # surface as `start.customParameters.{name}` on the WS. Without this the
    # bridge stored caller_number=NULL on every inbound call — diagnosed from
    # boss test `dMONAtJFme0` 2026-05-12, which broke Make.com's SMS-meeting-
    # link branch (Claude SMS-picker received empty inputs, returned prose,
    # ParseJSON failed). Read From/To from the form Twilio POSTs to /twiml.
    form = await request.form()
    from_number = (form.get("From") or "").strip()
    to_number = (form.get("To") or "").strip()

    call_id = secrets.token_urlsafe(8)
    now_ct = datetime.now(CT)
    friday_override = await db.get_friday_early_close_override(now_ct.date())
    friday_close_time = friday_override.close_time if friday_override else None
    agent_id = agent_for_datetime(now_ct, friday_close_time=friday_close_time)
    agent_config = get_agent_config(agent_id)
    hostname = settings.HOSTNAME.replace("https://", "").replace("http://", "").rstrip("/")
    stream_url = f"wss://{hostname}/media-stream/{agent_id}/{call_id}"

    # quoteattr handles XML-escaping the value AND wraps it in quotes.
    twiml_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Connect>'
        f'<Stream url="{stream_url}">'
        f'<Parameter name="from" value={quoteattr(from_number)}/>'
        f'<Parameter name="to" value={quoteattr(to_number)}/>'
        f'<Parameter name="agent_id" value={quoteattr(agent_id)}/>'
        '</Stream></Connect></Response>'
    )
    log.info(
        "twiml.served",
        call_id=call_id,
        agent_id=agent_id,
        agent_label=agent_config.label,
        stream_url=stream_url,
        from_number=from_number or None,
        to_number=to_number or None,
        friday_early_close=friday_close_time.isoformat(timespec="minutes") if friday_close_time else None,
    )
    return Response(content=twiml_xml, media_type="application/xml")


@app.post("/whisper/{call_id}")
async def whisper(request: Request, call_id: str) -> Response:
    """Twilio fetches this when the attorney's phone is answered during a
    warm transfer. The returned TwiML plays a spoken summary TO THE ATTORNEY
    only, then Twilio bridges the inbound caller in (per `answerOnBridge=true`
    on the parent <Dial> in transferCall_v3).

    Signed by Twilio when TWILIO_VALIDATE_SIGNATURE is true. The summary is
    looked up from calls.transfer_summary which transferCall_v3 wrote before
    issuing the call.update — DB-backed so it survives cross-machine LB
    routing (Twilio may hit a different Fly machine than the one that owned
    the original call).
    """
    if settings.TWILIO_VALIDATE_SIGNATURE:
        await _validate_twilio_signature(request)

    row = await db.get_call(call_id)
    summary = (row or {}).get("transfer_summary") if row else None
    summary = (summary or "").strip()

    if summary:
        say_text = (
            "Incoming urgent call from a real estate client. Summary: "
            f"{summary}. Connecting now."
        )
    else:
        say_text = (
            "Incoming call from a real estate client. Connecting now."
        )

    # Escape for XML embedding inside <Say>. quoteattr is for attribute
    # values; for element text we want xml.sax.saxutils.escape, which is
    # available because quoteattr imports it under the hood, but to be
    # explicit we use it directly.
    from xml.sax.saxutils import escape as xml_escape
    twiml_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="Polly.Joanna">{xml_escape(say_text)}</Say>'
        '</Response>'
    )
    log.info(
        "whisper.served",
        call_id=call_id,
        summary_present=bool(summary),
        summary_chars=len(summary),
    )
    return Response(content=twiml_xml, media_type="application/xml")


async def _validate_twilio_signature(request: Request) -> None:
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if not twilio_validator.validate(url, params, signature):
        log.warning("twilio.bad_signature", url=url)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


# ---------------------------------------------------------------------------
# Twilio Media Stream <-> xAI bridge
# ---------------------------------------------------------------------------

@app.websocket("/media-stream/{call_id}")
@app.websocket("/media-stream/{agent_id}/{call_id}")
async def media_stream(ws: WebSocket, call_id: str, agent_id: str = AGENT_ARIA) -> None:
    await ws.accept()
    agent_id = normalize_agent_id(agent_id)
    agent_config = get_agent_config(agent_id)
    log.info("twilio.ws_accepted", call_id=call_id, agent_id=agent_id, agent_label=agent_config.label)

    instructions = await _load_instructions(agent_id)
    session = XaiVoiceSession(
        call_id=call_id,
        instructions=instructions,
        agent_id=agent_id,
        tool_defs=tool_defs_for_agent(agent_id),
    )

    try:
        await session.connect()
    except Exception:  # noqa: BLE001
        log.exception("xai.connect_failed", call_id=call_id)
        await ws.close(code=1011)
        return

    call_sid: str | None = None
    start_ready = asyncio.Event()
    greeted = False
    greet_lock = asyncio.Lock()

    async def maybe_greet() -> None:
        """Send the opening only after xAI and Twilio start context are ready."""
        nonlocal greeted
        if greeted or not session.session_ready or not start_ready.is_set():
            return
        async with greet_lock:
            if greeted or not session.session_ready or not start_ready.is_set():
                return
            await session.greet()
            greeted = True
            log.info("xai.greeting_sent", call_id=call_id)

    def _fallback_warm_transfer_summary() -> str:
        caller_turns = [
            (turn.get("text") or "").strip()
            for turn in session.transcript_turns
            if turn.get("role") == "caller" and (turn.get("text") or "").strip()
        ]
        details = " ".join(caller_turns)
        if details:
            summary = (
                "Urgent real estate call. Caller details gathered so far: "
                f"{details}"
            )
        else:
            summary = "Urgent real estate caller requested an attorney transfer."
        summary = " ".join(summary.split())
        if len(summary) > _TRANSFER_SUMMARY_MAX_CHARS:
            summary = summary[: _TRANSFER_SUMMARY_MAX_CHARS - 3].rstrip() + "..."
        return summary

    async def _force_transfer(*, destination: str, reason: str, summary: str | None = None) -> bool:
        if not call_sid:
            log.warning("transfer_backstop.missing_call_sid", call_id=call_id, reason=reason)
            return False
        args = {
            "destination": destination,
            "reason": reason,
        }
        if summary:
            args["summary"] = summary
        record = ToolCallRecord(
            call_id=f"bridge-{reason}-backstop",
            name="transferCall_v3",
            args=args,
        )
        session.tool_calls.append(record)
        log.info(
            "transfer_backstop.dispatching",
            call_id=call_id,
            destination=destination,
            reason=reason,
            summary_chars=len(summary or ""),
        )
        output = await dispatch_tool(
            "transferCall_v3",
            json.dumps(args),
            call_sid=call_sid,
            call_id=call_id,
        )
        record.output = output
        raw_err = output.get("error") if isinstance(output, dict) else None
        record.error = raw_err if isinstance(raw_err, str) and raw_err else None
        record.finished_at = time.time()
        ok = (
            isinstance(output, dict)
            and output.get("status") == "transferring"
            and record.error is None
        )
        try:
            await db.insert_tool_call(
                call_id=call_id,
                name=record.name,
                args=record.args,
                output=record.output,
                error=record.error,
                started_at=record.started_at,
                finished_at=record.finished_at,
            )
        except Exception:  # noqa: BLE001
            log.exception("transfer_backstop.tool_call_persist_failed", call_id=call_id, reason=reason)
        if ok:
            log.info("transfer_backstop.dispatched", call_id=call_id, reason=reason)
        else:
            log.warning("transfer_backstop.dispatch_failed", call_id=call_id, reason=reason, output=output)
        return ok

    async def force_voicemail_transfer() -> bool:
        """Deterministic fallback for voicemail transfers xAI only narrates."""
        return await _force_transfer(
            destination=settings.VOICEMAIL_NUMBER,
            reason="voicemail",
        )

    async def force_warm_transfer() -> bool:
        """Deterministic fallback for warm transfers xAI only narrates."""
        return await _force_transfer(
            destination=settings.ATTORNEY_TRANSFER_NUMBER,
            reason="warm_transfer_attorney",
            summary=_fallback_warm_transfer_summary(),
        )

    session.voicemail_transfer_backstop = force_voicemail_transfer
    if agent_id == AGENT_ARIA:
        session.warm_transfer_backstop = force_warm_transfer

    async def from_twilio() -> None:
        nonlocal call_sid
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "start":
                    session.stream_sid = msg["start"]["streamSid"]
                    call_sid = msg["start"].get("callSid")
                    custom = msg["start"].get("customParameters", {}) or {}
                    session.caller_number = (custom.get("from") or "").strip() or None
                    to_number = (custom.get("to") or "").strip() or None
                    log.info(
                        "twilio.start",
                        call_id=call_id,
                        agent_id=agent_id,
                        stream_sid=session.stream_sid,
                        call_sid=call_sid,
                        caller_number=session.caller_number,
                        to_number=to_number,
                    )
                    # Persist the call row so it shows up in the dashboard immediately.
                    await db.insert_call_started(
                        call_id=call_id,
                        call_sid=call_sid,
                        caller_number=session.caller_number,
                        agent_id=agent_id,
                    )
                    # Repeat-caller continuity: silent lookup of the most recent
                    # prior call from this number. If found, inject quiet
                    # background context so Aria can reference the prior
                    # thread AFTER the caller introduces themselves with a
                    # matching name. The greeting is gated on this lookup
                    # finishing so the opening response cannot race ahead of
                    # the continuity note.
                    try:
                        if session.caller_number:
                            prev = await db.get_recent_call_by_number(
                                session.caller_number,
                                exclude_id=call_id,
                                days=30,
                            )
                            if prev:
                                await session.inject_repeat_caller_context(prev)
                    except Exception:  # noqa: BLE001
                        log.exception("repeat_caller.lookup_failed", call_id=call_id)
                    finally:
                        start_ready.set()
                        await maybe_greet()
                elif event == "media":
                    if msg["media"].get("track") == "inbound":
                        payload_b64 = msg["media"]["payload"]
                        await session.forward_caller_audio(payload_b64)
                        # Tee caller audio into the recording buffer for the
                        # stereo WAV we upload at end of call. Decoding errors
                        # are non-fatal — Twilio always sends valid base64
                        # μ-law per Media Streams spec.
                        try:
                            session.recording.append_caller(base64.b64decode(payload_b64))
                        except Exception:  # noqa: BLE001
                            pass
                elif event == "stop":
                    log.info("twilio.stop", call_id=call_id)
                    session.mark_end_source("caller")
                    # Caller hung up before Aria signaled end_call. Without
                    # this teardown, from_xai keeps blocking on `async for
                    # event in session.events()` (the xAI WS, separate from
                    # the Twilio WS) so gather() never returns and
                    # post_call.run never fires — call sits at
                    # disposition='in_progress' with no transcript / no
                    # recording / no after-call report.
                    #
                    # Diagnosed from yZXFDej6gpM 2026-05-12: boss hung up
                    # after Aria failed to invoke transferCall_v3; Twilio
                    # sent stop; from_twilio exited cleanly; the entire
                    # post-call pipeline was lost because from_xai never
                    # woke up. Same orphaned-row pattern as bSl5HIQl4w0
                    # but caused by caller-hangup-no-goodbye rather than
                    # deploy interruption.
                    #
                    # Fix: set end_requested so the next from_xai iteration
                    # detects it AND close the xAI WS so the iterator
                    # raises and exits even if xAI is currently idle (no
                    # events streaming). session.close() called later in
                    # the parent function (line ~415) is idempotent.
                    session.end_requested = True
                    if session.ws is not None:
                        try:
                            await session.ws.close()
                        except Exception:  # noqa: BLE001
                            pass
                    break
                elif event == "mark":
                    pass  # ignore for now
        except WebSocketDisconnect:
            log.info("twilio.ws_disconnect", call_id=call_id)
            session.mark_end_source("caller")
        except RuntimeError as exc:
            # `ws.close()` in from_xai (after end_requested / idle-give-up /
            # auto-end backstop) races this loop's `ws.receive_text()`.
            # Starlette transitions the WebSocket to DISCONNECTED without
            # delivering a normal close message, and `receive_text` raises
            # RuntimeError('WebSocket is not connected...') instead of
            # WebSocketDisconnect. If we don't catch it here the exception
            # propagates through asyncio.gather(return_exceptions=False),
            # which means session.close() AND post_call.run() are skipped —
            # the call lands in the DB stuck at disposition='in_progress'
            # with NULL transcript and no recording upload. Diagnosed from
            # call -bsAn5l8hII (2026-05-11): auto-end fired correctly, but
            # the entire post-call pipeline was lost to this race.
            log.info(
                "twilio.ws_disconnect.via_close",
                call_id=call_id,
                detail=str(exc)[:160],
            )
            session.mark_end_source("caller")

    async def _twilio_send_clear() -> None:
        # 1) Drain the Twilio Media Stream playback queue so the caller stops
        #    hearing Aria mid-utterance.
        if session.stream_sid:
            await ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": session.stream_sid,
            }))
        # 2) Tell xAI to stop generating. Without this, xAI keeps bursting
        #    the rest of the assistant's response down the WS — Twilio re-fills its
        #    own playout buffer and the caller hears Aria continue for
        #    several more seconds. See cancel_response docstring for the
        #    diagnosis and rationale.
        await session.cancel_response()

    twilio_barge_in = BargeInController(
        voice_seconds=settings.BARGE_IN_VOICE_SECONDS,
        backoff_seconds=settings.BARGE_IN_BACKOFF_SECONDS,
        send_clear=_twilio_send_clear,
        call_id=call_id,
        get_active_response_id=lambda: session._active_response_id,
    )

    async def from_xai() -> None:
        try:
            async for event in session.events():
                etype = event.get("type")

                # Once session and Twilio start context are both ready, send
                # the inbound greeting exactly once.
                await maybe_greet()

                if etype == "response.output_audio.delta":
                    delta = event.get("delta")
                    if delta:
                        # Tee the assistant's audio into the recording buffer.
                        try:
                            session.recording.append_assistant(base64.b64decode(delta))
                        except Exception:  # noqa: BLE001
                            pass
                        if session.stream_sid:
                            await ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": session.stream_sid,
                                "media": {"payload": delta},
                            }))

                elif etype == "input_audio_buffer.speech_started":
                    # Vapi-parity stopSpeakingPlan — debounce the playback
                    # clear by BARGE_IN_VOICE_SECONDS. A short "uh-huh" /
                    # "okay" that ends before the threshold won't barge in.
                    twilio_barge_in.on_speech_started()

                elif etype == "input_audio_buffer.speech_stopped":
                    twilio_barge_in.on_speech_stopped()

                elif etype == "response.function_call_arguments.done":
                    name = event.get("name")
                    fc_call_id = event.get("call_id")
                    args_json = event.get("arguments", "{}")
                    log.info("xai.function_call", call_id=call_id, name=name, fc_call_id=fc_call_id)
                    record = ToolCallRecord(call_id=fc_call_id or "", name=name or "", args=json.loads(args_json or "{}"))
                    session.tool_calls.append(record)
                    policy_error = validate_tool_call(agent_id, name or "", record.args)
                    output = policy_error or await dispatch_tool(name or "", args_json, call_sid=call_sid or "", call_id=call_id)
                    record.output = output
                    # Some tools (e.g. hubspot_get_availability) use {"error": False}
                    # to signal success — store only string error codes; coerce
                    # bool/None/missing to None so the text column doesn't reject
                    # bool values (asyncpg.exceptions.DataError).
                    raw_err = output.get("error") if isinstance(output, dict) else None
                    record.error = raw_err if isinstance(raw_err, str) and raw_err else None
                    record.finished_at = time.time()
                    # Stream tool call into Postgres so the dashboard sees it live.
                    await db.insert_tool_call(
                        call_id=call_id,
                        name=record.name,
                        args=record.args,
                        output=record.output,
                        error=record.error,
                        started_at=record.started_at,
                        finished_at=record.finished_at,
                    )
                    if name == "end_call":
                        session.mark_end_source("agent_end_call")
                        # Don't send function_output back to xAI for end_call.
                        # If we do, xAI will generate ANOTHER assistant turn
                        # in response (e.g. "Thank you for calling") and the
                        # model can keep yapping past the goodbye. The function
                        # itself already scheduled the delayed Twilio hangup;
                        # we additionally defer end_requested by 5s here so the
                        # WS-close path is in sync with the caller's audio
                        # finishing on the line.
                        async def _defer_end_requested() -> None:
                            await asyncio.sleep(5.0)
                            session.end_requested = True
                        asyncio.create_task(_defer_end_requested())
                    elif (
                        name == "transferCall_v3"
                        and isinstance(output, dict)
                        and output.get("status") == "transferring"
                        and record.error is None
                    ):
                        session.mark_end_source("transfer")
                        # The transfer updates the live Twilio call and the
                        # Media Stream detaches immediately. Sending a result
                        # back to xAI races the clean WS close and can log a
                        # noisy ConnectionClosedOK even though the transfer
                        # succeeded.
                        pass
                    elif fc_call_id:
                        await session.send_function_output(fc_call_id, output)

                # Idle give-up tear-down. The watcher sets end_requested
                # after speaking goodbye + sleeping long enough for the
                # audio to play out. We close the Twilio WS here so
                # from_twilio exits its receive loop and gather() can
                # complete -> post_call.run runs.
                if session.end_requested:
                    try:
                        await ws.close(code=1000)
                    except Exception:  # noqa: BLE001
                        pass
                    return
        except Exception:  # noqa: BLE001
            log.exception("xai.event_loop_failed", call_id=call_id)

    await asyncio.gather(from_twilio(), from_xai(), return_exceptions=False)
    await session.close()
    await post_call.run(call_id=call_id, session=session)
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Voice Playground — browser <-> bridge <-> xAI (no Twilio in the loop)
# ---------------------------------------------------------------------------

def _ws_origin_ok(ws: WebSocket) -> bool:
    """CSRF defense for the playground WS upgrade.

    Mirrors `admin.router._require_same_origin` (the HTTP version used as a
    POST-route dep). Browsers attach cached HTTP Basic creds + cookies on
    cross-origin WS upgrades — without this check, a malicious page that
    knows the URL could open the socket from a victim admin's browser and
    exercise the live tool dispatcher. Absent Origin (programmatic clients
    like a CLI test harness) is allowed; that's not the CSRF threat model.
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    expected = settings.HOSTNAME.rstrip("/").lower()
    if origin.rstrip("/").lower() == expected:
        return True
    if origin.startswith(("http://localhost:", "http://127.0.0.1:")):
        return True
    return False


@app.websocket("/admin/playground/voice")
async def playground_voice(ws: WebSocket, agent_id: str = AGENT_ARIA) -> None:
    """In-browser voice test of Aria. Mirrors /media-stream but:
      - parses a simple {type, payload} JSON envelope instead of Twilio frames,
      - tags the call row is_test_call=true so it's excluded from production
        dashboards by default,
      - generates a synthetic call_sid (log/correlation only — Twilio API calls
        from end_call/transferCall_v3 will harmlessly 404).

    Auth: HTTP Basic credentials cached by the browser from the surrounding
    /admin/playground page are sent automatically on the WS upgrade in Chrome
    and Edge. We read the Authorization header and call verify_admin_basic.
    """
    try:
        verify_admin_basic(ws.headers.get("authorization"))
    except HTTPException:
        # Reject the upgrade. Starlette/uvicorn translate a pre-accept ws.close()
        # into an HTTP 403 response on the upgrade handshake — the application
        # close code (4401) is NOT delivered to the browser, but the connection
        # is refused cleanly so there's no auth bypass. The 4401 here is a hint
        # to anyone reading server logs.
        await ws.close(code=4401)
        return

    if not _ws_origin_ok(ws):
        log.warning(
            "admin.cross_origin_ws_blocked",
            origin=ws.headers.get("origin"),
            expected=settings.HOSTNAME,
        )
        await ws.close(code=4403)
        return

    # Cap concurrent playground sessions process-wide. Non-blocking check so a
    # 4th tab gets a fast 4429 instead of hanging on the upgrade handshake.
    # Acquire AFTER the auth + Origin checks so rejected attempts don't burn
    # slots. The locked() check + acquire() is race-free under asyncio's
    # single-threaded loop — no await in between.
    if _voice_pg_semaphore.locked():
        log.warning("playground.voice.too_many_concurrent", limit=_VOICE_PG_MAX_CONCURRENT)
        await ws.close(code=4429)
        return
    await _voice_pg_semaphore.acquire()

    try:
        await ws.accept()
        agent_id = normalize_agent_id(agent_id)
        agent_config = get_agent_config(agent_id)
        call_id = secrets.token_urlsafe(8)
        call_sid = f"playground-voice-{call_id}"  # synthetic — for log correlation only
        log.info(
            "playground.voice.ws_accepted",
            call_id=call_id,
            agent_id=agent_id,
            agent_label=agent_config.label,
        )

        instructions = await _load_instructions(agent_id)
        # Live transcript stream: an async queue of flushed turns the WS
        # forwarder loop drains and sends to the browser as JSON frames.
        # Bounded so a stuck consumer can't grow memory unbounded — the
        # session's `_publish_turn` drops + logs on QueueFull, and the
        # canonical copy still lands in the DB at end-of-call.
        turn_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
        session = XaiVoiceSession(
            call_id=call_id,
            instructions=instructions,
            agent_id=agent_id,
            tool_defs=tool_defs_for_agent(agent_id),
            transcript_queue=turn_queue,
        )
        session.caller_number = None  # browser caller has no PSTN number

        try:
            await session.connect()
        except Exception:  # noqa: BLE001
            log.exception("xai.connect_failed", call_id=call_id)
            await ws.close(code=1011)
            return

        await db.insert_call_started(
            call_id=call_id,
            call_sid=None,
            caller_number=None,
            agent_id=agent_id,
            is_test_call=True,
        )

        # Tell the browser the call_id so it can poll the recording endpoint
        # after hangup. Sent BEFORE entering the gather so it lands ahead of
        # any media frames.
        try:
            await ws.send_text(json.dumps({"type": "call_started", "call_id": call_id}))
        except Exception:  # noqa: BLE001
            log.exception("playground.voice.call_started_send_failed", call_id=call_id)

        async def from_browser() -> None:
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    mtype = msg.get("type")
                    if mtype == "media":
                        payload_b64 = msg.get("payload")
                        if not isinstance(payload_b64, str):
                            continue
                        await session.forward_caller_audio(payload_b64)
                        try:
                            session.recording.append_caller(base64.b64decode(payload_b64))
                        except Exception:  # noqa: BLE001
                            pass
                    elif mtype == "stop":
                        log.info("playground.voice.stop", call_id=call_id)
                        session.mark_end_source("caller")
                        break
            except WebSocketDisconnect:
                log.info("playground.voice.ws_disconnect", call_id=call_id)
                session.mark_end_source("caller")
            except RuntimeError as exc:
                # Same race as the Twilio path's from_twilio handler:
                # `ws.close()` from from_xai (auto-end / idle-give-up /
                # end_requested) makes a concurrent `ws.receive_text()`
                # raise RuntimeError instead of WebSocketDisconnect. If we
                # let it propagate, post_call.run is skipped and the row
                # stays at disposition='in_progress'.
                log.info(
                    "playground.voice.ws_disconnect.via_close",
                    call_id=call_id,
                    detail=str(exc)[:160],
                )
                session.mark_end_source("caller")
            finally:
                # Unblock from_xai. Unlike Twilio (which closes its WS shortly after
                # sending `stop`), the browser keeps its WS open until the server
                # closes — meaning from_xai would otherwise stay parked in
                # `async for raw in self.ws` until xAI itself times out. Closing
                # the xAI session here causes events() to terminate, from_xai to
                # return, and gather() to complete promptly so post_call.run fires.
                try:
                    await session.close()
                except Exception:  # noqa: BLE001
                    log.exception("playground.voice.session_close_failed", call_id=call_id)

        async def _playground_send_clear() -> None:
            # 1) Drain the browser decoder worklet queue so the caller stops
            #    hearing Aria mid-utterance.
            await ws.send_text(json.dumps({"type": "clear"}))
            # 2) Tell xAI to stop generating. Without this, xAI keeps bursting
            #    the rest of the assistant's response down the WS at faster-than-1x
            #    wall-clock; the browser worklet re-fills its drained queue
            #    and Aria keeps talking for several seconds despite the
            #    clear. Diagnosed from call wehzFIQOzi8 (2026-05-07).
            await session.cancel_response()

        playground_barge_in = BargeInController(
            voice_seconds=settings.BARGE_IN_VOICE_SECONDS,
            backoff_seconds=settings.BARGE_IN_BACKOFF_SECONDS,
            send_clear=_playground_send_clear,
            call_id=call_id,
            get_active_response_id=lambda: session._active_response_id,
        )

        async def from_xai() -> None:
            greeted = False
            try:
                async for event in session.events():
                    etype = event.get("type")

                    if not greeted and session.session_ready:
                        await session.greet()
                        greeted = True

                    if etype == "response.output_audio.delta":
                        delta = event.get("delta")
                        if delta:
                            try:
                                session.recording.append_assistant(base64.b64decode(delta))
                            except Exception:  # noqa: BLE001
                                pass
                            await ws.send_text(json.dumps({
                                "type": "media", "payload": delta,
                            }))

                    elif etype == "input_audio_buffer.speech_started":
                        # Vapi-parity stopSpeakingPlan — debounce the playback
                        # clear by BARGE_IN_VOICE_SECONDS so a single
                        # "okay"/"uh-huh" doesn't cut Aria off.
                        playground_barge_in.on_speech_started()

                    elif etype == "input_audio_buffer.speech_stopped":
                        playground_barge_in.on_speech_stopped()

                    elif etype == "response.function_call_arguments.done":
                        name = event.get("name")
                        fc_call_id = event.get("call_id")
                        args_json = event.get("arguments", "{}")
                        log.info("xai.function_call", call_id=call_id, name=name, fc_call_id=fc_call_id)
                        record = ToolCallRecord(
                            call_id=fc_call_id or "", name=name or "",
                            args=json.loads(args_json or "{}"),
                        )
                        session.tool_calls.append(record)
                        # Live tools — same dispatcher as production. The warning banner
                        # in playground.html flags side-effect risk to the operator.
                        policy_error = validate_tool_call(agent_id, name or "", record.args)
                        output = policy_error or await dispatch_tool(
                            name or "", args_json, call_sid=call_sid, call_id=call_id,
                        )
                        record.output = output
                        raw_err = output.get("error") if isinstance(output, dict) else None
                        record.error = raw_err if isinstance(raw_err, str) and raw_err else None
                        record.finished_at = time.time()
                        await db.insert_tool_call(
                            call_id=call_id,
                            name=record.name,
                            args=record.args,
                            output=record.output,
                            error=record.error,
                            started_at=record.started_at,
                            finished_at=record.finished_at,
                        )
                        if name == "end_call":
                            session.mark_end_source("agent_end_call")
                            # See Twilio path for rationale. Don't echo
                            # function_output back to xAI (prevents the model
                            # from generating a follow-up turn after the
                            # goodbye), and defer the WS-close by 5s so the
                            # goodbye audio finishes playing on the caller's
                            # speaker before the line goes dead.
                            async def _defer_end_requested() -> None:
                                await asyncio.sleep(5.0)
                                session.end_requested = True
                            asyncio.create_task(_defer_end_requested())
                        elif (
                            name == "transferCall_v3"
                            and isinstance(output, dict)
                            and output.get("status") == "transferring"
                            and record.error is None
                        ):
                            session.mark_end_source("transfer")
                        elif fc_call_id:
                            await session.send_function_output(fc_call_id, output)

                    # Idle give-up tear-down (mirrors the Twilio path). When
                    # the watcher sets end_requested after speaking goodbye,
                    # close the browser WS so from_browser exits cleanly and
                    # gather() can complete -> post_call.run runs.
                    if session.end_requested:
                        try:
                            await ws.close(code=1000)
                        except Exception:  # noqa: BLE001
                            pass
                        return
            except Exception:  # noqa: BLE001
                log.exception("xai.event_loop_failed", call_id=call_id)

        async def from_turns() -> None:
            """Drain xAI transcript turns from the session's queue and forward
            them to the browser as `{type:"turn", role, text, ts_ms}` frames.

            Exits when the xAI session has closed (queue stays empty for one
            grace period) or when the WS is gone. We don't want to outlive the
            other two tasks: the gather() above runs with return_exceptions=False
            and waits for ALL three to complete, so this loop must terminate
            instead of looping forever on `queue.get()`.
            """

            async def _send(turn: dict) -> bool:
                """Forward one turn. Return False if the browser is gone."""
                try:
                    await ws.send_text(json.dumps({"type": "turn", **turn}))
                    return True
                except WebSocketDisconnect:
                    return False
                except Exception:  # noqa: BLE001
                    return False

            try:
                while True:
                    # Short timeout so we can re-check `session.ws is None`
                    # (set in session.close()) and exit cleanly when from_browser
                    # tears the session down on hangup.
                    try:
                        turn = await asyncio.wait_for(turn_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if session.ws is None:
                            # Drain anything `_publish_turn` enqueued in the
                            # window between session.close() (sets ws=None) and
                            # this check — typically the assistant's final
                            # response.done turn at hangup. Bounded by queue
                            # size, so this can't hang.
                            while not turn_queue.empty():
                                try:
                                    pending = turn_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                if not await _send(pending):
                                    return
                            return
                        continue
                    if not await _send(turn):
                        return
            except Exception:  # noqa: BLE001
                log.exception("playground.voice.turn_forwarder_failed", call_id=call_id)

        await asyncio.gather(
            from_browser(), from_xai(), from_turns(), return_exceptions=False,
        )
        await session.close()
        await post_call.run(call_id=call_id, session=session)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _voice_pg_semaphore.release()


# ---------------------------------------------------------------------------
# Optional: ephemeral token endpoint for browser clients
# ---------------------------------------------------------------------------

@app.post("/session")
async def create_ephemeral_session() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.x.ai/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {settings.XAI_API_KEY}", "Content-Type": "application/json"},
            json={"expires_after": {"seconds": 300}},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    body = resp.json()
    return {
        "client_secret": {"value": body.get("value"), "expires_at": body.get("expires_at")},
        "voice": settings.XAI_VOICE,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_instructions(agent_id: str = AGENT_ARIA) -> str:
    """Load the selected assistant prompt from DB, then file fallback, then a safety stub.

    The DB path lets the admin dashboard publish prompt edits without a redeploy.
    Each static assistant has its own file fallback for first boot.
    """
    agent_config = get_agent_config(agent_id)
    content = await db.get_active_prompt(agent_config.agent_id)
    if content:
        return content
    if agent_config.prompt_fallback_path.exists():
        return agent_config.prompt_fallback_path.read_text(encoding="utf-8")
    return (
        f"You are {agent_config.label}, the Acme Law client services voice assistant. "
        "Keep responses short and conversational. Ask one question at a time. "
        "Use only the tools enabled for this assistant."
    )
