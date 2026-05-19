# Voice AI Bridge (xAI + Twilio + FastAPI)

> Template / showcase project. A production-shaped FastAPI bridge that connects Twilio Media Streams to xAI's Voice Agent API (`grok-voice-think-fast-1.0`) for self-hosted voice AI receptionists. Not configured for live deployment — firm-specific business logic and credentials have been stripped out, but the architecture and patterns are intact.

## What this demonstrates

- **Real-time audio bridge** between Twilio's μ-law/8 kHz Media Streams and xAI's WebSocket voice API, with no transcoding in the live audio path
- **Barge-in detection** with response-id gating and debounced VAD so xAI's faster-than-realtime audio bursts don't cause cancellations on the caller's normal turn-take
- **Per-response transcript demultiplexing** so concurrent responses don't interleave at the delta level
- **Stereo call recording** with wall-clock realignment (caller on left, agent on right), uploaded to Supabase Storage with private signed URLs
- **Tool dispatch** for CRM integration (HubSpot meeting booking via Make.com webhooks), warm transfers via TwiML `<Dial>` with whisper TTS, voicemail routing, end-of-call hangup, SMS summary
- **Post-call pipeline** — structured-output extraction via xAI text completions with retry on transient errors, persisted to Postgres, fanned out to Make.com + Google Chat via cardsV2
- **Admin dashboard** with HTMX-inline editing, call logs, recording playback, cost dashboard, chat playground, and an in-browser voice playground (mic capture → μ-law worklet → bridge → speaker)
- **DNS workaround** for Mullvad-VPN content-blocking that resolves third-party API hosts via Cloudflare DoH and patches `asyncio.BaseEventLoop.getaddrinfo` (auto no-op when system DNS works)
- **Production deploy** patterns: Dockerfile + fly.toml with graceful shutdown for in-flight WebSocket calls, signed Twilio webhooks, bcrypt admin auth, structlog JSON logging with `call_id` correlation

## Tech stack

| Layer | Tech |
|---|---|
| Bridge | FastAPI + uvicorn (Python 3.12+) |
| Voice provider | xAI Voice Agent API (`grok-voice-think-fast-1.0`) — WebSocket realtime |
| Telephony | Twilio Programmable Voice + Media Streams |
| Persistence | Supabase Postgres (asyncpg, transaction pooler) + Supabase Storage (private bucket) |
| CRM | HubSpot meetings API (direct + Make.com booking webhooks) |
| Notification | Make.com Vapi-shape end-of-call envelope; Google Chat cardsV2 |
| Deploy | Fly.io (`iad`, internal port 8000, force HTTPS, graceful 5-min drain) |
| Logging | structlog JSON, pinned `websockets`/`httpx`/`httpcore` to WARNING to avoid leaking Bearer tokens at DEBUG |
| Auth | HTTP Basic + bcrypt for the admin dashboard |

## Repo layout

```
.
├── main.py                  FastAPI app: /twiml, /media-stream/{id}, /whisper/{id}, /health
├── xai_session.py           xAI WebSocket lifecycle, idle watcher, transcript demux, auto-end backstops
├── barge_in.py              Debounced VAD + response-id gating
├── recording.py             Per-channel μ-law buffers; stereo WAV finalization with wall-clock realignment
├── post_call.py             Structured-output extraction; cost accounting; Postgres persist; CRM mirror
├── notify_chat.py           Google Chat cardsV2 — direct webhook (Make's JSON-substitution path fails on
│                            transcripts with quotes/newlines)
├── notify_make.py           Vapi-shape end-of-call envelope POST to Make.com
├── db.py                    asyncpg pool + queries (calls, tool_calls, prompts) — per-agent prompt cache
├── storage.py               Supabase Storage REST client (upload_recording, signed_url)
├── settings.py              pydantic-settings env loader
├── logging_config.py        structlog JSON + secret-safe defaults
├── tools.py                 AGENT_TOOL_DEFS (realtime + chat-completions variants), dispatch_tool
├── tools_pkg/               One handler module per tool
├── agents.py                Static assistant registry + business-hours schedule
├── dev_dns.py               Mullvad VPN DNS bypass via Cloudflare DoH
├── admin/                   Admin dashboard (FastAPI routes + Jinja2 templates + HTMX + Chart.js)
├── prompts/aria_instructions.md       Generic voice receptionist system prompt
├── migrations/              SQL migrations (asyncpg-compatible)
├── scripts/                 Operator tools (backfill, HubSpot probe, etc.)
├── tests/                   unittest test suite
├── Dockerfile               python:3.12-slim, non-root, uvicorn --timeout-graceful-shutdown 300
└── fly.toml                 iad, internal_port 8000, kill_signal SIGINT, kill_timeout 5m
```

