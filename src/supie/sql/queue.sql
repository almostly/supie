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

-- Create a queue if it doesn't exist (pgmq.create is idempotent).
create or replace function public.supie_queue_create(queue text)
returns void
language sql
security definer
set search_path = ''
as $$
  select pgmq.create(queue)
$$;

-- Send messages, after delay seconds; returns their IDs.
create or replace function public.supie_queue_send(
  queue text, messages jsonb, delay integer default 0
)
returns bigint[]
language sql
security definer
set search_path = ''
as $$
  select coalesce(array_agg(id), '{}')
  from pgmq.send_batch(
    queue,
    array(select jsonb_array_elements(messages)),
    delay
  ) as id
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
    'supie_queue_send(text, jsonb, integer)',
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
