// @ts-check
// =====================================================================
// One run of the alert check — the steps behind the send-alerts function.
// =====================================================================
// Kept apart from index.ts (which only handles the web request) and written
// in plain JavaScript, so it can be tested on a Mac with no installs, using a
// fake database and a fake Telegram: see supabase/tests/test_send_alerts.py.
//
// Steps:
//   1. read who connected Telegram, all watches, and the latest prices of
//      the watched items;
//   2. apply the alert rule (alert_rule.js, contract C4) to each watch of a
//      connected user — only for items present in the newest snapshot of
//      their source, so old data never triggers anything;
//   3. for each alert: send the Telegram message; if it did not arrive, keep
//      that direction "armed" so it is tried again with the next snapshot;
//      if the user blocked the bot, disconnect them;
//   4. save changed armed/fired states, then write the sent alerts to alert_log;
//   5. housekeeping: delete alert_log rows older than 90 days, and accounts
//      nobody signed into for 24 months (both promised on privacy.html).
// Running it twice on the same data sends nothing new.

import { evaluateWatch, formatMessage } from "./alert_rule.js";

export const SOURCES = ["ct_card", "ct_sealed"];
export const LOG_KEEP_DAYS = 90;
const ITEMS_PER_REQUEST = 150;

/**
 * @typedef {{ id: number, user_id: string, source: string, item_id: number,
 *   foil: boolean, metric: string, above_cents: number | null,
 *   below_cents: number | null, above_state: "armed" | "fired",
 *   below_state: "armed" | "fired" }} Watch
 * @typedef {{ source: string, item_id: number, foil: boolean, date: string,
 *   metrics: Record<string, number | null>,
 *   prev_metrics: Record<string, number | null> | null, name: string }} Price
 * @typedef {{
 *   selectAll: (table: string, query: string, orderBy: string) => Promise<any[]>,
 *   request: (method: string, path: string, body?: unknown,
 *             headers?: Record<string, string>) => Promise<any>,
 *   rpc: (name: string, args?: Record<string, unknown>) => Promise<any>,
 * }} Db
 * @typedef {(chatId: number, text: string) => Promise<"sent" | "blocked" | "failed">} SendMessage
 */

/**
 * @param {Db} db                 the database (service role)
 * @param {SendMessage} sendMessage  sends one Telegram message
 * @param {Date} [now]
 */