## Architecture notes

**Audio passthrough.** Twilio sends μ-law/8 kHz frames over its Media Streams WebSocket. xAI's Voice API accepts `audio/pcmu` natively. Both directions stay μ-law end-to-end — no STT/TTS step, no resampling. The browser-based voice playground does encode 48 kHz → 8 kHz μ-law in an `AudioWorklet` so the same bridge code handles both telephony and browser sessions.

**Stereo recording realignment.** xAI bursts assistant audio faster than 1× wall-clock. Twilio sends caller frames at exactly 1×. A naive interleave would put the agent's reply at the same byte offset as the caller's question. `recording.RecordingBuffer.append_assistant` re-anchors at the start of each agent turn by padding `assistant_ulaw` with μ-law silence up to `len(caller_ulaw)` whenever it's at least 1 second behind.

**Barge-in three-layer fix.**
1. On VAD `speech_started` during agent playback, send `response.cancel` to xAI so it stops streaming more bytes (without this, the browser worklet kept refilling from xAI's burst).
2. Capture `active_response_id` at `speech_started` and skip the cancel if a different response started before the debounce timer fires (was killing xAI's auto-reply to a normal turn).
3. Clear `active_response_id` on a deferred timer matching projected audio-playback end, not at `response.done` — barge-ins during the playout window need the gate still open.

**Per-response transcript demux.** Single `_assistant_buffer` → `dict[response_id, list[str]]` so concurrent responses don't interleave at the delta level. Each `response.done` flushes only its own chunks.

**Auto-end backstop.** Some prompt configurations have the agent say goodbye verbally but skip emitting the `end_call` tool. Session-level regex on flushed assistant turns matches goodbye patterns (`have a (great|nice) day`, `take care`, etc.) plus verbal-intent patterns (`I'll end the call`, `going to wrap up`). On match, schedule `end_requested` 5 seconds after detection so the agent finishes speaking before the WebSocket closes.

**Post-call retry.** Structured-output extraction uses xAI text completions and occasionally hits 5xx. Retries 3 times at 0/1/3 s backoff for `5xx + TimeoutException + NetworkError + RemoteProtocolError`. 4xx and JSON-parse failures don't retry (deterministic). Mock-transport unit-tested.

**Logging hardening.** `LOG_LEVEL=DEBUG` + the `websockets` library DEBUG logger together logged the full `Authorization: Bearer xai-...` request header on every WS handshake. `logging_config` pins `websockets`/`httpx`/`httpcore` to WARNING regardless of root level.

## Running locally (template — needs your credentials)

```bash
# Python 3.12+. On 3.13 install the audioop-lts backport (PEP 594).
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt

# Copy and fill in
cp .env.example .env

# Apply migrations to your Supabase Postgres (one at a time, in numeric order)

# Run
uvicorn main:app --reload --host 127.0.0.1 --port 8080

# Expose to Twilio
ngrok http 8080
# Point your Twilio number's Voice Configuration at https://<ngrok-id>.ngrok-free.app/twiml
```

Admin dashboard at `http://localhost:8080/admin/calls` (HTTP Basic; password is the bcrypt hash in `.env`).

## Tests

```bash
python -m unittest discover -s tests
```

Tests cover assistant routing, recording realignment, post-call routing decisions, and the xAI session backstops.

## Not in this repo (by design)

This is a stripped-down template. Production-only pieces removed:
- Real API keys and webhook URLs (`.env.example` has placeholder values)
- Firm-specific intake business logic in the agent prompt
- Knowledge-base PDFs (industry-specific resources)
- Operational deploy notes and session history
- Firm-specific migration scripts (agent renames, schedule overrides)

The architecture, patterns, and most module-level code are otherwise identical to the working version.

## License

MIT (or your preferred license — adjust before publishing).
