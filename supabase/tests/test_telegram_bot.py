"""Checks what the Telegram bot answers (functions/telegram-webhook/commands.js),
using a fake database. Runs with "jsc", the JavaScript engine built into every
Mac; skipped elsewhere.

Run from the repository folder:
    python3 -m unittest discover -s supabase/tests -v
"""

import json
import unittest

from js_runner import JSC, RunsJavaScript

FAKE_DB = """
const calls = [];
const db = {
  async rpc(name, args) {
    calls.push([name, args]);
    if (name === "link_telegram_chat") return args.p_token === "goodcode123";
    if (name === "unlink_telegram_chat") return args.p_chat_id === 42 ? 1 : 0;
    throw new Error("unexpected " + name);
  },
};
const page = "https://example.github.io/site/account.html";
const say = bot.texts(page);
async function reply(text, chatId) { return await bot.answerMessage(text, chatId, db, page); }
"""


@unittest.skipIf(JSC is None, "no JavaScript engine (jsc) on this computer")
class TelegramBotTest(RunsJavaScript, unittest.TestCase):
    IMPORTS = {"bot": "telegram-webhook/commands.js"}

    def ask(self, *messages, chat_id=42):
        """Send messages to the bot; returns (names of the replies, database calls)."""
        out = self.run_js(FAKE_DB + f"""
            const names = [];
            for (const text of {json.dumps(list(messages))}) {{
              const answer = await reply(text, {chat_id});
              names.push(Object.keys(say).find((k) => say[k] === answer));
            }}
            print(JSON.stringify({{names, calls}}));""")
        return out["names"], out["calls"]

    def test_valid_code_links_the_chat(self):
        names, calls = self.ask("/start goodcode123")
        self.assertEqual(names, ["connected"])
        self.assertEqual(calls, [["link_telegram_chat",
                                  {"p_token": "goodcode123", "p_chat_id": 42}]])

    def test_unknown_or_expired_code(self):
        self.assertEqual(self.ask("/start oldcode")[0], ["expired"])

    def test_malformed_code_never_reaches_the_database(self):
        names, calls = self.ask("/start bad;code", "/start " + "a" * 65)
        self.assertEqual(names, ["expired", "expired"])
        self.assertEqual(calls, [])

    def test_start_without_code_explains_how_to_connect(self):
        names, calls = self.ask("/start", "/START@SomeBot")
        self.assertEqual(names, ["welcome", "welcome"])
        self.assertEqual(calls, [])

    def test_stop(self):
        self.assertEqual(self.ask("/stop")[0], ["stopped"])
        self.assertEqual(self.ask("/stop", chat_id=7)[0], ["notConnected"])

    def test_anything_else_gets_help(self):
        names, calls = self.ask("hello", "/prices", "")
        self.assertEqual(names, ["help", "help", "help"])
        self.assertEqual(calls, [])

    def test_texts_point_to_the_account_page(self):
        out = self.run_js(FAKE_DB + """
            print(JSON.stringify([say.welcome, bot.accountPageFrom("https://x.io/site"),
                                  bot.accountPageFrom("https://x.io/site/"),
                                  bot.accountPageFrom("")]));""")
        self.assertIn("https://example.github.io/site/account.html", out[0])
        self.assertEqual(out[1:], ["https://x.io/site/account.html",
                                   "https://x.io/site/account.html",
                                   "the account page of the website"])


if __name__ == "__main__":
    unittest.main()
