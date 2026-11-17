// Settings for the account page (docs/account.html).
//
// While supabaseUrl is empty, user accounts are switched OFF: the account
// page only says "Accounts are not available yet" and loads nothing else
// (no Supabase library, no requests). That is the state today.
//
// To switch accounts on, follow SETUP_ACCOUNTS.md. Only ever put the PUBLIC
// "anon" (or "publishable") key here. NEVER the service_role / secret key:
// this file is public on the internet.
window.APP_CONFIG = {
  supabaseUrl: "",       // e.g. "https://abcdefghijklmnop.supabase.co"
  supabaseAnonKey: "",   // the anon / publishable key (safe to publish)
  telegramBotName: "",   // the bot's username without the @, e.g. "MtgPriceAlertsBot"
};
