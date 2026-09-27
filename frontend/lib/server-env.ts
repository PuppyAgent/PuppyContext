/**
 * True iff `next` is a safe same-origin relative path — i.e. safe to
 * concatenate onto an origin for a redirect without allowing an open
 * redirect to an attacker-controlled host.
 *
 * Rejects:
 *   - non-string / empty
 *   - anything not starting with `/` (absolute URLs, `http:…`, etc.)
 *   - `//evil.com` (protocol-relative — browsers treat as absolute)
 *   - `/\evil.com` (backslash variant some parsers normalise to `//`)
 */
export function isSafeRelativePath(next: string | null | undefined): next is string {
  if (!next || typeof next !== 'string') return false;
  if (!next.startsWith('/')) return false;
  if (next.startsWith('//')) return false; // protocol-relative
  if (next.startsWith('/\\')) return false; // backslash → protocol-relative
  return true;
}

export function getServerApiBaseUrl(): string {
  return requireConfiguredHttpUrl(
    process.env.API_INTERNAL_URL || process.env.NEXT_PUBLIC_API_URL,
    'API_INTERNAL_URL or NEXT_PUBLIC_API_URL must configure the backend API URL',
  );
}

export function getServerSupabaseUrl(): string {
  return requireConfiguredHttpUrl(
    process.env.SUPABASE_INTERNAL_URL || process.env.NEXT_PUBLIC_SUPABASE_URL,
    'SUPABASE_INTERNAL_URL or NEXT_PUBLIC_SUPABASE_URL must configure Supabase',
  );
}

export function getPublicSupabaseUrl(): string {
  return requireConfiguredHttpUrl(
    process.env.NEXT_PUBLIC_SUPABASE_URL,
    'NEXT_PUBLIC_SUPABASE_URL must configure the public Supabase identity',
  );
}

export function getSupabaseAnonKey(): string {
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!anonKey) {
    throw new Error('NEXT_PUBLIC_SUPABASE_ANON_KEY must be set');
  }
  return anonKey;
}

/**
 * Resolve the public-facing origin for server-side redirects.
 *
 * Behind a reverse proxy, request/forwarded host headers are deployment input
 * and must not become an authentication redirect authority. The public origin
 * is explicit configuration so callbacks cannot be redirected by a spoofed
 * Host header.
 */
export function getRequestOrigin(_request: Request): string {
  const siteUrl = process.env.NEXT_PUBLIC_APP_URL;
  if (!siteUrl) throw new Error('NEXT_PUBLIC_APP_URL must configure the frontend origin');
  return requirePublicAppOrigin(siteUrl);
}

function requireConfiguredHttpUrl(value: string | undefined, message: string): string {
  if (!value) throw new Error(message);
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(message);
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error(message);
  }
  return url.toString().replace(/\/$/, '');
}

function requirePublicAppOrigin(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error('NEXT_PUBLIC_APP_URL must configure a valid frontend origin');
  }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (
    (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
    || url.username
    || url.password
    || (url.pathname !== '/' && url.pathname !== '')
    || url.search
    || url.hash
  ) {
    throw new Error(
      'NEXT_PUBLIC_APP_URL must be an HTTPS origin, or a loopback HTTP origin for local development',
    );
  }
  return url.origin;
}
