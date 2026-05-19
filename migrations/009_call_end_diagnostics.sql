-- Migration 009: call end attribution and recording diagnostics
-- Run in Supabase Studio SQL Editor. Idempotent.

alter table calls add column if not exists ended_by text;
alter table calls add column if not exists recording_health text;
alter table calls add column if not exists recording_mismatch_seconds integer;

alter table calls drop constraint if exists calls_disposition_chk;
alter table calls add constraint calls_disposition_chk check (
  disposition is null or disposition in (
    'scheduled', 'transferred_attorney', 'transferred_voicemail',
    'appointment_offered_no_response',
    'info_only', 'abandoned', 'in_progress'
  )
);

alter table calls drop constraint if exists calls_ended_by_chk;
alter table calls add constraint calls_ended_by_chk check (
  ended_by is null or ended_by in (
    'caller', 'agent_end_call', 'idle_timeout', 'auto_end',
    'transfer', 'system_error', 'unknown'
  )
);

alter table calls drop constraint if exists calls_recording_health_chk;
alter table calls add constraint calls_recording_health_chk check (
  recording_health is null or recording_health in (
    'ok', 'short', 'missing', 'finalize_failed', 'upload_failed'
  )
);

comment on column calls.ended_by is
  'Best bridge-level attribution for call teardown: caller, agent_end_call, idle_timeout, auto_end, transfer, system_error, unknown.';

comment on column calls.recording_health is
  'Post-call recording diagnostic. short means recording duration was materially shorter than call duration.';

comment on column calls.recording_mismatch_seconds is
  'Positive call-duration minus recording-duration gap in seconds, when measurable.';
