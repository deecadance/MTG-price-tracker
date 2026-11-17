// @ts-check
// =====================================================================
// The alert rule (contract C4) and the alert message — nothing else.
// =====================================================================
// This file is kept apart on purpose:
//   * it has no Supabase/Telegram/network code, so it is easy to read and
//     to compare line by line with the Python version used for the owner's
//     own alerts (private repo, alerts/);
//   * it is plain JavaScript (with type notes in comments) so it can be
//     tested on a Mac without installing anything — see
//     supabase/tests/test_send_alerts.py. Deno/Supabase import it from
//     index.ts like any TypeScript file.
//
// The rule, in words. A watch has an optional "above" and an optional
// "below" threshold (in euro cents); each has a state, "armed" or "fired".
//   above: fires when the price is >= the threshold on BOTH the latest and
//          the previous snapshot (two days in a row — one silly listing
//          can't trigger it) and the state is "armed"; then it becomes
//          "fired". It re-arms when the latest price drops below 98% of
//          the threshold (the 2% gap stops it flapping on and off).
//   below: the mirror image — fires at <= threshold twice in a row,
//          re-arms when the latest price rises above 102% of the threshold.
//   If either day's price is missing, nothing changes.

/** @typedef {"armed" | "fired"} AlertState */
/** @typedef {"above" | "below"} Direction */
/** @typedef {number | null | undefined} Cents */

/**
 * Apply the rule to one direction of one watch.
 * @param {Direction} direction
 * @param {Cents} threshold   the user's threshold in cents (null = not set)
 * @param {AlertState} state  the current state
 * @param {Cents} latest      the metric on the latest snapshot
 * @param {Cents} previous    the metric on the previous available snapshot
 * @returns {{ state: AlertState, fired: boolean }}
 */
export function evaluateDirection(direction, threshold, state, latest, previous) {
  const unchanged = { state, fired: false };
  if (threshold == null || latest == null || previous == null) return unchanged;

  if (direction === "above") {
    if (state === "armed" && latest >= threshold && previous >= threshold) {
      return { state: "fired", fired: true };
    }
    // latest < threshold * 0.98, in whole numbers (same as the Python version)
    if (state === "fired" && latest * 100 < threshold * 98) {
      return { state: "armed", fired: false };
    }
    return unchanged;
  }

  if (direction === "below") {
    if (state === "armed" && latest <= threshold && previous <= threshold) {
      return { state: "fired", fired: true };
    }
    // latest > threshold * 1.02, in whole numbers
    if (state === "fired" && latest * 100 > threshold * 102) {
      return { state: "armed", fired: false };
    }
    return unchanged;
  }

  throw new Error(`unknown direction: ${direction}`);
}

/**
 * Apply the rule to both directions of a watch.
 * @param {{ metric: string, above_cents: Cents, below_cents: Cents,
 *           above_state: AlertState, below_state: AlertState }} watch
 * @param {Record<string, Cents> | null | undefined} latestMetrics
 * @param {Record<string, Cents> | null | undefined} previousMetrics
 * @returns {{ above_state: AlertState, below_state: AlertState,
 *             fired: Direction[], value: Cents }}
 */
export function evaluateWatch(watch, latestMetrics, previousMetrics) {
  const latest = latestMetrics ? latestMetrics[watch.metric] : null;
  const previous = previousMetrics ? previousMetrics[watch.metric] : null;
  const above = evaluateDirection("above", watch.above_cents, watch.above_state, latest, previous);
  const below = evaluateDirection("below", watch.below_cents, watch.below_state, latest, previous);
  /** @type {Direction[]} */
  const fired = [];
  if (above.fired) fired.push("above");
  if (below.fired) fired.push("below");
  return { above_state: above.state, below_state: below.state, fired, value: latest };
}

// Words used in messages (and on the account page — keep docs/js/account.js
// in sync if you change them).
/** @type {Record<string, string>} */
export const METRIC_LABELS = {
  // sealed products (ct_sealed)
  min_all: "lowest price (all sellers)",
  avg5_all: "average of the 5 cheapest (all sellers)",
  min_zero: "lowest price (CardTrader Zero)",
  avg5_zero: "average of the 5 cheapest (CardTrader Zero)",
  // single cards, Near Mint (ct_card)
  buy_now_nm: "lowest price (all sellers)",
  ref_nm: "reference price (all sellers)",
  buy_now_nm_zero: "lowest price (CardTrader Zero)",
  ref_nm_zero: "reference price (CardTrader Zero)",
};

/**
 * 4999 -> "€49.99". Whole-number maths, so there are no rounding surprises.
 * @param {number} cents
 */
export function euros(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.round(cents));
  return `${sign}€${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/**
 * The Telegram text for one alert (contract C4). Plain text, no formatting.
 * @param {{ name: string, source: string, foil: boolean, metric: string,
 *           direction: Direction, valueCents: number, thresholdCents: number,
 *           date: string, itemId: number }} alert
 */
export function formatMessage(alert) {
  const kind = alert.source === "ct_sealed" ? "sealed" : (alert.foil ? "foil, NM" : "NM");
  const label = METRIC_LABELS[alert.metric] || alert.metric;
  return `${alert.name} (${kind}): ${label} is ${euros(alert.valueCents)} — ` +
    `${alert.direction} your ${euros(alert.thresholdCents)} threshold. ` +
    `Snapshot ${alert.date}. https://www.cardtrader.com/cards/${alert.itemId}`;
}
