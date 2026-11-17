// =====================================================================
// send-alerts — checks every user's watches against the latest prices and
// sends Telegram messages for the ones that trigger.
// =====================================================================
// Who calls it: only our daily sync job (private repo,
// supabase_sync/push_latest.py), right after it uploaded new prices. The
// call must carry the header  x-alerts-secret: <ALERTS_WEBHOOK_SECRET>,
// otherwise it is refused. It is deployed with --no-verify-jwt because
// that secret, not a Supabase login, is what protects it.
//
// This file only handles the web request and the Telegram connection.
// The steps of a run are in alert_job.js; the rule itself is in
// alert_rule.js (contract C4).
//
// Settings it needs (supabase secrets set ...):
//   ALERTS_WEBHOOK_SECRET  shared with the sync job
//   TELEGRAM_BOT_TOKEN     from @BotFather
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are provided by Supabase.
// While either setting is missing, the function refuses to do anything.

import { Database, jsonResponse, sameSecret } from "../_shared/supabase_rest.ts";
import { runAlertJob } from "./alert_job.js";

const PAUSE_BETWEEN_MESSAGES_MS = 60; // stay well under Telegram's ~30 messages/second

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") return jsonResponse({ error: "use POST" }, 405);

  const expectedSecret = Deno.env.get("ALERTS_WEBHOOK_SECRET");
  const botToken = Deno.env.get("TELEGRAM_BOT_TOKEN");
  if (!expectedSecret || !botToken) {
    return jsonResponse({ error: "send-alerts is not configured yet" }, 503);
  }
  if (!sameSecret(request.headers.get("x-alerts-secret"), expectedSecret)) {
    return jsonResponse({ error: "wrong or missing x-alerts-secret" }, 401);
  }

  try {
    const summary = await runAlertJob(
      Database.fromEnvironment(),
      (chatId: number, text: string) => sendTelegram(botToken, chatId, text),
    );
    console.log("send-alerts finished", JSON.stringify(summary));
    return jsonResponse(summary);
  } catch (error) {
    const message = hideToken(String(error), botToken);
    console.error("send-alerts failed:", message);
    return jsonResponse({ error: message }, 500);
  }
});

/**
 * Send one plain-text Telegram message.
 * "sent" = delivered; "blocked" = the user blocked the bot (Telegram answers
 * 403); "failed" = anything else (tried again with the next snapshot).
 */
async function sendTelegram(
  botToken: string,
  chatId: number,
  text: string,
): Promise<"sent" | "blocked" | "failed"> {
  for (let attempt = 1; attempt <= 2; attempt++) {
    await sleep(PAUSE_BETWEEN_MESSAGES_MS);
    try {
      const response = await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: chatId,
          text,
          link_preview_options: { is_disabled: true },
        }),
        // Give up on one message after 15 seconds, so a single stuck request
        // can't use up the function's whole time limit (about 150 s).
        signal: AbortSignal.timeout(15_000),
      });
      // deno-lint-ignore no-explicit-any
      const answer: any = await response.json().catch(() => ({}));
      if (response.ok) return "sent";
      if (response.status === 403) return "blocked";
      if (response.status === 429 && attempt === 1) {
        // "Too many requests": Telegram says how many seconds to wait.
        await sleep(Math.min(Number(answer?.parameters?.retry_after) || 5, 30) * 1000);
        continue;
      }
      console.error(`Telegram refused a message (${response.status}): ${answer?.description ?? ""}`);
      return "failed";
    } catch (error) {
      // Network errors can include the address, which contains the bot token.
      console.error("Telegram request failed:", hideToken(String(error), botToken));
      return "failed";
    }
  }
  return "failed";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Never print the bot token in logs or replies. */
function hideToken(text: string, botToken: string): string {
  return botToken ? text.split(botToken).join("***") : text;
}
