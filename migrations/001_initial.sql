-- Migration 001: Initial schema for the xAI voice bridge
-- Run this in Supabase Studio → SQL Editor (https://supabase.com/dashboard → your project → SQL).
-- Idempotent: safe to re-run; uses IF NOT EXISTS where supported.

-- ============================================================================
-- calls: one row per inbound call
-- ============================================================================

create table if not exists calls (
  id text primary key,                       -- our 8-char urlsafe call_id
  call_sid text,                             -- Twilio CallSid
  caller_number text,                        -- E.164
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  duration_seconds integer,
  ended_reason text,                         -- 'completed' | 'abandoned' | 'hangup' | 'error'

  -- Vapi-parity structured outputs (10 fields)
  first_name text,
  last_name text,
  caller_fullname text,
  caller_email text,
  callback_number text,                      -- the "best callback" — fixes the buggy `caller_number` enum from live Vapi
  caller_status text,                        -- enum (10 values, see CHECK below)
  service_type text,                         -- enum (15 values, see CHECK below)
  forward_msg_to text,                       -- enum: [Attorney Name] | [Staff Member] | [Staff Member] | Team (was `recipient`)
  call_outcome text,                         -- enum (5 values)
  sms_meeting_link text,                     -- 'Yes' | 'No'

  -- Additional fields requested by ops
  meeting_scheduled boolean,
  meeting_datetime timestamptz,
  meeting_notes text,

  -- Bridge-level metadata
  disposition text,                          -- 'scheduled' | 'transferred_attorney' | 'transferred_voicemail' | 'info_only' | 'abandoned'
  interruption_count integer default 0,

  -- Costs (per call)
  xai_input_tokens integer default 0,
  xai_output_tokens integer default 0,
  xai_input_audio_tokens integer default 0,
  xai_output_audio_tokens integer default 0,
  xai_cost_usd numeric(10, 6),
  twilio_cost_usd numeric(10, 6),
  total_cost_usd numeric(10, 6) generated always as
    (coalesce(xai_cost_usd, 0) + coalesce(twilio_cost_usd, 0)) stored,

  -- Transcripts (concatenated; per-turn detail can come later)
  transcript_caller text,
  transcript_bot text,

  created_at timestamptz default now(),
  updated_at timestamptz default now(),

  -- Enum guards — match the live Vapi structuredDataPlan values exactly.
  constraint calls_caller_status_chk check (
    caller_status is null or caller_status in (
      'Buyer', 'Seller', 'Lender', 'Real Estate Investor',
      'Owner', 'Real Estate Agent', 'Attorney', 'Closing Agent',
      'Unknown', 'Existing Client'
    )
  ),
  constraint calls_service_type_chk check (
    service_type is null or service_type in (
      'Purchase Agreement', 'Title Opinion', 'Seller Representation',
      'Cash Closing', 'Quit Claim Deed', 'Title Clearing Affidavit',
      'Limited Power of Attorney', 'Installment Contract',
      'Draft/Review Lease', 'Will Package', 'Durable Power of Attorney',
      'Entity Formation', 'Platting Assistance', 'Remote Online Notary',
      'Out of Scope'
    )
  ),
  constraint calls_forward_msg_to_chk check (
    forward_msg_to is null or forward_msg_to in ('[Attorney Name]', '[Staff Member]', '[Staff Member]', 'Team')
  ),
  constraint calls_call_outcome_chk check (
    call_outcome is null or call_outcome in (
      'Needs follow-up by attorney', 'Left Message',
      'Appointment scheduled, follow-up needed',
      'Provided Instructions', 'No specific outcome'
    )
  ),
  constraint calls_sms_meeting_link_chk check (
    sms_meeting_link is null or sms_meeting_link in ('Yes', 'No')
  ),
  constraint calls_disposition_chk check (
    disposition is null or disposition in (
      'scheduled', 'transferred_attorney', 'transferred_voicemail',
      'info_only', 'abandoned', 'in_progress'
    )
  ),
  constraint calls_ended_reason_chk check (
    ended_reason is null or ended_reason in (
      'completed', 'abandoned', 'hangup', 'error'
    )
  )
);

create index if not exists calls_started_at_idx on calls (started_at desc);
create index if not exists calls_disposition_idx on calls (disposition);
create index if not exists calls_caller_number_idx on calls (caller_number);

-- updated_at auto-touch trigger
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists calls_updated_at on calls;
create trigger calls_updated_at
  before update on calls
  for each row execute function set_updated_at();


-- ============================================================================
-- tool_calls: per-call tool invocations
-- ============================================================================

create table if not exists tool_calls (
  id bigserial primary key,
  call_id text references calls(id) on delete cascade,
  name text not null,                        -- 'hubspot_get_availability_v3' | 'send_sms_summary_openphone' | etc.
  args jsonb,
  output jsonb,
  error text,
  started_at timestamptz,
  finished_at timestamptz,
  latency_ms integer
);

create index if not exists tool_calls_call_id_idx on tool_calls (call_id);
create index if not exists tool_calls_name_idx on tool_calls (name);


-- ============================================================================
-- prompts: versioned system prompt; exactly one is active at a time
-- ============================================================================

create table if not exists prompts (
  id bigserial primary key,
  content text not null,
  is_active boolean default false,
  notes text,                                -- optional commit-style message
  created_at timestamptz default now()
);

-- Only one row may have is_active=true
create unique index if not exists prompts_one_active on prompts (is_active) where is_active = true;


-- ============================================================================
-- Row-Level Security: lock down anon + authenticated; service_role bypasses RLS
-- so the bridge (using the service_role key / direct DB connection) still works.
-- ============================================================================

alter table calls enable row level security;
alter table tool_calls enable row level security;
alter table prompts enable row level security;

-- No policies = no anon/authenticated access. Service role bypasses RLS.
-- (If you later want PostgREST/REST API access for read-only views, add policies here.)


-- ============================================================================
-- Seed: insert a placeholder active prompt. Run the next bit ONCE after
-- migration to load the real Aria instructions from prompts/aria_instructions.md:
--
--   insert into prompts (content, is_active, notes)
--   values (<paste full file>, true, 'Initial seed from prompts/aria_instructions.md');
--
-- The bridge's db.get_active_prompt() falls back to the file on disk if the
-- prompts table is empty, so this seed is recommended but not strictly required
-- on first deploy.
-- ============================================================================
