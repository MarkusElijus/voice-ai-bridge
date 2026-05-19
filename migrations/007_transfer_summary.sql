-- Migration 007: per-call transfer_summary text column.
--
-- Background: warm transfers (scenario 2, urgent -> attorney) now use Twilio's
-- <Number url="https://.../whisper/{call_id}"> pattern, which makes Twilio
-- fetch a TwiML from the bridge when the attorney's phone is answered. That
-- TwiML plays a spoken summary to the attorney BEFORE the inbound caller is
-- bridged in. This replaces the prior Vapi-era workaround of sending an
-- OpenPhone SMS heads-up beforehand (which existed because Vapi could not do
-- warm transfers to non-SIP/PSTN destinations).
--
-- transferCall_v3 stashes the `summary` argument into this column right
-- before issuing the Twilio update. The /whisper/{call_id} endpoint reads it
-- back, embeds it in a <Say>, and returns TwiML. Two machines run the bridge
-- so the summary MUST live in shared storage (Postgres) - process-local
-- memory wouldn't survive cross-machine LB routing.
--
-- Idempotent at the SQL layer via `if not exists`.

alter table calls add column if not exists transfer_summary text;
