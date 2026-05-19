-- Migration 002: agent_id forward-compat
-- Run this in Supabase Studio → SQL Editor.
-- Idempotent: safe to re-run. Adds an `agent_id` column to `calls` and `prompts`
-- so the dashboard + bridge can support multiple voice agents (Aria inbound,
-- outbound, future agents) without a schema rewrite later. Defaults to 'aria'
-- for all existing rows so nothing else has to change.

alter table calls   add column if not exists agent_id text not null default 'aria';
alter table prompts add column if not exists agent_id text not null default 'aria';

create index if not exists calls_agent_id_idx   on calls   (agent_id);
create index if not exists prompts_agent_id_idx on prompts (agent_id);

-- Active-prompt uniqueness is now per-agent (was global). Drop the old global
-- partial unique index and recreate it scoped to (agent_id, is_active=true).
drop index if exists prompts_one_active;
create unique index if not exists prompts_one_active_per_agent
  on prompts (agent_id) where is_active = true;
