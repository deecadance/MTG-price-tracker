-- =====================================================================
-- MTG price tracker — user accounts database (Supabase), version 1
-- =====================================================================
--
-- What this file does
--   Creates the four tables behind user accounts and price alerts:
--     latest_prices  today's and yesterday's price of every tracked item
--                    (public: anyone may read it, only our sync job writes)
--     profiles       one row per user: their Telegram chat, if connected
--     watches        "tell me when X goes above/below €Y" (max 50 per user)
--     alert_log      which alerts were sent (kept 90 days)
--   ...and the security rules that decide who may read or change what.
--
-- How to run it
--   Supabase dashboard -> SQL Editor -> New query -> paste this whole file
--   -> Run. Run it ONCE, on a new project (see SETUP_ACCOUNTS.md).
--
-- The three kinds of visitor Supabase knows about
--   anon           someone who is not signed in (uses the public "anon" key)
--   authenticated  a signed-in user (Supabase knows who: auth.uid())
--   service_role   our own trusted code (the sync job and the two Edge
--                  Functions). It uses the SECRET service-role key and
--                  skips the row rules below, so that key must never
--                  appear in the website or in any public file.
--
-- Two layers of protection are used on every table:
--   1. GRANTs say which kinds of visitor may touch a table/column at all.
--      Supabase normally grants everything to anon/authenticated and relies
--      on layer 2, so we first take everything away and grant back only
--      what is needed.
--   2. Row Level Security (RLS) policies say WHICH ROWS they may touch
--      (e.g. "only rows where user_id is you"). Tables created with SQL do
--      NOT get RLS automatically, so it is switched on explicitly below.
-- No views are created: views skip RLS by default.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. Tables
-- ---------------------------------------------------------------------

-- Latest prices. One row per item (see contract C3):
--   source  'ct_card' (a single card) or 'ct_sealed' (a sealed product)
--   item_id the CardTrader blueprint id
--   foil    true/false (always false for sealed products)
--   metrics      {"metric name": price in euro cents} on `date`
--   prev_metrics the same for the previous available snapshot (or null)
create table public.latest_prices (
  source       text    not null check (source in ('ct_card', 'ct_sealed')),
  item_id      bigint  not null check (item_id > 0),
  foil         boolean not null default false,
  date         date    not null,
  metrics      jsonb   not null default '{}'::jsonb
               check (jsonb_typeof(metrics) = 'object'),
  prev_metrics jsonb
               check (prev_metrics is null or jsonb_typeof(prev_metrics) = 'object'),
  name         text    not null default '',
  primary key (source, item_id, foil),
  check (source = 'ct_card' or foil = false)
);
comment on table public.latest_prices is
  'Latest and previous snapshot of every tracked item. Public read; written only by the sync job (service role).';

-- One profile per user, created automatically when they first sign in.
-- If the user is deleted, their profile goes too ("on delete cascade").
create table public.profiles (
  id                    uuid primary key references auth.users (id) on delete cascade,
  telegram_chat_id      bigint,
  telegram_link_token   text unique,
  telegram_link_expires timestamptz,
  created_at            timestamptz not null default now()
);
comment on table public.profiles is
  'One row per user. telegram_chat_id is set only by the Telegram bot after the user opens a link from their account page.';

-- What each user follows. user_id is filled in by the database itself
-- (DEFAULT auth.uid()) — the website never chooses it.
create table public.watches (
  id          bigserial primary key,
  user_id     uuid    not null default auth.uid()
              references auth.users (id) on delete cascade,
  source      text    not null check (source in ('ct_card', 'ct_sealed')),
  item_id     bigint  not null check (item_id > 0),
  foil        boolean not null default false,
  metric      text    not null,
  above_cents integer check (above_cents > 0),
  below_cents integer check (below_cents > 0),
  above_state text    not null default 'armed' check (above_state in ('armed', 'fired')),
  below_state text    not null default 'armed' check (below_state in ('armed', 'fired')),
  created_at  timestamptz not null default now(),
  -- A watch needs at least one threshold, otherwise it can never alert.
  constraint watches_need_a_threshold
    check (above_cents is not null or below_cents is not null),
  -- The metric must exist for that kind of item (contract C3), and
  -- sealed products are never foil.
  constraint watches_metric_matches_source check (
    (source = 'ct_sealed' and foil = false
       and metric in ('min_all', 'avg5_all', 'min_zero', 'avg5_zero'))
    or
    (source = 'ct_card'
       and metric in ('buy_now_nm', 'ref_nm', 'buy_now_nm_zero', 'ref_nm_zero'))
  ),
  -- The same item + metric only once per user (no duplicate messages).
  constraint watches_one_per_item_metric unique (user_id, source, item_id, foil, metric)
);
create index watches_item_idx on public.watches (source, item_id, foil);
comment on table public.watches is
  'Price alerts chosen by users. At most 50 per user (enforced by a trigger).';

