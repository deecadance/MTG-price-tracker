"""Checks the send-alerts function's logic: the alert rule (contract C4),
the message text, and one whole run against a fake database and Telegram.

The rule examples live in c4_cases.json. The checks:
  1. A short Python copy of the rule, written straight from the contract,
     agrees with every example — so the examples themselves are right.
  2. The real JavaScript (functions/send-alerts/alert_rule.js and
     alert_job.js) gives the same answers. This uses "jsc", the JavaScript
     engine built into every Mac; on computers without it, these are skipped.

Run from the repository folder:
    python3 -m unittest discover -s supabase/tests -v
"""

import json
import unittest
from pathlib import Path

from js_runner import JSC, RunsJavaScript

CASES = json.loads((Path(__file__).resolve().parent / "c4_cases.json").read_text(encoding="utf-8"))


def reference_rule(direction, threshold, state, latest, previous):
    """Contract C4 in Python, for checking the examples. Returns (state, fired)."""
    if threshold is None or latest is None or previous is None:
        return state, False
    if direction == "above":
        if state == "armed" and latest >= threshold and previous >= threshold:
            return "fired", True
        if state == "fired" and latest * 100 < threshold * 98:  # i.e. < 98%
            return "armed", False
        return state, False
    if state == "armed" and latest <= threshold and previous <= threshold:
        return "fired", True
    if state == "fired" and latest * 100 > threshold * 102:  # i.e. > 102%
        return "armed", False
    return state, False


class ExamplesMatchTheContract(unittest.TestCase):
    def test_python_reference_agrees_with_every_example(self):
        for case in CASES["direction_cases"]:
            with self.subTest(case["note"]):
                got = reference_rule(case["direction"], case["threshold"], case["state"],
                                     case["latest"], case["previous"])
                self.assertEqual(got, (case["expect_state"], case["expect_fired"]))


@unittest.skipIf(JSC is None, "no JavaScript engine (jsc) on this computer")
class JavaScriptRuleMatchesExamples(RunsJavaScript, unittest.TestCase):
    IMPORTS = {"rule": "send-alerts/alert_rule.js"}

    def run_js(self, body):
        return super().run_js(f"const CASES = {json.dumps(CASES)};\n" + body)

    def test_direction_rule(self):
        results = self.run_js(
            "print(JSON.stringify(CASES.direction_cases.map(c => "
            "rule.evaluateDirection(c.direction, c.threshold, c.state, c.latest, c.previous))));")
        for case, got in zip(CASES["direction_cases"], results, strict=True):
            with self.subTest(case["note"]):
                self.assertEqual((got["state"], got["fired"]),
                                 (case["expect_state"], case["expect_fired"]))

    def test_messages(self):
        results = self.run_js(
            "print(JSON.stringify(CASES.message_cases.map(c => rule.formatMessage(c.alert))));")
        for case, got in zip(CASES["message_cases"], results, strict=True):
            self.assertEqual(got, case["expect"])

    def test_whole_watch_uses_the_watched_metric_and_both_directions(self):
        result = self.run_js("""
            const watch = {metric: "min_all", above_cents: 1000, below_cents: 5000,
                           above_state: "armed", below_state: "fired"};
            print(JSON.stringify([
              rule.evaluateWatch(watch, {min_all: 1200, avg5_all: 1}, {min_all: 1100}),
              rule.evaluateWatch(watch, {min_all: 6000}, null),
              rule.evaluateWatch(watch, null, null),
            ]));""")
        self.assertEqual(result[0], {"above_state": "fired", "below_state": "fired",
                                     "fired": ["above"], "value": 1200})
        # No previous snapshot / no data: nothing changes.
        self.assertEqual(result[1], {"above_state": "armed", "below_state": "fired",
                                     "fired": [], "value": 6000})
        self.assertEqual(result[2]["fired"], [])


