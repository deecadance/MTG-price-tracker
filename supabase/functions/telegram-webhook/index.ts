// =====================================================================
// telegram-webhook — receives the messages people send to our Telegram bot.
// =====================================================================
// Telegram calls this address for every message to the bot (we tell it to
// with "setWebhook", see SETUP_ACCOUNTS.md). Every genuine call carries the
// header X-Telegram-Bot-Api-Secret-Token with the secret we chose; anything
// without it is refused. Deployed with --no-verify-jwt because Telegram
// cannot log in to Supabase — the secret header is the protection.
//
// It only answers text messages in one-to-one chats. What it answers is
// decided in commands.js:
//   /start <code>  (sent automatically when someone presses "Connect
//                  Telegram" on their account page) links this chat to their
//                  account, if the code is valid (15 minutes, single use)
//   /stop          disconnects this chat
//
// The reply goes back inside the answer to Telegram itself (Telegram allows
// one "sendMessage" in a webhook response), so this function does not need
// the bot token.
//
// Settings it needs (supabase secrets set ...):
//   TELEGRAM_WEBHOOK_SECRET  the same value given to setWebhook as secret_token
//   SITE_URL                 optional, e.g. https://deecadance.github.io/MTG-price-tracker/
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are provided by Supabase.
// While TELEGRAM_WEBHOOK_SECRET is missing, the function refuses everything.

import { Database, sameSecret } from "../_shared/supabase_rest.ts";
import { accountPageFrom, answerMessage, texts } from "./commands.js";

const accountPage = accountPageFrom(Deno.env.get("SITE_URL") ?? "");

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });

  const expectedSecret = Deno.env.get("TELEGRAM_WEBHOOK_SECRET");
  if (!expectedSecret) return new Response("not configured yet", { status: 503 });
  if (!sameSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), expectedSecret)) {
    return new Response("forbidden", { status: 401 });
  }

  // deno-lint-ignore no-explicit-any
  let update: any;
  try {
    update = await request.json();
  } catch {
    return new Response("not JSON", { status: 400 });
  }

  const message = update?.message;
  const chat = message?.chat;
  // Ignore everything that isn't a text message in a one-to-one chat.
  if (typeof message?.text !== "string" || chat?.type !== "private" || typeof chat?.id !== "number") {
    return new Response("ignored");
  }

  try {
    const reply = await answerMessage(message.text, chat.id, Database.fromEnvironment(), accountPage);
    return replyWith(chat.id, reply);
  } catch (error) {
    console.error("telegram-webhook failed:", String(error));
    return replyWith(chat.id, texts(accountPage).error);
  }
});

/** Answer Telegram with one plain-text message for this chat. */
function replyWith(chatId: number, text: string): Response {
  return new Response(JSON.stringify({ method: "sendMessage", chat_id: chatId, text }), {
    headers: { "Content-Type": "application/json" },
  });
}
