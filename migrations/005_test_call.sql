-- Migration 005: voice playground test-call flag
--
-- Calls that originate from the in-browser /admin/playground/voice route are
-- tagged with is_test_call = true so the admin call list and costs dashboard
-- can filter them out. The default false keeps every existing row + every
-- Twilio inbound call categorized as a real production call.
--
-- A partial index covers only the rare is_test_call = true rows — the
-- eventual "Show test calls" admin toggle will scan this small subset
-- without bloating the index for the production write path.

alter table calls add column if not exists is_test_call boolean not null default false;

create index if not exists idx_calls_is_test_call
  on calls (started_at desc)
  where is_test_call = true;

comment on column calls.is_test_call is
  'True when call originated from the in-browser voice playground (admin/playground/voice). Filtered out of /admin/calls and /admin/costs by default.';
