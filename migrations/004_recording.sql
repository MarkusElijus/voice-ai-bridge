-- Migration 004: call recording metadata
--
-- The recording itself lives in Supabase Storage (bucket "call-recordings",
-- key "<call_id>.wav") — we persist only the storage path here and generate
-- signed URLs on demand from the admin route. recording_path is NULL until
-- post_call uploads successfully; recording_duration_seconds matches the
-- duration of the audio (may be slightly different from call duration_seconds
-- because the recording starts on the first audio frame, not call setup).

alter table calls add column if not exists recording_path text;
alter table calls add column if not exists recording_duration_seconds integer;

comment on column calls.recording_path is
  'Object key inside the Supabase Storage call-recordings bucket (no signed URL — generate on demand).';
comment on column calls.recording_duration_seconds is
  'Duration of the recorded WAV in seconds.';
