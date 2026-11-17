// @ts-check
// What the bot answers to each message. Kept apart from index.ts (which only
// checks the secret header and reads Telegram's request) and written in plain
// JavaScript so it can be tested on a Mac with no installs
// (supabase/tests/test_telegram_bot.py).

/**
 * The bot's texts. `accountPage` is the address of docs/account.html.
 * @param {string} accountPage
 */
export function texts(accountPage) {
  return {
    connected:
      "Connected! You'll get a message here when one of your price alerts triggers. " +
      "Prices are checked once a day. To stop the alerts, send /stop.",
    expired:
      "Sorry, this link has expired or was already used. Please go back to " +
      `${accountPage} and press "Connect Telegram" again (each link works for 15 minutes).`,
    welcome:
      "Hello! This bot sends the price alerts you set up on MTG Price Tracker. " +
      `To connect, sign in at ${accountPage} and press "Connect Telegram".`,
    stopped:
      "Done: this chat is disconnected and you won't receive alerts here anymore. " +
      "Your watches are still saved on the website; you can reconnect any time.",
    notConnected: "This chat isn't connected to any account, so there is nothing to stop.",
    help:
      `Send /stop to stop alerts in this chat. To connect or change your alerts, visit ${accountPage}.`,
    error: "Sorry, something went wrong on our side. Please try again in a few minutes.",
  };
}

/**
 * SITE_URL setting -> address of the account page (or a description).
 * @param {string} siteUrl
 */
export function accountPageFrom(siteUrl) {
  const base = (siteUrl || "").trim();
  if (!base) return "the account page of the website";
  return base.replace(/\/?$/, "/") + "account.html";
}

/**
 * Work out the reply to one text message from a private chat.
 *   "/start <code>"  -> link this chat to the account that made the code
 *   "/start"         -> how to connect
 *   "/stop"          -> disconnect this chat
 *   anything else    -> help
 * @param {string} text
 * @param {number} chatId
 * @param {{ rpc: (name: string, args?: Record<string, unknown>) => Promise<any> }} db
 * @param {string} accountPage
 * @returns {Promise<string>}
 */
export async function answerMessage(text, chatId, db, accountPage) {
  const say = texts(accountPage);
  // "/start abc", "/start@OurBot abc", "/stop" ...
  const match = text.trim().match(/^\/([A-Za-z]+)(?:@\w+)?(?:\s+(\S+))?/);
  if (!match) return say.help;
  const command = match[1].toLowerCase();
  const argument = match[2] || "";

  if (command === "start") {
    if (!argument) return say.welcome;
    // Our codes are 32 letters/digits; Telegram allows up to 64 of A-Z a-z 0-9 _ -.
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(argument)) return say.expired;
    const linked = await db.rpc("link_telegram_chat", { p_token: argument, p_chat_id: chatId });
    return linked === true ? say.connected : say.expired;
  }

  if (command === "stop") {
    const count = Number(await db.rpc("unlink_telegram_chat", { p_chat_id: chatId }));
    return count > 0 ? say.stopped : say.notConnected;
  }

  return say.help;
}
