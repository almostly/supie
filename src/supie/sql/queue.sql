-- supie queues on pgmq (Supabase Queues): functions that let Python reach the
-- queues over PostgREST, plus a dead-letter table. Written by
-- `supie queue migration`; safe to run again.
--
-- Only the service role may call these: they run as their owner (security
-- definer) to reach the pgmq schema, which PostgREST doesn't expose.

create extension if not exists pgmq;

-- Messages that failed every attempt, with the last error, until replayed.
create table if not exists public.supie_queue_dead (
  id bigint generated always as identity primary key,
  queue text not null,
  msg_id bigint not null,
  message jsonb,
  error text,
  attempts integer not null,
  enqueued_at timestamptz,
  failed_at timestamptz not null default now()
);
create index if not exists supie_queue_dead_queue on public.supie_queue_dead (queue, failed_at);
alter table public.supie_queue_dead enable row level security;
revoke all on public.supie_queue_dead from anon, authenticated;
grant select, insert, delete on public.supie_queue_dead to service_role;

-- Idempotency keys for sends: a key is enqueued once per queue; sending it
-- again returns the first message's ID. Delete old keys on a schedule, e.g.
-- delete from public.supie_queue_keys where created_at < now() - interval '7 days'.
create table if not exists public.supie_queue_keys (
  queue text not null,
  key text not null,
  msg_id bigint,
  created_at timestamptz not null default now(),
  primary key (queue, key)
);
alter table public.supie_queue_keys enable row level security;
revoke all on public.supie_queue_keys from anon, authenticated;
grant select, insert, update, delete on public.supie_queue_keys to service_role;

-- Effects a consumer ran once (ctx.once): running under a lease, or done with
-- their result. Delete old rows on a schedule, as for the keys.
create table if not exists public.supie_queue_once (
  queue text not null,
  key text not null,
  status text not null check (status in ('running', 'done')),
  result jsonb,
  lease_until timestamptz,
  updated_at timestamptz not null default now(),
  primary key (queue, key)
);
alter table public.supie_queue_once enable row level security;
revoke all on public.supie_queue_once from anon, authenticated;
grant select, insert, update, delete on public.supie_queue_once to service_role;

