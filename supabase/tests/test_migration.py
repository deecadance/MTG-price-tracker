"""Checks the security rules of supabase/migrations/0001_init.sql for real.

How: start a throwaway PostgreSQL in a temporary folder, load a tiny fake
of Supabase's sign-in system (fake_supabase.sql), run the migration, then
pretend to be different visitors (not signed in, Alice, Bob, our own sync
job) and check what each one can and cannot do.

Nothing touches the internet or the real Supabase project. If PostgreSQL is
not installed on this computer, these tests are skipped (not failed).

Run from the repository folder:
    python3 -m unittest discover -s supabase/tests -v
Use another PostgreSQL by pointing PG_BIN at its "bin" folder.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATION = HERE.parent / "migrations" / "0001_init.sql"
FAKE_SUPABASE = HERE / "fake_supabase.sql"
PORT = "54329"


def find_postgres_bin():
    """The folder holding initdb/pg_ctl/psql, or None if PostgreSQL isn't installed."""
    candidates = [os.environ.get("PG_BIN")]
    on_path = shutil.which("pg_ctl")
    if on_path:
        candidates.append(str(Path(on_path).parent))
    for version in ("17", "16", "15"):
        candidates += [
            f"/opt/homebrew/opt/postgresql@{version}/bin",
            f"/usr/local/opt/postgresql@{version}/bin",
            f"/usr/lib/postgresql/{version}/bin",
        ]
    for folder in candidates:
        if folder and all((Path(folder) / tool).exists() for tool in ("initdb", "pg_ctl", "psql")):
            return Path(folder)
    return None


PG_BIN = find_postgres_bin()