export async function runAlertJob(db, sendMessage, now = new Date()) {
  const summary = {
    watches_checked: 0, skipped_not_connected: 0, skipped_no_price: 0,
    skipped_not_in_newest_snapshot: 0, alerts_sent: 0, send_failures: 0,
    rearmed: 0, chats_disconnected: 0, purged_accounts: 0,
  };

  // 1. Who has Telegram connected, and what does everyone watch?
  const profiles = await db.selectAll(
    "profiles", "select=id,telegram_chat_id&telegram_chat_id=not.is.null", "id");
  /** @type {Map<string, number>} */
  const chatOf = new Map(profiles.map((p) => [p.id, p.telegram_chat_id]));
  /** @type {Watch[]} */
  const allWatches = await db.selectAll("watches", "select=*", "id");
  // Watches of users without Telegram are left untouched (still "armed"),
  // so they get their alert once they connect.
  const watches = allWatches.filter((w) => chatOf.has(w.user_id));
  summary.skipped_not_connected = allWatches.length - watches.length;

  const prices = await loadPrices(db, watches);
  const newestDate = await loadNewestDates(db);

  // 2-4. Apply the rule to each watch.
  // Chats found blocked during this run. Kept by CHAT (not by user): the same
  // Telegram chat can be connected to two accounts, and "/stop" or a block
  // disconnects it from all of them at once.
  /** @type {Set<number>} */
  const blockedChats = new Set();
  for (const watch of watches) {
    const chatId = /** @type {number} */ (chatOf.get(watch.user_id));
    if (blockedChats.has(chatId)) continue;
    const price = prices.get(itemKey(watch));
    if (!price) {
      summary.skipped_no_price++;
      continue;
    }
    if (price.date !== newestDate.get(watch.source)) {
      summary.skipped_not_in_newest_snapshot++;
      continue;
    }
    summary.watches_checked++;

    const result = evaluateWatch(watch, price.metrics, price.prev_metrics);
    const newStates = { above: result.above_state, below: result.below_state };
    if (watch.above_state === "fired" && newStates.above === "armed") summary.rearmed++;
    if (watch.below_state === "fired" && newStates.below === "armed") summary.rearmed++;

    const delivered = [];
    for (const direction of result.fired) {
      const threshold = direction === "above" ? watch.above_cents : watch.below_cents;
      const text = formatMessage({
        name: price.name || `CardTrader item ${watch.item_id}`,
        source: watch.source,
        foil: watch.foil,
        metric: watch.metric,
        direction,
        valueCents: /** @type {number} */ (result.value),
        thresholdCents: /** @type {number} */ (threshold),
        date: price.date,
        itemId: watch.item_id,
      });
      // (If the chat was just found blocked, don't try again.)
      const outcome = blockedChats.has(chatId)
        ? "skipped"
        : await sendMessage(chatId, text);
      if (outcome === "sent") {
        summary.alerts_sent++;
        delivered.push({
          user_id: watch.user_id,
          watch_id: watch.id,
          direction,
          value_cents: result.value,
          snapshot_date: price.date,
        });
      } else {
        // Not delivered: keep the old state, so it is tried again next time.
        newStates[direction] = direction === "above" ? watch.above_state : watch.below_state;
        if (outcome !== "skipped") summary.send_failures++;
        if (outcome === "blocked") {
          // The user blocked the bot or deleted the chat: stop writing to it.
          await db.rpc("unlink_telegram_chat", { p_chat_id: chatId });
          blockedChats.add(chatId);
          summary.chats_disconnected++;
        }
      }
    }

    // Save the new states, then record what was sent. (The message has
    // already gone out at this point. If saving fails, the run stops with an
    // error and the next run may send that one message again — better than
    // silently losing an alert. Because the run stops at the first error,
    // at most this one watch's message(s) can be repeated.)
    if (newStates.above !== watch.above_state || newStates.below !== watch.below_state) {
      await saveStates(db, watch, newStates);
    }
    if (delivered.length) {
      await db.request("POST", "alert_log", delivered, { Prefer: "return=minimal" });
    }
  }

  // 5. Housekeeping.
  const cutoff = new Date(now.getTime() - LOG_KEEP_DAYS * 24 * 3600 * 1000).toISOString();
  await db.request("DELETE", `alert_log?sent_at=lt.${encodeURIComponent(cutoff)}`, undefined,
    { Prefer: "return=minimal" });
  summary.purged_accounts = Number(await db.rpc("purge_inactive_accounts"));

  return summary;
}

/** @param {{ source: string, item_id: number, foil: boolean }} item */
export function itemKey(item) {
  return `${item.source}:${item.item_id}:${item.foil}`;
}

/**
 * Latest prices of the watched items, by itemKey.
 * @param {Db} db
 * @param {Watch[]} watches
 * @returns {Promise<Map<string, Price>>}
 */
async function loadPrices(db, watches) {
  /** @type {Map<string, Price>} */
  const prices = new Map();
  const ids = [...new Set(watches.map((w) => w.item_id))];
  for (let start = 0; start < ids.length; start += ITEMS_PER_REQUEST) {
    const chunk = ids.slice(start, start + ITEMS_PER_REQUEST);
    const rows = await db.selectAll(
      "latest_prices",
      `select=source,item_id,foil,date,metrics,prev_metrics,name&item_id=in.(${chunk.join(",")})`,
      "source,item_id,foil",
    );
    for (const row of rows) prices.set(itemKey(row), row);
  }
  return prices;
}

/**
 * The newest snapshot date of each source, e.g. ct_sealed -> "2026-11-17".
 * @param {Db} db
 * @returns {Promise<Map<string, string>>}
 */
async function loadNewestDates(db) {
  /** @type {Map<string, string>} */
  const newest = new Map();
  for (const source of SOURCES) {
    const rows = await db.request(
      "GET", `latest_prices?select=date&source=eq.${source}&order=date.desc&limit=1`);
    if (rows && rows.length) newest.set(source, rows[0].date);
  }
  return newest;
}

/**
 * Save new armed/fired states. The threshold filters mean: if the user
 * changed a threshold meanwhile, leave their fresh watch alone.
 * @param {Db} db
 * @param {Watch} watch
 * @param {{ above: string, below: string }} states
 */
async function saveStates(db, watch, states) {
  /** @param {string} column @param {number | null} value */
  const same = (column, value) => value === null ? `${column}=is.null` : `${column}=eq.${value}`;
  await db.request(
    "PATCH",
    `watches?id=eq.${watch.id}&${same("above_cents", watch.above_cents)}` +
      `&${same("below_cents", watch.below_cents)}`,
    { above_state: states.above, below_state: states.below },
    { Prefer: "return=minimal" },
  );
}
