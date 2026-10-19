-- Run this once in Supabase Dashboard -> SQL Editor -> New query -> Run
create table if not exists public.backend_dev (
  job_id      text primary key,
  status      text not null,           -- sent | rejected | title_blocked | failed
  title       text,
  company     text,
  location    text,
  url         text,
  min_years   numeric,
  max_years   numeric,
  reason      text,
  created_at  timestamptz not null default now()
);

-- Add the cleanup timestamp if the table was created from an older schema.
alter table public.backend_dev
  add column if not exists created_at timestamptz not null default now();

create index if not exists backend_dev_created_at_idx on public.backend_dev (created_at);

-- Only the server (service_role key) should touch this table.
alter table public.backend_dev enable row level security;
