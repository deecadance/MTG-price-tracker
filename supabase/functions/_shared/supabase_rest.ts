// Small helpers shared by the two Edge Functions (folders starting with "_"
// are not deployed as functions of their own; the others import from here).
//
// They talk to our Supabase database through its web API ("PostgREST")
// with the service-role key, which Supabase gives every Edge Function
// automatically as SUPABASE_SERVICE_ROLE_KEY. That key skips the row
// rules, so it only ever lives on Supabase's servers — never in the website.

/** Read a required setting (a "secret" set with `supabase secrets set`). */
export function setting(name: string): string {
  const value = Deno.env.get(name);
  if (!value) throw new Error(`missing setting ${name}`);
  return value;
}

/**
 * Compare a received secret with the expected one without leaking, through
 * timing, how many characters were right.
 */
export function sameSecret(received: string | null, expected: string): boolean {
  if (!received || !expected) return false;
  const a = new TextEncoder().encode(received);
  const b = new TextEncoder().encode(expected);
  let difference = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) difference |= (a[i] ?? 0) ^ b[i];
  return difference === 0;
}

/** The headers that prove we are the service role. */
function keyHeaders(key: string): Record<string, string> {
  const headers: Record<string, string> = { apikey: key };
  // Classic keys are JWTs (they start with "eyJ") and also go in
  // Authorization. Newer "sb_secret_..." keys go only in the apikey header.
  if (key.startsWith("eyJ")) headers.Authorization = `Bearer ${key}`;
  return headers;
}

export class Database {
  constructor(private url: string, private key: string) {}

  /** Uses the URL and key Supabase provides to every Edge Function. */
  static fromEnvironment(): Database {
    return new Database(setting("SUPABASE_URL"), setting("SUPABASE_SERVICE_ROLE_KEY"));
  }

  /** One request to the database API. Throws a readable error if it fails. */
  async request(
    method: string,
    path: string,
    body?: unknown,
    extraHeaders: Record<string, string> = {},
  ): Promise<unknown> {
    const response = await fetch(`${this.url}/rest/v1/${path}`, {
      method,
      headers: { ...keyHeaders(this.key), "Content-Type": "application/json", ...extraHeaders },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) {
      const where = path.split("?")[0];
      throw new Error(`database ${method} ${where} failed (${response.status}): ${text.slice(0, 300)}`);
    }
    return text ? JSON.parse(text) : null;
  }

  /**
   * Read ALL matching rows. Supabase hands out at most 1000 rows per
   * request, so this asks page after page until a page comes back empty.
   * `query` is a PostgREST filter such as "select=*&user_id=eq.123".
   */
  async selectAll<T>(table: string, query: string, orderBy: string): Promise<T[]> {
    const rows: T[] = [];
    for (;;) {
      const page = await this.request(
        "GET",
        `${table}?${query}&order=${orderBy}&limit=1000&offset=${rows.length}`,
      ) as T[];
      if (page.length === 0) return rows;
      rows.push(...page);
    }
  }

  /** Call one of the SQL functions defined in migrations/0001_init.sql. */
  rpc(name: string, args: Record<string, unknown> = {}): Promise<unknown> {
    return this.request("POST", `rpc/${name}`, args);
  }
}

/** A JSON reply for whoever called the function. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
