-- supie on pgflow: functions that let Python workers reach pgflow over
-- PostgREST, since WebAssembly has no sockets for a Postgres connection.
-- Written by `supie flow migration --pgflow`; safe to run again. Install
-- pgflow first (its migrations create the pgflow schema and the pgmq queues).
--
-- Only the service role may call these: they run as their owner (security
-- definer) to reach the pgflow and pgmq schemas, which PostgREST doesn't expose.

-- Who started a run, so a signed-in user only sees their own runs.
create table if not exists public.supie_pgflow_owners (
  run_id uuid primary key,
  owner text not null
);
alter table public.supie_pgflow_owners enable row level security;
revoke all on public.supie_pgflow_owners from anon, authenticated;
grant select, insert on public.supie_pgflow_owners to service_role;

-- Compile a flow from its shape, or check the stored one matches.
create or replace function public.supie_pgflow_compile(flow_slug text, shape jsonb)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select pgflow.ensure_flow_compiled(flow_slug, shape)
$$;

-- Start a run and return its ID.
create or replace function public.supie_pgflow_start(
  flow_slug text, input jsonb, owner text default null
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run_id uuid;
begin
  select r.run_id into v_run_id from pgflow.start_flow(flow_slug, input) as r;
  if owner is not null then
    insert into public.supie_pgflow_owners (run_id, owner) values (v_run_id, owner);
  end if;
  return v_run_id;
end
$$;

-- Register a worker (pgflow tracks which worker holds each task).
create or replace function public.supie_pgflow_register(
  worker_id uuid, flow_slug text, function_name text
)
returns void
language sql
security definer
set search_path = ''
as $$
  insert into pgflow.workers (worker_id, queue_name, function_name)
  values (worker_id, flow_slug, function_name)
  on conflict (worker_id) do update set last_heartbeat_at = now()
$$;

-- Wait up to max_poll_seconds for messages on the flow's queue and return
-- their IDs. A separate call from claiming them: start_tasks stamps tasks with
-- now(), which must be later than when a retried task was queued again.
create or replace function public.supie_pgflow_read(
  flow_slug text,
  worker_id uuid,
  qty integer default 10,
  max_poll_seconds integer default 2
)
returns bigint[]
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ids bigint[];
begin
  update pgflow.workers as w set last_heartbeat_at = now()
  where w.worker_id = supie_pgflow_read.worker_id;
  select array_agg(m.msg_id) into v_ids
  from pgmq.read_with_poll(flow_slug, 10, qty, max_poll_seconds, 100) as m;
  return coalesce(v_ids, '{}');
end
$$;

-- Claim tasks by message ID and return each with its input, the run's input
-- (pgflow sends that only to root steps) and its attempt number.
create or replace function public.supie_pgflow_claim(
  flow_slug text, worker_id uuid, msg_ids bigint[]
)
returns setof jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  t record;
  v_flow_input jsonb;
  v_attempt integer;
begin
  for t in select * from pgflow.start_tasks(flow_slug, msg_ids, worker_id, flow_slug) loop
    -- A separate statement, so it sees the attempt count start_tasks just raised.
    select r.input, st.attempts_count into v_flow_input, v_attempt
    from pgflow.runs as r
    join pgflow.step_tasks as st on st.run_id = r.run_id
    where r.run_id = t.run_id and st.step_slug = t.step_slug and st.task_index = t.task_index;
    return next jsonb_build_object(
      'run_id', t.run_id,
      'step_slug', t.step_slug,
      'task_index', t.task_index,
      'input', t.input,
      'flow_input', coalesce(t.flow_input, v_flow_input),
      'attempt', v_attempt
    );
  end loop;
end
$$;

drop function if exists public.supie_pgflow_poll(text, uuid, integer, integer);

create or replace function public.supie_pgflow_complete(
  run_id uuid, step_slug text, task_index integer, output jsonb
)
returns void
language sql
security definer
set search_path = ''
as $$
  select from pgflow.complete_task(run_id, step_slug, task_index, output)
$$;

-- Record a failure; pgflow retries until the step's max attempts.
create or replace function public.supie_pgflow_fail(
  run_id uuid, step_slug text, task_index integer, error_message text
)
returns void
language sql
security definer
set search_path = ''
as $$
  select from pgflow.fail_task(run_id, step_slug, task_index, error_message)
$$;

-- A run with its steps' states, and who started it.
create or replace function public.supie_pgflow_run(run_id uuid)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select pgflow.get_run_with_states(run_id)
    || jsonb_build_object(
      'owner',
      (select o.owner from public.supie_pgflow_owners as o where o.run_id = supie_pgflow_run.run_id)
    )
$$;

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'supie_pgflow_compile(text, jsonb)',
    'supie_pgflow_start(text, jsonb, text)',
    'supie_pgflow_register(uuid, text, text)',
    'supie_pgflow_read(text, uuid, integer, integer)',
    'supie_pgflow_claim(text, uuid, bigint[])',
    'supie_pgflow_complete(uuid, text, integer, jsonb)',
    'supie_pgflow_fail(uuid, text, integer, text)',
    'supie_pgflow_run(uuid)'
  ] loop
    execute format('revoke execute on function public.%s from public, anon, authenticated', fn);
    execute format('grant execute on function public.%s to service_role', fn);
  end loop;
end
$$;