-- Every alert actually sent. Old rows are deleted after 90 days by the
-- send-alerts function. If the user deletes a watch, its log rows stay
-- (watch_id becomes empty) until they expire.
create table public.alert_log (
  id            bigserial primary key,
  user_id       uuid    not null references auth.users (id) on delete cascade,
  watch_id      bigint  references public.watches (id) on delete set null,
  direction     text    not null check (direction in ('above', 'below')),
  value_cents   integer not null,
  snapshot_date date    not null,
  sent_at       timestamptz not null default now()
);
create index alert_log_user_idx on public.alert_log (user_id);
create index alert_log_sent_at_idx on public.alert_log (sent_at);
comment on table public.alert_log is
  'Alerts that were sent. Kept 90 days. Users can read only their own rows.';


-- ---------------------------------------------------------------------
-- 2. Switch on Row Level Security for EVERY table
-- ---------------------------------------------------------------------
alter table public.latest_prices enable row level security;
alter table public.profiles      enable row level security;
alter table public.watches       enable row level security;
alter table public.alert_log     enable row level security;


-- ---------------------------------------------------------------------
-- 3. Privileges: take away Supabase's broad defaults, grant back the minimum
-- ---------------------------------------------------------------------
revoke all on table public.latest_prices, public.profiles,
                    public.watches, public.alert_log
  from public, anon, authenticated;
revoke all on sequence public.watches_id_seq, public.alert_log_id_seq
  from public, anon, authenticated;

-- latest_prices: everyone may read; only the service role may write.
grant select on public.latest_prices to anon, authenticated;
grant select, insert, update, delete on public.latest_prices to service_role;

-- profiles: a user may read their own row, and may only clear their
-- Telegram chat ("disconnect"). Linking happens through the bot.
grant select on public.profiles to authenticated;
grant update (telegram_chat_id) on public.profiles to authenticated;
grant select, insert, update, delete on public.profiles to service_role;

-- watches: users may read, add, change thresholds and delete. They can
-- NOT write user_id or the armed/fired states (those columns are left out).
grant select, delete on public.watches to authenticated;
grant insert (source, item_id, foil, metric, above_cents, below_cents)
  on public.watches to authenticated;
grant update (above_cents, below_cents) on public.watches to authenticated;
grant usage on sequence public.watches_id_seq to authenticated;
grant select, insert, update, delete on public.watches to service_role;
grant usage, select on sequence public.watches_id_seq to service_role;

-- alert_log: users may only read; only the service role may add/delete.
grant select on public.alert_log to authenticated;
grant select, insert, delete on public.alert_log to service_role;
grant usage, select on sequence public.alert_log_id_seq to service_role;


-- ---------------------------------------------------------------------
-- 4. Row Level Security policies (which ROWS each visitor may touch)
-- ---------------------------------------------------------------------
-- "(select auth.uid())" is the id of the signed-in user. Writing it inside
-- "select" lets Postgres compute it once per query instead of once per row.
-- The service role skips RLS anyway; its policies are written out only so
-- the intent is visible in the dashboard.

-- latest_prices
create policy "Anyone can read prices"
  on public.latest_prices for select
  to anon, authenticated
  using (true);
create policy "Only the sync job writes prices"
  on public.latest_prices for all
  to service_role
  using (true) with check (true);

-- profiles
create policy "Users read their own profile"
  on public.profiles for select
  to authenticated
  using (id = (select auth.uid()));
create policy "Users can only disconnect their own Telegram"
  on public.profiles for update
  to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()) and telegram_chat_id is null);
create policy "Service role manages profiles"
  on public.profiles for all
  to service_role
  using (true) with check (true);

-- watches
create policy "Users read their own watches"
  on public.watches for select
  to authenticated
  using (user_id = (select auth.uid()));
create policy "Users add watches only for themselves"
  on public.watches for insert
  to authenticated
  with check (user_id = (select auth.uid()));
create policy "Users change only their own watches"
  on public.watches for update
  to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));
create policy "Users delete only their own watches"
  on public.watches for delete
  to authenticated
  using (user_id = (select auth.uid()));
create policy "Service role manages watches"
  on public.watches for all
  to service_role
  using (true) with check (true);

-- alert_log
create policy "Users read their own alert log"
  on public.alert_log for select
  to authenticated
  using (user_id = (select auth.uid()));
create policy "Only the service role writes the alert log"
  on public.alert_log for insert
  to service_role
  with check (true);
create policy "Service role reads the alert log"
  on public.alert_log for select
  to service_role
  using (true);
create policy "Service role deletes old alert log rows"
  on public.alert_log for delete
  to service_role
  using (true);


-- ---------------------------------------------------------------------
-- 5. Automatic rules (triggers)
-- ---------------------------------------------------------------------

-- 5a. Give every new user an empty profile row.
--     SECURITY DEFINER = runs with the rights of the function's owner,
--     because the sign-in system (not the user) creates auth.users rows.
--     "set search_path = ''" makes every name below fully spelled out, so
--     nobody can trick the function with look-alike tables.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id) values (new.id)
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Users who already existed before this file was run also get a profile.
insert into public.profiles (id)
  select id from auth.users
  on conflict (id) do nothing;