-- Create a queue if it doesn't exist. pgmq.create alone isn't safe when new
-- workers call it at the same moment (concurrent DDL collides), so creation
-- is serialized per queue name and skipped once the queue exists.
create or replace function public.supie_queue_create(queue text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform pg_advisory_xact_lock(hashtext('supie_queue:' || queue));
  if not exists (select 1 from pgmq.meta as m where m.queue_name = queue) then
    perform pgmq.create(queue);
  end if;
end
$$;

-- Send messages, after delay seconds; returns their IDs. keys, when given, is a
-- JSON array as long as messages, with a key or null for each: a key seen
-- before isn't sent again, and its first message's ID is returned.
drop function if exists public.supie_queue_send(text, jsonb, integer);
create or replace function public.supie_queue_send(
  queue text, messages jsonb, delay integer default 0, keys jsonb default null
)
returns bigint[]
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ids bigint[] := '{}';
  v_key text;
  v_id bigint;
  v_new boolean;
begin
  for i in 0 .. jsonb_array_length(messages) - 1 loop
    v_key := keys ->> i;
    if v_key is null then
      select pgmq.send(queue, messages -> i, delay) into v_id;
    else
      -- the unique key makes a concurrent send with the same key wait here,
      -- then find this row instead of sending a second message
      insert into public.supie_queue_keys (queue, key) values (queue, v_key)
      on conflict do nothing
      returning true into v_new;
      if v_new then
        select pgmq.send(queue, messages -> i, delay) into v_id;
        update public.supie_queue_keys as k set msg_id = v_id
        where k.queue = supie_queue_send.queue and k.key = v_key;
      else
        select k.msg_id into v_id from public.supie_queue_keys as k
        where k.queue = supie_queue_send.queue and k.key = v_key;
      end if;
    end if;
    v_ids := v_ids || v_id;
  end loop;
  return v_ids;
end
$$;

-- Start an effect for ctx.once: 'run' when this caller should run it (now
-- holding a lease), 'done' with the stored result, or 'busy' while another
-- caller's lease is live.
create or replace function public.supie_queue_once_begin(
  queue text, key text, lease_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.supie_queue_once;
begin
  insert into public.supie_queue_once (queue, key, status, lease_until)
  values (queue, key, 'running', now() + make_interval(secs => lease_seconds))
  on conflict do nothing
  returning * into v_row;
  if v_row.key is not null then
    return jsonb_build_object('state', 'run');
  end if;
  select * into v_row from public.supie_queue_once as o
  where o.queue = supie_queue_once_begin.queue and o.key = supie_queue_once_begin.key
  for update;
  if v_row.status = 'done' then
    return jsonb_build_object('state', 'done', 'result', v_row.result);
  end if;
  if v_row.lease_until > now() then
    return jsonb_build_object('state', 'busy');
  end if;
  -- the last runner's lease ran out (it died, or released it): take over
  update public.supie_queue_once as o
  set lease_until = now() + make_interval(secs => lease_seconds), updated_at = now()
  where o.queue = supie_queue_once_begin.queue and o.key = supie_queue_once_begin.key;
  return jsonb_build_object('state', 'run');
end
$$;

-- The effect succeeded: keep its result for any repeat.
create or replace function public.supie_queue_once_finish(
  queue text, key text, result jsonb
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.supie_queue_once as o
  set status = 'done', result = supie_queue_once_finish.result, lease_until = null, updated_at = now()
  where o.queue = supie_queue_once_finish.queue and o.key = supie_queue_once_finish.key
$$;

-- The effect raised: release the lease so a retry can run it.
create or replace function public.supie_queue_once_release(queue text, key text)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.supie_queue_once as o
  set lease_until = now(), updated_at = now()
  where o.queue = supie_queue_once_release.queue
    and o.key = supie_queue_once_release.key
    and o.status = 'running'
$$;

-- Read up to qty messages, waiting up to max_poll_seconds for the first; each
-- stays hidden from other readers for visibility seconds.
create or replace function public.supie_queue_read(
  queue text,
  visibility integer default 30,
  qty integer default 10,
  max_poll_seconds integer default 1
)
returns setof jsonb
language sql
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'msg_id', m.msg_id,
    'attempt', m.read_ct,
    'enqueued_at', m.enqueued_at,
    'message', m.message
  )
  from pgmq.read_with_poll(queue, visibility, qty, max_poll_seconds, 100) as m
$$;

-- Done with a message.
create or replace function public.supie_queue_ack(queue text, msg_id bigint)
returns boolean
language sql
security definer
set search_path = ''
as $$
  select pgmq.delete(queue, msg_id)
$$;

-- A failed attempt: make the message visible again after delay seconds.
create or replace function public.supie_queue_retry(
  queue text, msg_id bigint, delay integer
)
returns void
language sql
security definer
set search_path = ''
as $$
  select from pgmq.set_vt(queue, msg_id, delay)
$$;

-- The last attempt failed: move the message to the dead-letter table.
create or replace function public.supie_queue_kill(
  queue text, msg_id bigint, error text
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dead_id bigint;
begin
  execute format(
    'insert into public.supie_queue_dead (queue, msg_id, message, error, attempts, enqueued_at)
     select $1, m.msg_id, m.message, $3, m.read_ct, m.enqueued_at
     from pgmq.%I as m where m.msg_id = $2
     returning id',
    'q_' || queue
  ) into v_dead_id using queue, msg_id, error;
  perform pgmq.delete(queue, msg_id);
  return v_dead_id;
end
$$;

-- Send a dead message back to its queue; returns its new message ID.
create or replace function public.supie_queue_replay(dead_id bigint)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dead public.supie_queue_dead;
  v_msg_id bigint;
begin
  delete from public.supie_queue_dead as d where d.id = dead_id returning * into v_dead;
  if v_dead.id is null then
    return null;
  end if;
  select pgmq.send(v_dead.queue, v_dead.message) into v_msg_id;
  return v_msg_id;
end
$$;

-- Queue length, oldest message age and the dead-letter count.
create or replace function public.supie_queue_stats(queue text)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'queue', queue,
    'length', m.queue_length,
    'oldest_seconds', m.oldest_msg_age_sec,
    'total_sent', m.total_messages,
    'dead', (select count(*) from public.supie_queue_dead as d where d.queue = supie_queue_stats.queue)
  )
  from pgmq.metrics(queue) as m
$$;

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'supie_queue_create(text)',
    'supie_queue_send(text, jsonb, integer, jsonb)',
    'supie_queue_once_begin(text, text, integer)',
    'supie_queue_once_finish(text, text, jsonb)',
    'supie_queue_once_release(text, text)',
    'supie_queue_read(text, integer, integer, integer)',
    'supie_queue_ack(text, bigint)',
    'supie_queue_retry(text, bigint, integer)',
    'supie_queue_kill(text, bigint, text)',
    'supie_queue_replay(bigint)',
    'supie_queue_stats(text)'
  ] loop
    execute format('revoke execute on function public.%s from public, anon, authenticated', fn);
    execute format('grant execute on function public.%s to service_role', fn);
  end loop;
end
$$;
