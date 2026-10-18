create table if not exists public.jobs (
    job_id text primary key,
    ts bigint not null,
    status text not null
);

alter table public.jobs enable row level security;
