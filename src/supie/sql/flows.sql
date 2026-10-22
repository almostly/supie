-- supie workflows: one row per run, one per finished step. Written by
-- `supie flow migration`; safe to run again.
--
-- Only the service role reads and writes these tables: row level security is
-- on with no policies, and anon and authenticated get no grants. The grants
-- are explicit because Supabase stops granting new tables to its Data API
-- roles by default.

create table if not exists public.supie_flow_runs (
  id text primary key,
  flow text not null,
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'failed')),
  owner text,  -- the signed-in user who started the run, if any
  input jsonb,
  output jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.supie_flow_steps (
  run_id text not null references public.supie_flow_runs (id) on delete cascade,
  step text not null,
  status text not null check (status in ('succeeded', 'failed')),
  output jsonb,
  error text,
  attempts integer not null default 1,
  goto text,  -- the branch this step handed its result to
  finished_at timestamptz not null default now(),
  primary key (run_id, step)
);

create index if not exists supie_flow_runs_idle
  on public.supie_flow_runs (flow, status, updated_at);

alter table public.supie_flow_runs enable row level security;
alter table public.supie_flow_steps enable row level security;

revoke all on public.supie_flow_runs, public.supie_flow_steps from anon, authenticated;
grant select, insert, update, delete
  on public.supie_flow_runs, public.supie_flow_steps to service_role;

-- updated_at tracks activity: any change to a run or to one of its steps.
create or replace function public.supie_flow_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_table_name = 'supie_flow_runs' then
    new.updated_at := now();
  else
    update public.supie_flow_runs set updated_at = now() where id = new.run_id;
  end if;
  return new;
end
$$;

drop trigger if exists supie_flow_runs_touch on public.supie_flow_runs;
create trigger supie_flow_runs_touch
  before update on public.supie_flow_runs
  for each row execute function public.supie_flow_touch();

drop trigger if exists supie_flow_steps_touch on public.supie_flow_steps;
create trigger supie_flow_steps_touch
  after insert or update on public.supie_flow_steps
  for each row execute function public.supie_flow_touch();

-- Runs whose worker stopped: still running, with no activity for a while.
create or replace function public.supie_flow_stale(flow_name text, idle_seconds integer)
returns setof text
language sql
stable
set search_path = ''
as $$
  select id from public.supie_flow_runs
  where flow = flow_name
    and status = 'running'
    and updated_at < now() - make_interval(secs => idle_seconds)
  order by updated_at
$$;

revoke execute on function public.supie_flow_touch() from public, anon, authenticated;
revoke execute on function public.supie_flow_stale(text, integer) from public, anon, authenticated;
grant execute on function public.supie_flow_stale(text, integer) to service_role;