class ThrowawayPostgres:
    """A private PostgreSQL server living in a temp folder, deleted afterwards."""

    def __init__(self, bin_dir):
        self.bin = bin_dir
        self.dir = Path(tempfile.mkdtemp(prefix="pgtest"))
        # PostgreSQL refuses to start on macOS without a plain locale.
        self.env = {**os.environ, "LC_ALL": "C", "LANG": "C"}

    def start(self):
        self._tool("initdb", "-D", str(self.dir / "data"), "-U", "postgres",
                   "--auth=trust", "-E", "UTF8", "--no-locale")
        # Only a private socket file in our temp folder: no network port.
        self._tool("pg_ctl", "-D", str(self.dir / "data"), "-l", str(self.dir / "log"), "-w",
                   "-o", f"-k {self.dir} -c listen_addresses='' -p {PORT}", "start")

    def stop(self):
        try:
            self._tool("pg_ctl", "-D", str(self.dir / "data"), "-m", "immediate", "stop")
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    def _tool(self, name, *args):
        subprocess.run([str(self.bin / name), *args], check=True, env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def run_file(self, path):
        return self.run(Path(path).read_text(encoding="utf-8"), wrap=False)

    def run(self, sql, role=None, user=None, setup="", wrap=True):
        """Run SQL as `role` (anon/authenticated/service_role), signed in as `user`.

        `setup` runs first as the database owner, inside the same
        transaction (so it is undone if the test statement fails).
        Returns (ok, output, error_text).
        """
        script = sql
        if wrap:
            lines = ["begin;", setup]
            if user:
                claims = json.dumps({"sub": user, "role": "authenticated"})
                lines.append(f"set local request.jwt.claims = '{claims}';")
            if role:
                lines.append(f"set local role {role};")
            lines += [sql.rstrip().rstrip(";") + ";", "commit;"]
            script = "\n".join(lines)
        result = subprocess.run(
            [str(self.bin / "psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-h", str(self.dir), "-p", PORT, "-U", "postgres", "-d", "postgres", "-f", "-"],
            input=script, capture_output=True, text=True, env=self.env,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


@unittest.skipIf(PG_BIN is None, "PostgreSQL is not installed here; SQL tests skipped")
class MigrationSecurityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = ThrowawayPostgres(PG_BIN)
        cls.db.start()
        for path in (FAKE_SUPABASE, MIGRATION):
            ok, _, error = cls.db.run_file(path)
            if not ok:
                cls.db.stop()
                raise AssertionError(f"{path.name} failed to run:\n{error}")

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    # ---------- helpers ----------

    def ok(self, sql, **kwargs):
        ok, output, error = self.db.run(sql, **kwargs)
        self.assertTrue(ok, f"expected success, got error:\n{error}\nSQL: {sql}")
        return output

    def fails(self, sql, expected_error, **kwargs):
        ok, _, error = self.db.run(sql, **kwargs)
        self.assertFalse(ok, f"expected an error, but it worked. SQL: {sql}")
        self.assertIn(expected_error, error)

    def new_user(self, last_sign_in="now()"):
        user = str(uuid.uuid4())
        self.ok(f"insert into auth.users (id, last_sign_in_at) values ('{user}', {last_sign_in})")
        return user

    def add_watch(self, user, item_id=1, metric="min_all", above=10000):
        return self.ok(
            "insert into public.watches (source, item_id, metric, above_cents) "
            f"values ('ct_sealed', {item_id}, '{metric}', {above}) returning id",
            role="authenticated", user=user)

    # ---------- the structure ----------

    def test_every_public_table_has_rls_switched_on(self):
        tables = self.ok("select string_agg(relname, ',' order by relname) from pg_class c "
                         "join pg_namespace n on n.oid = c.relnamespace "
                         "where n.nspname = 'public' and c.relkind in ('r', 'p')")
        self.assertEqual(tables, "alert_log,latest_prices,profiles,watches")
        without_rls = self.ok("select count(*) from pg_class c "
                              "join pg_namespace n on n.oid = c.relnamespace "
                              "where n.nspname = 'public' and c.relkind in ('r', 'p') "
                              "and not c.relrowsecurity")
        self.assertEqual(without_rls, "0")

    def test_no_views_that_could_skip_rls(self):
        self.assertEqual(self.ok("select count(*) from pg_class c join pg_namespace n "
                                 "on n.oid = c.relnamespace where n.nspname = 'public' "
                                 "and c.relkind in ('v', 'm')"), "0")

    def test_anon_may_only_select_latest_prices(self):
        rows = self.ok(
            "select t, p from unnest(array['latest_prices','profiles','watches','alert_log']) t, "
            "unnest(array['select','insert','update','delete']) p "
            "where has_table_privilege('anon', 'public.' || t, p) order by 1, 2")
        self.assertEqual(rows, "latest_prices|select")

    def test_nobody_but_the_right_role_can_run_functions(self):
        rows = self.ok(
            "select p.proname || ':' || r.rolname from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "cross join (values ('anon'), ('authenticated'), ('service_role')) r(rolname) "
            "where n.nspname = 'public' and has_function_privilege(r.rolname, p.oid, 'execute') "
            "order by 1")
        self.assertEqual(rows.splitlines(), [
            "create_telegram_link_token:authenticated",
            "delete_my_account:authenticated",
            "link_telegram_chat:service_role",
            "purge_inactive_accounts:service_role",
            "unlink_telegram_chat:service_role",
        ])

    # ---------- latest_prices ----------

    def test_prices_are_public_but_only_the_service_role_writes(self):
        self.ok("insert into public.latest_prices (source, item_id, foil, date, metrics, name) "
                "values ('ct_sealed', 777, false, '2026-11-17', '{\"min_all\": 4999}', 'Test Box') "
                "on conflict (source, item_id, foil) do update set metrics = excluded.metrics",
                role="service_role")
        self.assertEqual(self.ok("select name from public.latest_prices where item_id = 777",
                                 role="anon"), "Test Box")
        alice = self.new_user()
        self.assertEqual(self.ok("select count(*) from public.latest_prices where item_id = 777",
                                 role="authenticated", user=alice), "1")
        for role, user in (("anon", None), ("authenticated", alice)):
            self.fails("insert into public.latest_prices (source, item_id, date) "
                       "values ('ct_sealed', 778, '2026-11-17')",
                       "permission denied", role=role, user=user)
            self.fails("update public.latest_prices set name = 'hacked'",
                       "permission denied", role=role, user=user)
            self.fails("delete from public.latest_prices", "permission denied",
                       role=role, user=user)

    def test_anon_cannot_read_private_tables(self):
        for table in ("profiles", "watches", "alert_log"):
            self.fails(f"select * from public.{table}", "permission denied", role="anon")

    def test_sealed_prices_cannot_be_foil(self):
        self.fails("insert into public.latest_prices (source, item_id, foil, date) "
                   "values ('ct_sealed', 5, true, '2026-11-17')",
                   "violates check constraint", role="service_role")

    # ---------- profiles ----------

    def test_new_user_gets_a_profile_automatically(self):
        alice = self.new_user()
        self.assertEqual(self.ok(f"select count(*) from public.profiles where id = '{alice}'"), "1")

    def test_profiles_are_private_to_their_owner(self):
        alice, bob = self.new_user(), self.new_user()
        self.assertEqual(self.ok("select string_agg(id::text, ',') from public.profiles",
                                 role="authenticated", user=alice), alice)
        # Alice may not point alerts at an arbitrary Telegram chat...
        self.fails(f"update public.profiles set telegram_chat_id = 999 where id = '{alice}'",
                   "row-level security", role="authenticated", user=alice)
        # ...nor touch the link code or anything else...
        self.fails("update public.profiles set telegram_link_token = 'x'",
                   "permission denied", role="authenticated", user=alice)
        # ...but she may disconnect her own Telegram.
        self.ok(f"update public.profiles set telegram_chat_id = 555 where id = '{alice}'",
                role="service_role")
        changed = self.ok("with c as (update public.profiles set telegram_chat_id = null "
                          "returning 1) select count(*) from c", role="authenticated", user=alice)
        self.assertEqual(changed, "1")
        # Bob's profile is invisible to Alice: updating it changes nothing.
        self.ok(f"update public.profiles set telegram_chat_id = 556 where id = '{bob}'",
                role="service_role")
        changed = self.ok(f"with c as (update public.profiles set telegram_chat_id = null "
                          f"where id = '{bob}' returning 1) select count(*) from c",
                          role="authenticated", user=alice)
        self.assertEqual(changed, "0")
        self.assertEqual(self.ok(f"select telegram_chat_id from public.profiles where id = '{bob}'"),
                         "556")

    # ---------- watches ----------

    def test_user_id_is_always_the_signed_in_user(self):
        alice, bob = self.new_user(), self.new_user()
        owner = self.ok("insert into public.watches (source, item_id, metric, above_cents) "
                        "values ('ct_sealed', 1, 'min_all', 100) returning user_id",
                        role="authenticated", user=alice)
        self.assertEqual(owner, alice)
        # Layer 1: the website may not even name the user_id column.
        self.fails("insert into public.watches (user_id, source, item_id, metric, above_cents) "
                   f"values ('{bob}', 'ct_sealed', 2, 'min_all', 100)",
                   "permission denied", role="authenticated", user=alice)
        # Layer 2: even if that column were allowed, the RLS policy refuses.
        self.fails("insert into public.watches (user_id, source, item_id, metric, above_cents) "
                   f"values ('{bob}', 'ct_sealed', 2, 'min_all', 100)",
                   "row-level security", role="authenticated", user=alice,
                   setup="grant insert (user_id) on public.watches to authenticated;")

    def test_users_cannot_see_or_change_each_others_watches(self):
        alice, bob = self.new_user(), self.new_user()
        watch = self.add_watch(alice)
        self.assertEqual(self.ok("select count(*) from public.watches",
                                 role="authenticated", user=bob), "0")
        self.assertEqual(self.ok(f"with c as (update public.watches set above_cents = 1 "
                                 f"where id = {watch} returning 1) select count(*) from c",
                                 role="authenticated", user=bob), "0")
        self.assertEqual(self.ok(f"with c as (delete from public.watches where id = {watch} "
                                 f"returning 1) select count(*) from c",
                                 role="authenticated", user=bob), "0")
        self.assertEqual(self.ok(f"select above_cents from public.watches where id = {watch}"),
                         "10000")
        # Alice herself can change and delete it.
        self.ok(f"update public.watches set above_cents = 20000 where id = {watch}",
                role="authenticated", user=alice)
        self.assertEqual(self.ok(f"with c as (delete from public.watches where id = {watch} "
                                 f"returning 1) select count(*) from c",
                                 role="authenticated", user=alice), "1")

    def test_users_cannot_change_owner_or_alert_states(self):
        alice, bob = self.new_user(), self.new_user()
        watch = self.add_watch(alice)
        self.fails(f"update public.watches set above_state = 'armed' where id = {watch}",
                   "permission denied", role="authenticated", user=alice)
        self.fails(f"update public.watches set user_id = '{bob}' where id = {watch}",
                   "permission denied", role="authenticated", user=alice)

    def test_changing_a_threshold_rearms_that_direction(self):
        alice = self.new_user()
        watch = self.add_watch(alice)
        self.ok(f"update public.watches set above_state = 'fired', below_state = 'fired' "
                f"where id = {watch}", role="service_role")
        self.ok(f"update public.watches set above_cents = 15000 where id = {watch}",
                role="authenticated", user=alice)
        self.assertEqual(self.ok(f"select above_state || ',' || below_state "
                                 f"from public.watches where id = {watch}"), "armed,fired")

    def test_at_most_50_watches_per_user(self):
        alice, bob = self.new_user(), self.new_user()
        self.ok("insert into public.watches (source, item_id, metric, above_cents) "
                "select 'ct_sealed', g, 'min_all', 100 from generate_series(1, 50) g",
                role="authenticated", user=alice)
        self.fails("insert into public.watches (source, item_id, metric, above_cents) "
                   "values ('ct_sealed', 51, 'min_all', 100)",
                   "at most 50", role="authenticated", user=alice)
        self.add_watch(bob, item_id=51)  # other users are not affected

    def test_watches_must_make_sense(self):
        alice = self.new_user()
        bad_inserts = {
            "no threshold": "('ct_sealed', 1, false, 'min_all', null, null)",
            "foil sealed": "('ct_sealed', 1, true, 'min_all', 100, null)",
            "card metric on sealed": "('ct_sealed', 1, false, 'ref_nm', 100, null)",
            "sealed metric on card": "('ct_card', 1, false, 'min_all', 100, null)",
            "zero threshold": "('ct_card', 1, false, 'ref_nm', 0, null)",
            "unknown source": "('cm_card', 1, false, 'ref_nm', 100, null)",
        }
        for label, values in bad_inserts.items():
            with self.subTest(label):
                self.fails("insert into public.watches "
                           "(source, item_id, foil, metric, above_cents, below_cents) "
                           f"values {values}", "violates check constraint",
                           role="authenticated", user=alice)
        self.ok("insert into public.watches (source, item_id, foil, metric, below_cents) "
                "values ('ct_card', 9, true, 'ref_nm', 500)", role="authenticated", user=alice)
        self.fails("insert into public.watches (source, item_id, foil, metric, below_cents) "
                   "values ('ct_card', 9, true, 'ref_nm', 400)", "duplicate key",
                   role="authenticated", user=alice)

    # ---------- Telegram linking ----------

    def test_telegram_link_code_flow(self):
        alice = self.new_user()
        token = self.ok("select public.create_telegram_link_token()",
                        role="authenticated", user=alice)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        minutes = self.ok(f"select round(extract(epoch from telegram_link_expires - now()) / 60) "
                          f"from public.profiles where id = '{alice}'")
        self.assertEqual(minutes, "15")
        # A signed-in user cannot link a chat themselves; only the bot can.
        self.fails(f"select public.link_telegram_chat('{token}', 4242)",
                   "permission denied", role="authenticated", user=alice)
        self.assertEqual(self.ok(f"select public.link_telegram_chat('{token}', 4242)",
                                 role="service_role"), "t")
        self.assertEqual(self.ok(f"select coalesce(telegram_link_token, 'used') || ',' || "
                                 f"telegram_chat_id from public.profiles where id = '{alice}'"),
                         "used,4242")
        # The code works only once.
        self.assertEqual(self.ok(f"select public.link_telegram_chat('{token}', 9999)",
                                 role="service_role"), "f")
        # /stop disconnects.
        self.fails("select public.unlink_telegram_chat(4242)", "permission denied",
                   role="authenticated", user=alice)
        self.assertEqual(self.ok("select public.unlink_telegram_chat(4242)",
                                 role="service_role"), "1")

    def test_expired_link_code_is_refused(self):
        alice = self.new_user()
        self.ok(f"update public.profiles set telegram_link_token = 'oldcode', "
                f"telegram_link_expires = now() - interval '1 minute' where id = '{alice}'")
        self.assertEqual(self.ok("select public.link_telegram_chat('oldcode', 1)",
                                 role="service_role"), "f")

    def test_anon_cannot_create_link_codes(self):
        self.fails("select public.create_telegram_link_token()", "permission denied", role="anon")

    # ---------- alert log ----------

    def test_alert_log_is_read_only_for_users(self):
        alice, bob = self.new_user(), self.new_user()
        watch = self.add_watch(alice)
        self.fails(f"insert into public.alert_log (user_id, watch_id, direction, value_cents, "
                   f"snapshot_date) values ('{alice}', {watch}, 'above', 1, '2026-11-17')",
                   "permission denied", role="authenticated", user=alice)
        self.ok(f"insert into public.alert_log (user_id, watch_id, direction, value_cents, "
                f"snapshot_date) values ('{alice}', {watch}, 'above', 12000, '2026-11-17')",
                role="service_role")
        self.assertEqual(self.ok("select count(*) from public.alert_log",
                                 role="authenticated", user=alice), "1")
        self.assertEqual(self.ok("select count(*) from public.alert_log",
                                 role="authenticated", user=bob), "0")
        self.fails("delete from public.alert_log", "permission denied",
                   role="authenticated", user=alice)
        # Deleting the watch keeps the log row (until it expires).
        self.ok(f"delete from public.watches where id = {watch}", role="authenticated", user=alice)
        self.assertEqual(self.ok(f"select count(*) from public.alert_log where user_id = '{alice}' "
                                 f"and watch_id is null"), "1")

    def test_service_role_can_do_everything_send_alerts_needs(self):
        alice = self.new_user()
        watch = self.add_watch(alice)
        self.ok(f"update public.profiles set telegram_chat_id = 31337 where id = '{alice}'",
                role="service_role")
        self.assertEqual(self.ok("select count(*) > 0 from public.profiles "
                                 "where telegram_chat_id is not null", role="service_role"), "t")
        self.assertEqual(self.ok("select count(*) > 0 from public.watches", role="service_role"),
                         "t")
        self.ok(f"update public.watches set above_state = 'fired' where id = {watch} "
                f"and above_cents = 10000 and below_cents is null", role="service_role")
        self.ok(f"insert into public.alert_log (user_id, watch_id, direction, value_cents, "
                f"snapshot_date, sent_at) values ('{alice}', {watch}, 'above', 1, '2026-01-01', "
                f"now() - interval '91 days')", role="service_role")
        removed = self.ok("with d as (delete from public.alert_log where sent_at < "
                          "now() - interval '90 days' returning 1) select count(*) from d",
                          role="service_role")
        self.assertEqual(removed, "1")

    # ---------- deleting accounts ----------

    def test_delete_my_account_removes_everything_of_that_user_only(self):
        carol, dave = self.new_user(), self.new_user()
        for user in (carol, dave):
            watch = self.add_watch(user)
            self.ok(f"insert into public.alert_log (user_id, watch_id, direction, value_cents, "
                    f"snapshot_date) values ('{user}', {watch}, 'above', 1, '2026-11-17')",
                    role="service_role")
        self.fails("select public.delete_my_account()", "permission denied", role="anon")
        self.fails("select public.delete_my_account()", "Please sign in first",
                   role="authenticated")
        self.ok("select public.delete_my_account()", role="authenticated", user=carol)
        for table, column in (("auth.users", "id"), ("public.profiles", "id"),
                              ("public.watches", "user_id"), ("public.alert_log", "user_id")):
            self.assertEqual(self.ok(f"select count(*) from {table} where {column} = '{carol}'"),
                             "0", table)
            self.assertEqual(self.ok(f"select count(*) from {table} where {column} = '{dave}'"),
                             "1", table)

    def test_accounts_unused_for_24_months_are_purged(self):
        old = self.new_user(last_sign_in="now() - interval '25 months'")
        recent = self.new_user(last_sign_in="now() - interval '23 months'")
        self.fails("select public.purge_inactive_accounts()", "permission denied",
                   role="authenticated", user=recent)
        self.ok("select public.purge_inactive_accounts()", role="service_role")
        self.assertEqual(self.ok(f"select count(*) from auth.users where id = '{old}'"), "0")
        self.assertEqual(self.ok(f"select count(*) from auth.users where id = '{recent}'"), "1")


if __name__ == "__main__":
    unittest.main()
