// The account page (docs/account.html): sign in, follow prices, connect
// Telegram, delete the account.
//
// How it is protected: this page only ever uses the PUBLIC key from
// config.js. What a signed-in visitor may read or change is decided by the
// database itself (Row Level Security, see supabase/migrations/0001_init.sql),
// not by this file — so even a modified copy of this page can only touch
// the visitor's own data.
//
// Switched off while config.js is empty: then nothing is loaded and the page
// just says "Accounts are not available yet".
(function () {
  "use strict";

  // supabase-js, pinned to one exact version (no surprise updates).
  const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4";
  const MAX_WATCHES = 50;

  // Price types per kind of item, with the same words as the Telegram
  // messages (supabase/functions/send-alerts/alert_rule.js).
  const METRICS = {
    ct_sealed: [
      ["avg5_all", "average of the 5 cheapest (all sellers)"],
      ["min_all", "lowest price (all sellers)"],
      ["avg5_zero", "average of the 5 cheapest (CardTrader Zero)"],
      ["min_zero", "lowest price (CardTrader Zero)"],
    ],
    ct_card: [
      ["ref_nm", "reference price (all sellers)"],
      ["buy_now_nm", "lowest price (all sellers)"],
      ["ref_nm_zero", "reference price (CardTrader Zero)"],
      ["buy_now_nm_zero", "lowest price (CardTrader Zero)"],
    ],
  };

  const config = window.APP_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  let client = null;     // the Supabase connection
  let user = null;       // the signed-in user, or null
  let selected = null;   // the item chosen in the search results
  let watchCount = 0;
  let pollTimer = null;
  let shownUserId;      // whose page is on screen (undefined = nothing yet)

  // ---------- small helpers ----------

  function metricLabel(metric) {
    for (const list of Object.values(METRICS)) {
      for (const [key, label] of list) if (key === metric) return label;
    }
    return metric;
  }

  // 4999 -> "€49.99"
  function euros(cents) {
    if (cents === null || cents === undefined) return "–";
    return "€" + Math.floor(cents / 100) + "." + String(cents % 100).padStart(2, "0");
  }

  // "12,50" or "12.50" -> 1250 cents; "" -> null; nonsense -> NaN
  function parseEuros(text) {
    const clean = text.trim().replace(",", ".");
    if (!clean) return null;
    if (!/^\d{1,7}(\.\d{1,2})?$/.test(clean)) return NaN;
    const cents = Math.round(parseFloat(clean) * 100);
    return cents > 0 ? cents : NaN;
  }

  function kindLabel(item) {
    if (item.source === "ct_sealed") return "sealed";
    return item.foil ? "foil, NM" : "NM";
  }

  // Build an element safely: text is always inserted as text, never as HTML.
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function notice(text, kind) {
    const box = $("notice");
    box.textContent = text || "";
    box.className = kind || "";
  }

  function show(id, visible) { $(id).hidden = !visible; }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = () => reject(new Error("could not load " + src));
      document.head.appendChild(script);
    });
  }

  function friendlyError(error) {
    const text = (error && (error.message || error.details)) || String(error);
    if (/duplicate key|watches_one_per_item_metric/.test(text)) {
      return "You already follow this item with this price type.";
    }
    if (/at most 50/.test(text)) return "You can follow at most 50 items. Remove one first.";
    if (/watches_need_a_threshold/.test(text)) return "Enter at least one threshold.";
    return "Something went wrong: " + text;
  }

  // ---------- start ----------

  async function start() {
    if (!config.supabaseUrl || !config.supabaseAnonKey) return; // switched off
    try {
      await loadScript(SUPABASE_JS);
    } catch (error) {
      $("unavailable-detail").textContent =
        "The sign-in service could not be loaded. Please try again later.";
      return;
    }
    client = window.supabase.createClient(config.supabaseUrl, config.supabaseAnonKey, {
      auth: { flowType: "pkce", persistSession: true, detectSessionInUrl: true },
    });
    show("unavailable", false);
    wireButtons();

    const params = new URLSearchParams(location.search);
    if (params.get("error_description")) {
      notice("Sign-in did not work: " + params.get("error_description"), "error");
    }
    // Let supabase-js finish a sign-in that just came back from Google/GitHub,
    // then tidy the address bar.
    const { data } = await client.auth.getSession();
    if (location.search || location.hash) history.replaceState(null, "", location.pathname);
    await showSession(data.session);
    client.auth.onAuthStateChange((_event, session) => {
      // Run outside the callback, as supabase-js recommends.
      setTimeout(() => showSession(session), 0);
    });
  }

  async function showSession(session) {
    const newUser = session ? session.user : null;
    const newId = newUser ? newUser.id : null;
    if (newId === shownUserId) return; // e.g. a routine token refresh: nothing changed
    shownUserId = newId;
    user = newUser;
    show("signed-out", !user);
    show("signed-in", !!user);
    if (!user) return;
    $("user-email").textContent = user.email || "(no email)";
    await Promise.all([loadTelegramStatus(), loadWatches()]);
  }

  function wireButtons() {
    $("sign-in-google").onclick = () => signIn("google");
    $("sign-in-github").onclick = () => signIn("github");
    $("sign-out").onclick = async () => {
      await client.auth.signOut();
      notice("Signed out.", "ok");
    };
    $("telegram-connect").onclick = connectTelegram;
    $("telegram-disconnect").onclick = disconnectTelegram;
    $("add-watch").onclick = addWatch;
    $("delete-account").onclick = deleteAccount;
    let searchTimer = null;
    $("search").oninput = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(search, 300);
    };
  }

  async function signIn(provider) {
    const { error } = await client.auth.signInWithOAuth({
      provider,
      options: { redirectTo: location.origin + location.pathname },
    });
    if (error) notice("Sign-in did not work: " + error.message, "error");
  }

  // ---------- Telegram ----------

  async function loadTelegramStatus() {
    const { data, error } = await client.from("profiles").select("telegram_chat_id").maybeSingle();
    if (error) return notice(friendlyError(error), "error");
    const connected = !!(data && data.telegram_chat_id);
    const status = $("telegram-status");
    status.textContent = connected ? "connected" : "not connected";
    status.className = connected ? "status-ok" : "";
    show("telegram-disconnect", connected);
    $("telegram-connect").textContent = connected ? "Connect a different chat" : "Connect Telegram";
    if (!config.telegramBotName) {
      $("telegram-connect").disabled = true;
      status.textContent += " (the bot is not set up yet)";
    }
    if (connected) {
      show("telegram-link-box", false);
      stopPolling();
    }
    return connected;
  }

  async function connectTelegram() {
    const { data: code, error } = await client.rpc("create_telegram_link_token");
    if (error) return notice(friendlyError(error), "error");
    const link = "https://t.me/" + encodeURIComponent(config.telegramBotName) +
      "?start=" + encodeURIComponent(code);
    $("telegram-link").href = link;
    show("telegram-link-box", true);
    window.open(link, "_blank", "noopener");
    // Check every 5 seconds (for up to 15 minutes) whether the bot linked us.
    stopPolling();
    let tries = 0;
    pollTimer = setInterval(async () => {
      tries += 1;
      const connected = await loadTelegramStatus();
      if (connected) notice("Telegram connected.", "ok");
      if (connected || tries >= 180) stopPolling();
    }, 5000);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function disconnectTelegram() {
    const { error } = await client.from("profiles")
      .update({ telegram_chat_id: null }).eq("id", user.id);
    if (error) return notice(friendlyError(error), "error");
    notice("Telegram disconnected. You won't get alerts until you connect again.", "ok");
    await loadTelegramStatus();
  }

  // ---------- search and add ----------

  let searchNumber = 0;  // only the newest search may draw its results

  async function search() {
    const mySearch = ++searchNumber;
    const words = $("search").value.replace(/[%_*\\(),."]/g, " ").trim().split(/\s+/)
      .filter(Boolean);
    const box = $("results");
    box.replaceChildren();
    if (words.join("").length < 2) return;
    const { data, error } = await client.from("latest_prices")
      .select("source,item_id,foil,name,date,metrics")
      .ilike("name", "%" + words.join("%") + "%")
      .order("name").limit(30);
    // While we waited, the visitor typed more (or picked an item): a newer
    // search owns the list now, so drop this older answer.
    if (mySearch !== searchNumber) return;
    if (error) return notice(friendlyError(error), "error");
    if (!data.length) box.appendChild(el("li", "No match. Try fewer or different words.", "muted"));
    for (const item of data) {
      const first = METRICS[item.source][0][0];
      const li = el("li");
      li.tabIndex = 0;
      li.appendChild(el("span", item.name + " (" + kindLabel(item) + ")"));
      li.appendChild(el("span", euros(item.metrics[first]) + " on " + item.date, "muted"));
      li.onclick = () => choose(item);
      li.onkeydown = (event) => { if (event.key === "Enter") choose(item); };
      box.appendChild(li);
    }
  }

  function choose(item) {
    selected = item;
    $("selected").textContent = item.name + " (" + kindLabel(item) + ")";
    const select = $("metric");
    select.replaceChildren();
    for (const [key, label] of METRICS[item.source]) {
      const option = el("option", label + " — now " + euros(item.metrics[key]));
      option.value = key;
      select.appendChild(option);
    }
    $("above").value = "";
    $("below").value = "";
    show("add-form", true);
    searchNumber++;  // a search still on its way must not refill the list
    $("results").replaceChildren();
  }

  async function addWatch() {
    if (!selected) return;
    const above = parseEuros($("above").value);
    const below = parseEuros($("below").value);
    if (Number.isNaN(above) || Number.isNaN(below)) {
      return notice("Write prices like 120 or 120.50.", "error");
    }
    if (above === null && below === null) return notice("Enter at least one threshold.", "error");
    if (watchCount >= MAX_WATCHES) {
      return notice("You can follow at most 50 items. Remove one first.", "error");
    }
    // No user_id here on purpose: the database fills it in with the
    // signed-in user and refuses anything else.
    const { error } = await client.from("watches").insert({
      source: selected.source,
      item_id: selected.item_id,
      foil: selected.foil,
      metric: $("metric").value,
      above_cents: above,
      below_cents: below,
    });
    if (error) return notice(friendlyError(error), "error");
    notice("Added: " + selected.name, "ok");
    show("add-form", false);
    $("search").value = "";
    selected = null;
    await loadWatches();
  }

  // ---------- the list of watches ----------

  async function loadWatches() {
    const { data: watches, error } = await client.from("watches")
      .select("id,source,item_id,foil,metric,above_cents,below_cents,above_state,below_state")
      .order("created_at");
    if (error) return notice(friendlyError(error), "error");
    watchCount = watches.length;
    $("watch-count").textContent = "(" + watches.length + " of " + MAX_WATCHES + ")";
    show("no-watches", watches.length === 0);
    show("watch-table", watches.length > 0);

    // Names and latest prices of the watched items.
    const prices = new Map();
    for (const source of Object.keys(METRICS)) {
      const ids = [...new Set(watches.filter((w) => w.source === source).map((w) => w.item_id))];
      if (!ids.length) continue;
      const { data } = await client.from("latest_prices")
        .select("source,item_id,foil,name,date,metrics")
        .eq("source", source).in("item_id", ids);
      for (const row of data || []) prices.set(row.source + ":" + row.item_id + ":" + row.foil, row);
    }

    const rows = $("watch-rows");
    rows.replaceChildren();
    for (const watch of watches) {
      const price = prices.get(watch.source + ":" + watch.item_id + ":" + watch.foil);
      const name = (price ? price.name : "Item " + watch.item_id) + " (" + kindLabel(watch) + ")";
      const tr = el("tr");
      const cell = (label, text) => {
        const td = el("td", text);
        if (label) td.dataset.label = label; // shown as a caption on phones
        tr.appendChild(td);
        return td;
      };
      cell("", name).style.fontWeight = "600";
      cell("Price type", metricLabel(watch.metric));
      cell("Above", thresholdText(watch.above_cents, watch.above_state));
      cell("Below", thresholdText(watch.below_cents, watch.below_state));
      cell("Last snapshot",
        price ? euros(price.metrics[watch.metric]) + " (" + price.date + ")" : "–");
      const actions = cell("");
      const remove = el("button", "Remove");
      remove.onclick = () => removeWatch(watch, name);
      actions.appendChild(remove);
      rows.appendChild(tr);
    }
  }

  function thresholdText(cents, state) {
    if (cents === null) return "–";
    return euros(cents) + (state === "fired" ? " (alert sent)" : "");
  }

  async function removeWatch(watch, name) {
    if (!confirm("Stop following " + name + "?")) return;
    const { error } = await client.from("watches").delete().eq("id", watch.id);
    if (error) return notice(friendlyError(error), "error");
    notice("Removed: " + name, "ok");
    await loadWatches();
  }

  // ---------- delete the account ----------

  async function deleteAccount() {
    const sure = confirm(
      "Delete your account?\n\nThis removes your account, everything you follow, your " +
      "Telegram connection and your alert history. It cannot be undone.");
    if (!sure) return;
    const { error } = await client.rpc("delete_my_account");
    if (error) return notice(friendlyError(error), "error");
    stopPolling();
    await client.auth.signOut({ scope: "local" });
    notice("Your account and all its data have been deleted.", "ok");
  }

  start();
})();