-- 5b. At most 50 watches per user.
--     Runs as the user (no SECURITY DEFINER needed): RLS lets them count
--     exactly their own rows. The lock stops two simultaneous inserts from
--     both sneaking in as number 50.
create function public.enforce_watch_limit()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('watches:' || new.user_id::text, 0));
  if (select count(*) from public.watches w where w.user_id = new.user_id) >= 50 then
    raise exception 'You can follow at most 50 items. Remove one before adding another.';
  end if;
  return new;
end;
$$;

create trigger watches_max_50_per_user
  before insert on public.watches
  for each row execute function public.enforce_watch_limit();

-- 5c. When a user changes a threshold, that direction starts fresh
--     ("armed"), so the new threshold can alert.
create function public.rearm_on_threshold_change()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.above_cents is distinct from old.above_cents then
    new.above_state := 'armed';
  end if;
  if new.below_cents is distinct from old.below_cents then
    new.below_state := 'armed';
  end if;
  return new;
end;
$$;

create trigger watches_rearm_on_threshold_change
  before update of above_cents, below_cents on public.watches
  for each row execute function public.rearm_on_threshold_change();


-- ---------------------------------------------------------------------
-- 6. Functions the website may call (signed-in users only)
-- ---------------------------------------------------------------------

-- 6a. "Connect Telegram": make a one-time code for the signed-in user.
--     It is random (122 random bits), works for 15 minutes, and replaces
--     any older code. The website then opens t.me/<bot>?start=<code>.
create function public.create_telegram_link_token()
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  me    uuid := auth.uid();
  token text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text, '-', '');
begin
  if me is null then
    raise exception 'Please sign in first.';
  end if;
  insert into public.profiles (id, telegram_link_token, telegram_link_expires)
  values (me, token, pg_catalog.now() + interval '15 minutes')
  on conflict (id) do update
    set telegram_link_token   = excluded.telegram_link_token,
        telegram_link_expires = excluded.telegram_link_expires;
  return token;
end;
$$;

-- 6b. "Delete my account": removes the signed-in user from Supabase's
--     sign-in system; their profile, watches and alert log disappear with
--     it (on delete cascade). Cannot be undone.
create function public.delete_my_account()
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  me uuid := auth.uid();
begin
  if me is null then
    raise exception 'Please sign in first.';
  end if;
  delete from auth.users where id = me;
end;
$$;


-- ---------------------------------------------------------------------
-- 7. Functions only our own code may call (service role)
-- ---------------------------------------------------------------------

-- 7a. Used by the Telegram bot when someone sends "/start <code>":
--     if the code exists and has not expired, remember this chat for that
--     user and use up the code. Returns true when it worked.
create function public.link_telegram_chat(p_token text, p_chat_id bigint)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  linked integer;
begin
  update public.profiles
     set telegram_chat_id      = p_chat_id,
         telegram_link_token   = null,
         telegram_link_expires = null
   where telegram_link_token = p_token
     and telegram_link_expires > pg_catalog.now();
  get diagnostics linked = row_count;
  return linked > 0;
end;
$$;

-- 7b. Used by the Telegram bot when someone sends "/stop".
--     Returns how many accounts were disconnected from that chat.
create function public.unlink_telegram_chat(p_chat_id bigint)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  unlinked integer;
begin
  update public.profiles
     set telegram_chat_id = null
   where telegram_chat_id = p_chat_id;
  get diagnostics unlinked = row_count;
  return unlinked;
end;
$$;

-- 7c. Privacy promise: accounts not signed into for 24 months are deleted
--     (with everything attached to them). Called daily by send-alerts.
create function public.purge_inactive_accounts()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  removed integer;
begin
  delete from auth.users
   where coalesce(last_sign_in_at, created_at) < pg_catalog.now() - interval '24 months';
  get diagnostics removed = row_count;
  return removed;
end;
$$;


-- ---------------------------------------------------------------------
-- 8. Who may call which function
-- ---------------------------------------------------------------------
-- Postgres lets EVERYONE ("public") run a new function by default, and
-- Supabase also grants anon/authenticated/service_role. Take that away from
-- all of them, then grant back exactly what is needed.
revoke execute on function
  public.handle_new_user(),
  public.enforce_watch_limit(),
  public.rearm_on_threshold_change(),
  public.create_telegram_link_token(),
  public.delete_my_account(),
  public.link_telegram_chat(text, bigint),
  public.unlink_telegram_chat(bigint),
  public.purge_inactive_accounts()
  from public, anon, authenticated, service_role;

grant execute on function
  public.create_telegram_link_token(),
  public.delete_my_account()
  to authenticated;

grant execute on function
  public.link_telegram_chat(text, bigint),
  public.unlink_telegram_chat(bigint),
  public.purge_inactive_accounts()
  to service_role;

-- (The three trigger functions need no grant: PostgreSQL checks that right
-- only when a trigger is created, not each time it fires.)
