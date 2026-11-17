"""Checks the account page's script (docs/js/account.js) without a browser.

It runs the real file with "jsc" (the JavaScript engine built into every
Mac) on top of a tiny fake web page and a fake Supabase library, so nothing
is downloaded and nothing goes on the internet. Skipped where jsc is missing.

What is checked:
  * switched off (empty docs/config.js): the script loads NO library,
    creates no connection and leaves "Accounts are not available yet" on
    screen — the dormant state the site is published in today;
  * switched on (fake settings): when a slow, older search answers after a
    newer one, only the newer search's results are shown (reviewer fix:
    before, both lists were shown one after the other).

Run from the repository folder:
    python3 -m unittest discover -s supabase/tests -v
"""

import json
import unittest
from pathlib import Path

from js_runner import JSC, RunsJavaScript

ACCOUNT_JS = Path(__file__).resolve().parents[2] / "docs" / "js" / "account.js"

# A pretend browser: just enough of `document`, `location`, timers, and a
# fake Supabase library whose answers we control.
FAKE_PAGE_JS = r"""
const log = { scriptsLoaded: [], elementsCreated: 0, clientsCreated: 0, queries: [] };

// jsc has setTimeout but no clearTimeout/setInterval: add simple ones.
const realSetTimeout = setTimeout;
let timerId = 0;
const cancelled = new Set();
globalThis.setTimeout = (fn, ms) => {
  const id = ++timerId;
  realSetTimeout(() => { if (!cancelled.has(id)) fn(); }, ms || 0);
  return id;
};
globalThis.clearTimeout = (id) => cancelled.add(id);
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fakeElement(tag) {
  return {
    tagName: tag, children: [], textContent: "", value: "", hidden: false,
    className: "", dataset: {}, style: {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
  };
}
const elements = {};
globalThis.window = globalThis;
globalThis.document = {
  getElementById(id) { return elements[id] || (elements[id] = fakeElement("#" + id)); },
  createElement(tag) { log.elementsCreated++; return fakeElement(tag); },
  head: {
    appendChild(script) {
      // "Loading" the library: hand over the fake one instead of downloading.
      log.scriptsLoaded.push(script.src);
      setTimeout(() => { window.supabase = fakeSupabase; script.onload(); }, 0);
    },
  },
};
globalThis.location = { search: "", hash: "", pathname: "/site/account.html",
                        origin: "https://example.test" };
globalThis.history = { replaceState() {} };
globalThis.URLSearchParams = function () { this.get = () => null; };
globalThis.confirm = () => false;

// Search answers: pattern -> [delay in ms, rows]
const searchAnswers = {};
function query(table) {
  const q = { table, calls: [] };
  for (const name of ["select", "ilike", "order", "limit", "eq", "in", "maybeSingle"]) {
    q[name] = (...args) => { q.calls.push([name, ...args]); return q; };
  }
  q.then = (resolve, reject) => {
    log.queries.push([table, q.calls]);
    const ilike = q.calls.find((c) => c[0] === "ilike");
    let answer = Promise.resolve({ data: [], error: null });
    if (table === "profiles") answer = Promise.resolve({ data: { telegram_chat_id: null }, error: null });
    if (ilike) {
      const [delay, rows] = searchAnswers[ilike[2]];
      answer = wait(delay).then(() => ({ data: rows, error: null }));
    }
    return answer.then(resolve, reject);
  };
  return q;
}
const fakeSupabase = {
  createClient(url, key) {
    log.clientsCreated++;
    return {
      auth: {
        getSession: async () => ({ data: { session: { user: { id: "u1", email: "a@example.test" } } } }),
        onAuthStateChange() {},
      },
      from: query,
      rpc: async () => ({ data: null, error: null }),
    };
  },
};
function box(name, item_id) {
  return { source: "ct_sealed", item_id, foil: false, name, date: "2026-11-17",
           metrics: { avg5_all: 10000 } };
}
"""


@unittest.skipIf(JSC is None, "no JavaScript engine (jsc) on this computer")
class AccountPageTest(RunsJavaScript, unittest.TestCase):

    def run_page(self, config, then_js):
        return self.run_js(FAKE_PAGE_JS + f"""
            window.APP_CONFIG = {json.dumps(config)};
            load({json.dumps(str(ACCOUNT_JS))});
            await wait(50);   // let start() finish
            {then_js}""")

    def test_switched_off_loads_nothing(self):
        out = self.run_page(
            {"supabaseUrl": "", "supabaseAnonKey": "", "telegramBotName": ""},
            "print(JSON.stringify({log, unavailableHidden: document.getElementById('unavailable').hidden}));")
        self.assertEqual(out["log"]["scriptsLoaded"], [])
        self.assertEqual(out["log"]["elementsCreated"], 0)
        self.assertEqual(out["log"]["clientsCreated"], 0)
        self.assertEqual(out["log"]["queries"], [])
        self.assertFalse(out["unavailableHidden"])

    def test_switched_on_loads_the_pinned_library_once(self):
        out = self.run_page(
            {"supabaseUrl": "https://x.supabase.co", "supabaseAnonKey": "public-key",
             "telegramBotName": "SomeBot"},
            "print(JSON.stringify({log, unavailableHidden: document.getElementById('unavailable').hidden}));")
        self.assertEqual(len(out["log"]["scriptsLoaded"]), 1)
        self.assertRegex(out["log"]["scriptsLoaded"][0], r"@supabase/supabase-js@\d+\.\d+\.\d+")
        self.assertEqual(out["log"]["clientsCreated"], 1)
        self.assertTrue(out["unavailableHidden"])

    def test_a_slow_older_search_does_not_overwrite_a_newer_one(self):
        out = self.run_page(
            {"supabaseUrl": "https://x.supabase.co", "supabaseAnonKey": "public-key",
             "telegramBotName": "SomeBot"},
            """
            searchAnswers["%foundations%"] = [500, [box("Foundations Play Booster Box", 1)]];
            searchAnswers["%foundations%collector%"] = [10, [box("Foundations Collector Booster Box", 2)]];
            const input = document.getElementById("search");
            input.value = "foundations";
            input.oninput();          // search 1 starts after 300 ms, answers 500 ms later
            await wait(350);
            input.value = "foundations collector";
            input.oninput();          // search 2 starts after 300 ms, answers 10 ms later
            await wait(900);          // both have answered by now
            const shown = document.getElementById("results").children
              .map((li) => li.children[0].textContent);
            print(JSON.stringify(shown));""")
        self.assertEqual(out, ["Foundations Collector Booster Box (sealed)"])


if __name__ == "__main__":
    unittest.main()