# A fake database for alert_job.js: keeps rows in memory, records every
# call, and understands just the few requests the job makes.
FAKE_DB_JS = """
function fakeDb(data) {
  const calls = [];
  return {
    calls,
    async selectAll(table, query, orderBy) {
      calls.push(["selectAll", table]);
      if (table === "profiles") return data.profiles.filter((p) => p.telegram_chat_id != null);
      if (table === "watches") return data.watches.map((w) => ({...w}));
      if (table === "latest_prices") {
        const ids = query.match(/item_id=in\\.\\(([^)]*)\\)/)[1].split(",").map(Number);
        return data.prices.filter((p) => ids.includes(p.item_id));
      }
      throw new Error("unexpected table " + table);
    },
    async request(method, path, body, headers) {
      calls.push([method, path, body === undefined ? null : body]);
      if (method === "GET") {
        const source = path.match(/source=eq\\.(\\w+)/)[1];
        return data.newest[source] ? [{date: data.newest[source]}] : [];
      }
      if (method === "PATCH") {
        const id = Number(path.match(/id=eq\\.(\\d+)/)[1]);
        Object.assign(data.watches.find((w) => w.id === id), body);
      }
      return null;
    },
    async rpc(name, args) {
      calls.push(["rpc", name, args === undefined ? null : args]);
      if (name === "unlink_telegram_chat") {
        for (const p of data.profiles) if (p.telegram_chat_id === args.p_chat_id) p.telegram_chat_id = null;
        return 1;
      }
      return 0;
    },
  };
}
const sent = [];
async function fakeTelegram(chatId, text) {
  sent.push([chatId, text]);
  return chatId === 222 ? "blocked" : "sent";
}
"""

