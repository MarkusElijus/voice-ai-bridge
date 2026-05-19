-- Migration 003: per-turn transcript stored as ordered JSONB
--
-- transcript_caller / transcript_bot stay (useful for full-text search), but
-- the dashboard now wants a chronological view where caller and Aria turns
-- alternate the way a chat transcript reads. This column stores an ordered
-- array shaped like:
--   [
--     {"role": "assistant", "text": "Thank you for calling Acme Law...", "ts_ms": 1640},
--     {"role": "caller",    "text": "Hi, this is Mark...",                   "ts_ms": 4280},
--     ...
--   ]
-- ts_ms is milliseconds since started_at (call start). The bridge appends to
-- this list as turns complete; post_call._persist_postgres writes the final
-- list once.

alter table calls add column if not exists transcript_turns jsonb;

comment on column calls.transcript_turns is
  'Ordered chat-style transcript: [{role, text, ts_ms}]. role in (caller, assistant). ts_ms is offset from started_at.';
