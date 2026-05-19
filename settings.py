"""Application settings loaded from environment variables.

All secrets and config live here. Use `from settings import settings` everywhere.
For Fly.io: set with `fly secrets set KEY=VALUE`.
For local dev: put in `.env` next to this file.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # xAI
    XAI_API_KEY: str
    XAI_REALTIME_URL: str = "wss://api.x.ai/v1/realtime"
    XAI_MODEL: str = "grok-voice-think-fast-1.0"
    XAI_VOICE: str = "ara"  # ara | rex | eve | sal | leo

    # Twilio
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_PHONE_NUMBER: str  # E.164, e.g. +15155551234
    TWILIO_VALIDATE_SIGNATURE: bool = True

    # OpenPhone (SMS)
    OPENPHONE_API_KEY: str
    OPENPHONE_DEFAULT_FROM: str  # E.164

    # HubSpot booking via Make.com — keep existing scenario URL
    HUBSPOT_MAKE_WEBHOOK: str = "https://hook.<region>.make.com/<your-booking-webhook>"
    # Make.com webhook for the end-of-call report scenario (post-call fanout:
    # Google Chat notification, SMS to caller, Google Sheets log, HubSpot
    # contact lookup, transfer post-processing). The bridge POSTs a Vapi-shape
    # envelope to this URL. Leave unset locally so dev calls don't trigger
    # production notifications; set the Fly secret in production.
    MAKE_VAPI_WEBHOOK_URL: str | None = None
    # Google Chat incoming webhook for the after-call card. Bridge POSTs the
    # fully-rendered cardsV2 card directly to this URL (skips Make.com for
    # the card path) because Make.com's HTTP module mustache-substitutes
    # values into a JSON string body — transcripts/summaries containing "
    # or newline chars break JSON validation (InvalidConfigurationError
    # observed 2026-05-11). The Make.com scenario still receives the same
    # payload via MAKE_VAPI_WEBHOOK_URL for Quo SMS / Sheets log / HubSpot
    # contact lookup / transfer post-processing — it just no longer renders
    # the Chat card.
    GOOGLE_CHAT_WEBHOOK_URL: str | None = None
    # HubSpot availability — direct Private App (primary path; mirrors live Vapi code tool)
    HUBSPOT_PRIVATE_APP_TOKEN: str | None = None
    HUBSPOT_MEETING_LINK_PATH: str = "<your-hubspot-account>%2F<your-meeting-link-slug>"
    # Optional: Make.com webhook fallback for availability (only used if Private App token absent)
    HUBSPOT_AVAILABILITY_MAKE_WEBHOOK: str | None = None
    HUBSPOT_AVAILABILITY_USER_ID: str | None = None  # legacy, currently unused

    # Vapi (not used by the live bridge — kept for periodic config-sync / drift detection)
    VAPI_API_KEY: str | None = None

    # Transfer destinations
    ATTORNEY_TRANSFER_NUMBER: str = "+15555550100"
    VOICEMAIL_NUMBER: str = "+15555550101"

    # Supabase Postgres (transaction pooler URL on port 6543 recommended)
    DATABASE_URL: str | None = None
    SUPABASE_DB_POOL_MIN: int = 2
    SUPABASE_DB_POOL_MAX: int = 10

    # Supabase Storage (call recordings) — both required for recordings to upload
    # SUPABASE_URL e.g. https://<project-ref>.supabase.co (no trailing slash)
    # SUPABASE_SERVICE_ROLE_KEY: long JWT from Supabase project settings -> API
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_ROLE_KEY: str | None = None

    # Cost rates (USD). Placeholders pending official xAI rate confirmation.
    # Update from https://docs.x.ai/developers/models or your team console.
    XAI_COST_INPUT_AUDIO_PER_1K: float = 0.030    # $/1K input audio tokens
    XAI_COST_OUTPUT_AUDIO_PER_1K: float = 0.060   # $/1K output audio tokens
    XAI_COST_INPUT_TEXT_PER_1K: float = 0.0025    # $/1K input text tokens
    XAI_COST_OUTPUT_TEXT_PER_1K: float = 0.010    # $/1K output text tokens
    TWILIO_INBOUND_COST_PER_MIN: float = 0.0085   # $/min inbound voice
    TWILIO_OUTBOUND_COST_PER_MIN: float = 0.013   # $/min outbound voice (transfers)

    # ------------------------------------------------------------------
    # Speaking plans — Vapi-parity barge-in / endpointing knobs.
    #
    # startSpeakingPlan: how patient xAI's VAD is before declaring the
    #   caller's turn over and letting Aria respond. Maps to xAI's
    #   `turn_detection.silence_duration_ms` in session.update.
    # stopSpeakingPlan: how aggressively Aria yields when the caller
    #   barges in. We debounce xAI's `speech_started` event by
    #   BARGE_IN_VOICE_SECONDS (must hear caller voice for at least
    #   that long before clearing playback) and then enforce a
    #   BARGE_IN_BACKOFF_SECONDS refractory period so VAD oscillation
    #   can't double-clear. NumWords is not implementable yet — xAI
    #   only emits the full transcript on `completed`, not word
    #   deltas during speech.
    #
    # Defaults are intentionally LESS aggressive than Vapi's
    # (voiceSeconds=0.1) because the operator's 2026-05-05 test showed a single
    # "okay" cutting Aria mid-sentence. Tune in .env.
    # ------------------------------------------------------------------
    START_SPEAKING_WAIT_MS: int = 500          # xAI silence_duration_ms; Vapi default 500 ms
    # xAI server-VAD activation threshold (0.1-0.9). Higher = needs louder /
    # longer audio to trigger speech_started. xAI's documented default is
    # 0.85, which is very conservative — observed in calls vFIqBTyEPeY and
    # uIvOEVDMs54 (2026-05-07) that speech_started fires ~2 seconds AFTER
    # the caller actually started speaking. Lowering to 0.5 makes barge-in
    # responsive without false-firing on background noise. Tune in .env.
    XAI_VAD_THRESHOLD: float = 0.5
    # Audio (in ms) included BEFORE the detected start of speech. Helps
    # capture the first syllable that would otherwise be clipped by VAD's
    # detection latency. xAI default 333 ms; lower = snappier perceived
    # response, slightly higher risk of clipping the first word.
    XAI_VAD_PREFIX_PADDING_MS: int = 200
    # 0.7 ≈ 2 spoken words at normal English speech rate (~3 words/sec). xAI
    # realtime doesn't emit input-transcription deltas during speech, so we
    # can't implement Vapi's exact `numWords:2` semantics — voice duration is
    # the closest proxy. Tune in .env if you want a snappier or more patient
    # interrupt threshold.
    BARGE_IN_VOICE_SECONDS: float = 0.7        # voice duration before assistant yields
    BARGE_IN_BACKOFF_SECONDS: float = 1.0      # refractory window after a clear

    # ------------------------------------------------------------------
    # Parity-test injection knobs. Default off — only flip in .env when
    # running scenario 6 of docs/parity-test.md (HubSpot 503 simulation),
    # then unset and restart uvicorn after the test.
    # ------------------------------------------------------------------
    HUBSPOT_FORCE_503: bool = False

    # Admin dashboard auth (Phase 2). HTTP Basic with bcrypt-hashed password.
    ADMIN_USER: str = "admin"
    ADMIN_PASSWORD_HASH: str | None = None  # bcrypt hash; if None, /admin/* returns 503

    # Public hostname (Fly.io URL or ngrok URL during dev) — no trailing slash, https://
    HOSTNAME: str

    # Server
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    ENV: str = Field(default="dev", pattern="^(dev|staging|prod)$")


settings = Settings()  # type: ignore[call-arg]