SCENARIO = {
    "profiles": [{"id": "u1", "telegram_chat_id": 111}, {"id": "u2", "telegram_chat_id": 222},
                 {"id": "u3", "telegram_chat_id": None}],
    "newest": {"ct_sealed": "2026-11-17", "ct_card": "2026-11-17"},
    "prices": [
        {"source": "ct_sealed", "item_id": 1, "foil": False, "date": "2026-11-17",
         "metrics": {"min_all": 12000}, "prev_metrics": {"min_all": 11000}, "name": "Box A"},
        {"source": "ct_sealed", "item_id": 2, "foil": False, "date": "2026-11-16",
         "metrics": {"min_all": 99999}, "prev_metrics": {"min_all": 99999}, "name": "Old Box"},
        {"source": "ct_card", "item_id": 10, "foil": True, "date": "2026-11-17",
         "metrics": {"ref_nm": 400}, "prev_metrics": {"ref_nm": 450}, "name": "Card X"},
        {"source": "ct_card", "item_id": 10, "foil": False, "date": "2026-11-17",
         "metrics": {"ref_nm": 9000}, "prev_metrics": {"ref_nm": 9000}, "name": "Card X"},
    ],
    "watches": [
        # fires (two days above 100.00)
        {"id": 1, "user_id": "u1", "source": "ct_sealed", "item_id": 1, "foil": False,
         "metric": "min_all", "above_cents": 10000, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
        # item not in the newest snapshot -> skipped
        {"id": 2, "user_id": "u1", "source": "ct_sealed", "item_id": 2, "foil": False,
         "metric": "min_all", "above_cents": 10000, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
        # foil card fires below 5.00
        {"id": 3, "user_id": "u1", "source": "ct_card", "item_id": 10, "foil": True,
         "metric": "ref_nm", "above_cents": None, "below_cents": 500,
         "above_state": "armed", "below_state": "armed"},
        # was fired, price fell under 98% -> re-armed, no message
        {"id": 4, "user_id": "u1", "source": "ct_card", "item_id": 10, "foil": False,
         "metric": "ref_nm", "above_cents": 10000, "below_cents": None,
         "above_state": "fired", "below_state": "armed"},
        # user without Telegram -> left alone
        {"id": 5, "user_id": "u3", "source": "ct_sealed", "item_id": 1, "foil": False,
         "metric": "min_all", "above_cents": 100, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
        # user who blocked the bot -> disconnected, state stays armed
        {"id": 6, "user_id": "u2", "source": "ct_sealed", "item_id": 1, "foil": False,
         "metric": "min_all", "above_cents": 100, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
        {"id": 7, "user_id": "u2", "source": "ct_card", "item_id": 10, "foil": True,
         "metric": "ref_nm", "above_cents": None, "below_cents": 500,
         "above_state": "armed", "below_state": "armed"},
        # no price at all for this item
        {"id": 8, "user_id": "u1", "source": "ct_sealed", "item_id": 99, "foil": False,
         "metric": "min_all", "above_cents": 100, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
        # metric missing from the data -> no change
        {"id": 9, "user_id": "u1", "source": "ct_sealed", "item_id": 1, "foil": False,
         "metric": "avg5_all", "above_cents": 1, "below_cents": None,
         "above_state": "armed", "below_state": "armed"},
    ],
}


@unittest.skipIf(JSC is None, "no JavaScript engine (jsc) on this computer")
class AlertJobRunTest(RunsJavaScript, unittest.TestCase):
    """One full run of alert_job.js on a fake database, then a second run."""
    IMPORTS = {"job": "send-alerts/alert_job.js"}

    def setUp(self):
        self.result = self.run_js(FAKE_DB_JS + f"""
            const data = {json.dumps(SCENARIO)};
            const db = fakeDb(data);
            const now = new Date("2026-11-17T12:00:00Z");
            const first = await job.runAlertJob(db, fakeTelegram, now);
            const firstCalls = db.calls.slice();
            const sentFirst = sent.slice();
            const second = await job.runAlertJob(db, fakeTelegram, now);
            print(JSON.stringify({{first, second, firstCalls, sentFirst,
                                   sentSecond: sent.slice(sentFirst.length)}}));""")

    def test_summary_of_the_first_run(self):
        self.assertEqual(self.result["first"], {
            "watches_checked": 5, "skipped_not_connected": 1, "skipped_no_price": 1,
            "skipped_not_in_newest_snapshot": 1, "alerts_sent": 2, "send_failures": 1,
            "rearmed": 1, "chats_disconnected": 1, "purged_accounts": 0})

    def test_messages_sent(self):
        self.assertEqual(self.result["sentFirst"], [
            [111, "Box A (sealed): lowest price (all sellers) is €120.00 — above your €100.00 "
                  "threshold. Snapshot 2026-11-17. https://www.cardtrader.com/cards/1"],
            [111, "Card X (foil, NM): reference price (all sellers) is €4.00 — below your €5.00 "
                  "threshold. Snapshot 2026-11-17. https://www.cardtrader.com/cards/10"],
            [222, "Box A (sealed): lowest price (all sellers) is €120.00 — above your €1.00 "
                  "threshold. Snapshot 2026-11-17. https://www.cardtrader.com/cards/1"],
        ])

    def test_database_writes(self):
        writes = [c for c in self.result["firstCalls"] if c[0] in ("POST", "PATCH", "DELETE", "rpc")]
        self.assertEqual(writes, [
            ["PATCH", "watches?id=eq.1&above_cents=eq.10000&below_cents=is.null",
             {"above_state": "fired", "below_state": "armed"}],
            ["POST", "alert_log", [{"user_id": "u1", "watch_id": 1, "direction": "above",
                                    "value_cents": 12000, "snapshot_date": "2026-11-17"}]],
            ["PATCH", "watches?id=eq.3&above_cents=is.null&below_cents=eq.500",
             {"above_state": "armed", "below_state": "fired"}],
            ["POST", "alert_log", [{"user_id": "u1", "watch_id": 3, "direction": "below",
                                    "value_cents": 400, "snapshot_date": "2026-11-17"}]],
            ["PATCH", "watches?id=eq.4&above_cents=eq.10000&below_cents=is.null",
             {"above_state": "armed", "below_state": "armed"}],
            ["rpc", "unlink_telegram_chat", {"p_chat_id": 222}],
            ["DELETE", "alert_log?sent_at=lt.2026-08-19T12%3A00%3A00.000Z", None],
            ["rpc", "purge_inactive_accounts", None],
        ])

    def test_second_run_on_the_same_data_sends_nothing(self):
        self.assertEqual(self.result["sentSecond"], [])
        self.assertEqual(self.result["second"]["alerts_sent"], 0)
        self.assertEqual(self.result["second"]["skipped_not_connected"], 3)  # u2 now disconnected


@unittest.skipIf(JSC is None, "no JavaScript engine (jsc) on this computer")
class BlockedChatTest(RunsJavaScript, unittest.TestCase):
    IMPORTS = {"job": "send-alerts/alert_job.js"}

    def test_both_directions_firing_for_a_blocked_chat_keep_their_states(self):
        # A (strange but allowed) watch whose "below" is higher than its
        # "above": both fire at once. The first message finds the bot blocked.
        data = {
            "profiles": [{"id": "u2", "telegram_chat_id": 222}],
            "newest": {"ct_sealed": "2026-11-17"},
            "prices": [{"source": "ct_sealed", "item_id": 1, "foil": False,
                        "date": "2026-11-17", "metrics": {"min_all": 5000},
                        "prev_metrics": {"min_all": 5000}, "name": "Box"}],
            "watches": [{"id": 1, "user_id": "u2", "source": "ct_sealed", "item_id": 1,
                         "foil": False, "metric": "min_all", "above_cents": 4000,
                         "below_cents": 6000, "above_state": "armed", "below_state": "armed"}],
        }
        out = self.run_js(FAKE_DB_JS + f"""
            const db = fakeDb({json.dumps(data)});
            const summary = await job.runAlertJob(db, fakeTelegram, new Date("2026-11-17T12:00:00Z"));
            print(JSON.stringify({{summary, sent, calls: db.calls}}));""")
        self.assertEqual(len(out["sent"]), 1)  # the second message is not even tried
        self.assertEqual(out["summary"]["send_failures"], 1)
        self.assertEqual(out["summary"]["alerts_sent"], 0)
        self.assertFalse([c for c in out["calls"] if c[0] in ("PATCH", "POST")])

    def test_a_blocked_chat_shared_by_two_accounts_is_tried_only_once(self):
        # Reviewer fix: the same Telegram chat can be connected to two
        # accounts. Once it is found blocked, the second account's alert must
        # not be sent to it again (and it must not be "disconnected" twice).
        watch = {"source": "ct_sealed", "item_id": 1, "foil": False, "metric": "min_all",
                 "above_cents": 100, "below_cents": None,
                 "above_state": "armed", "below_state": "armed"}
        data = {
            "profiles": [{"id": "a", "telegram_chat_id": 222}, {"id": "b", "telegram_chat_id": 222}],
            "newest": {"ct_sealed": "2026-11-17"},
            "prices": [{"source": "ct_sealed", "item_id": 1, "foil": False,
                        "date": "2026-11-17", "metrics": {"min_all": 5000},
                        "prev_metrics": {"min_all": 5000}, "name": "Box"}],
            "watches": [dict(watch, id=1, user_id="a"), dict(watch, id=2, user_id="b")],
        }
        out = self.run_js(FAKE_DB_JS + f"""
            const db = fakeDb({json.dumps(data)});
            const summary = await job.runAlertJob(db, fakeTelegram, new Date("2026-11-17T12:00:00Z"));
            print(JSON.stringify({{summary, sent, calls: db.calls}}));""")
        self.assertEqual(len(out["sent"]), 1)
        self.assertEqual(out["summary"]["chats_disconnected"], 1)
        unlinks = [c for c in out["calls"] if c[:2] == ["rpc", "unlink_telegram_chat"]]
        self.assertEqual(len(unlinks), 1)
        self.assertFalse([c for c in out["calls"] if c[0] in ("PATCH", "POST")])


if __name__ == "__main__":
    unittest.main()
