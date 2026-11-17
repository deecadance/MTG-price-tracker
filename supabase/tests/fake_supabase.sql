-- A tiny stand-in for the parts of Supabase that 0001_init.sql relies on,
-- so the migration can be tested on a plain local PostgreSQL (no Supabase,
-- no internet). Used only by test_migration.py — never run this on Supabase.
--
-- It copies Supabase's behaviour where it matters for security:
--   * the roles anon / authenticated / service_role (service_role skips RLS)
--   * auth.users and auth.uid() (who is signed in, read from the JWT claims)
--   * Supabase's generous default grants on new tables, sequences and
--     functions in "public" (the migration must take these away itself)

create role anon nologin noinherit;
create role authenticated nologin noinherit;
create role service_role nologin noinherit bypassrls;

create schema auth;

create table auth.users (
  id              uuid primary key default gen_random_uuid(),
  email           text,
  created_at      timestamptz not null default now(),
  last_sign_in_at timestamptz
);

-- Same definition Supabase uses: the "sub" claim of the caller's JWT.
create function auth.uid() returns uuid
language sql stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables    to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
