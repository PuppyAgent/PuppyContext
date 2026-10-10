import { createBrowserClient } from '@supabase/ssr';
import { createRequestScope, withAbort } from './requestScope';
import { API_BASE_URL } from '@/config/api';
import { scopedResponseBody } from './responseStream';
import {
  REPOSITORY_TARGET_CONTRACT_HEADER,
  REPOSITORY_TARGET_CONTRACT_VERSION,
} from '@puppyone/cloud-core';

/**
 * Shared API client.
 * Automatically attaches Authorization header.
 */

const DEFAULT_API_TIMEOUT_MS = 30_000;
const BROWSER_API_PROXY_PREFIX = '/api/backend';

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
}

function normalizeRelativeApiEndpoint(endpoint: string): string {
  const queryStart = endpoint.indexOf('?');
  const rawPath = queryStart >= 0 ? endpoint.slice(0, queryStart) : endpoint;
  const query = queryStart >= 0 ? endpoint.slice(queryStart) : '';
  const path = rawPath.startsWith('/') ? rawPath : `/${rawPath}`;
  const collapsedPath = path.replace(/\/{2,}/g, '/');
  const canonicalPath =
    collapsedPath.length > 1 ? collapsedPath.replace(/\/+$/g, '') : collapsedPath;
  return `${canonicalPath}${query}`;
}

export class ApiNetworkError extends Error {
  status = 0;
  code = 'NETWORK_ERROR';
  isNetworkError = true;
  endpoint: string;
  url: string;
  cause: unknown;

  constructor(message: string, context: { endpoint: string; url: string; cause: unknown }) {
    super(message);
    this.name = 'ApiNetworkError';
    this.endpoint = context.endpoint;
    this.url = context.url;
    this.cause = context.cause;
  }
}

function buildApiUrl(endpoint: string): string {
  if (/^https?:\/\//i.test(endpoint)) return endpoint;
  const path = normalizeRelativeApiEndpoint(endpoint);
  if (typeof window !== 'undefined') {
    return `${BROWSER_API_PROXY_PREFIX}${path}`;
  }
  const base = API_BASE_URL.replace(/\/+$/, '');
  return `${base}${path}`;
}

function getNetworkErrorMessage(args: {
  url: string;
  endpoint: string;
  cause: unknown;
  timeoutMs: number;
}): string {
  const { url, endpoint, cause, timeoutMs } = args;
  const isAbort =
    typeof DOMException !== 'undefined' && cause instanceof DOMException
      ? cause.name === 'AbortError'
      : cause instanceof Error && cause.name === 'AbortError';

  const hints: string[] = [];
  try {
    const currentOrigin = typeof window !== 'undefined' ? window.location.origin : '';
    const target = currentOrigin ? new URL(url, currentOrigin) : new URL(url);
    const current = currentOrigin ? new URL(currentOrigin) : null;
    if (target.hostname === 'localhost' || target.hostname === '127.0.0.1') {
      hints.push(`make sure the backend is running at ${target.origin}`);
    }
    if (current && target.origin !== current.origin) {
      hints.push('if the backend is reachable, check CORS, HTTPS, and mixed-content settings');
    }
    if (
      typeof window !== 'undefined' &&
      window.location.hostname !== 'localhost' &&
      target.hostname === 'localhost'
    ) {
      hints.push('remote deployments cannot use localhost as NEXT_PUBLIC_API_URL');
    }
  } catch {
    // ignore malformed URLs; the failed URL is still included below.
  }

  let reason = 'network request failed';
  if (cause instanceof Error && cause.message) reason = cause.message;
  if (isAbort) reason = `request timed out (${Math.round(timeoutMs / 1000)}s)`;
  const hintText = hints.length ? `. ${hints.join('; ')}` : '';
  return `Unable to reach the backend API: ${endpoint} -> ${url} (${reason})${hintText}`;
}

function _initSupabase() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) {
    throw new Error(
      'NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY must be set'
    );
  }
  return createBrowserClient(url, key);
}

let _supabase: ReturnType<typeof _initSupabase> | null = null;

function getSupabase() {
  if (!_supabase) {
    _supabase = _initSupabase();
  }
  return _supabase;
}

/**
 * Cached access token + expiry — kept in sync with auth state changes.
 *
 * PERFORMANCE (P-6): 10 concurrent SWR requests previously caused 10
 * sequential `getSession()` calls. We now subscribe to onAuthStateChange
 * so the cached token is updated proactively, and getAuthToken() returns
 * synchronously while the cache is fresh.
 */
let _cachedToken: string | null = null;
let _cacheValidUntilMs = 0;
let _authSubscribed = false;
let _inflightFetch: Promise<string | null> | null = null;

function _setCacheFromSession(session: { access_token?: string; expires_at?: number } | null | undefined) {
  if (session?.access_token) {
    _cachedToken = session.access_token;
    // expires_at is in seconds (epoch). Refresh 60s before actual expiry.
    _cacheValidUntilMs = session.expires_at ? session.expires_at * 1000 : Date.now() + 30_000;
  } else {
    _cachedToken = null;
    _cacheValidUntilMs = 0;
  }
}

function _ensureAuthSubscription() {
  if (_authSubscribed) return;
  _authSubscribed = true;
  try {
    getSupabase().auth.onAuthStateChange((_event, session) => {
      _setCacheFromSession(session as any);
    });
  } catch {
    // ignore — fall back to per-call getSession()
  }
}

async function getAuthToken(): Promise<string | null> {
  _ensureAuthSubscription();

  // Fast path: cache fresh (60s buffer before expiry)
  if (_cachedToken && Date.now() < _cacheValidUntilMs - 60_000) {
    return _cachedToken;
  }

  // Coalesce: concurrent callers share a single in-flight getSession().
  if (_inflightFetch) return _inflightFetch;
  _inflightFetch = (async () => {
    try {
      const { data } = await getSupabase().auth.getSession();
      _setCacheFromSession(data.session as any);
      return _cachedToken;
    } finally {
      _inflightFetch = null;
    }
  })();
  return _inflightFetch;
}

/**
 * Exposed for other API clients.
 */
export async function getApiAccessToken(): Promise<string | null> {
  return getAuthToken();
}

// Alias for backward compatibility
export const getAccessToken = getApiAccessToken;

// ── session recovery on 401 ───────────────────────────────────────────
// A Supabase access token lives ~1h. When it lapses, the backend returns
// 401 on every call; without recovery the UI just renders empty (orgs +
// projects "disappear", pages stick on loading). On a 401 we force a
// one-time token refresh and retry; if that fails the session is truly
// dead and we redirect to /login instead of silently failing.
let _inflightRefresh: Promise<string | null> | null = null;
let _redirectingToLogin = false;

/** Force-refresh the Supabase session (coalesced). Returns the new token or null. */
async function refreshAuthToken(): Promise<string | null> {
  _cachedToken = null;
  _cacheValidUntilMs = 0;
  if (_inflightRefresh) return _inflightRefresh;
  _inflightRefresh = (async () => {
    try {
      const { data, error } = await getSupabase().auth.refreshSession();
      if (error || !data.session) return null;
      _setCacheFromSession(data.session as any);
      return _cachedToken;
    } catch {
      return null;
    } finally {
      _inflightRefresh = null;
    }
  })();
  return _inflightRefresh;
}

/** Session is unrecoverable: clear it and send the user to /login (once). */
function handleAuthFailure(): void {
  _cachedToken = null;
  _cacheValidUntilMs = 0;
  if (typeof window === 'undefined' || _redirectingToLogin) return;
  if (window.location.pathname.startsWith('/login')) return;
  _redirectingToLogin = true;
  const go = () => {
    window.location.href = '/login?session=expired';
  };
  try {
    Promise.resolve(getSupabase().auth.signOut({ scope: 'local' })).finally(go);
  } catch {
    go();
  }
}

interface ApiResponse<T> {
  code: number;
  message: string;
  data: T;
}

/**
 * Authenticated API request.
 */
export async function apiRequest<T>(endpoint: string, options?: ApiRequestOptions): Promise<T> {
  const { timeoutMs = DEFAULT_API_TIMEOUT_MS, ...fetchOptions } = options ?? {};
  const scope = createRequestScope(fetchOptions.signal, timeoutMs);
  try {
    return await performRequest<T>(endpoint, { ...fetchOptions, timeoutMs, signal: scope.signal });
  } finally {
    scope.dispose();
  }
}

/** Authenticated SSE transport with the same token refresh, proxy and errors as JSON.
 * The request scope remains alive until consumption/cancellation of the body. */
export async function apiStreamRequest(endpoint: string, options: ApiRequestOptions = {}): Promise<Response> {
  const { timeoutMs = 65_000, ...fetchOptions } = options;
  const scope = createRequestScope(fetchOptions.signal, timeoutMs);
  try {
    const response = await performRequest<Response>(endpoint,
      { ...fetchOptions, timeoutMs, signal: scope.signal }, false, 'stream');
    if (!response.body) { scope.dispose(); return response; }
    return new Response(scopedResponseBody(response.body, scope), {
      status: response.status, headers: response.headers,
    });
  } catch (error) { scope.dispose(); throw error; }
}

async function performRequest<T>(
  endpoint: string,
  options: ApiRequestOptions & { signal: AbortSignal },
  _isRetry = false,
  responseMode: 'json' | 'stream' = 'json',
): Promise<T> {
  const token = await withAbort(getAuthToken(), options.signal);
  const { timeoutMs = DEFAULT_API_TIMEOUT_MS, ...fetchOptions } = options ?? {};
  const url = buildApiUrl(endpoint);

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    [REPOSITORY_TARGET_CONTRACT_HEADER]: REPOSITORY_TARGET_CONTRACT_VERSION,
    ...(fetchOptions.headers as Record<string, string>),
  };

  // Attach Authorization header when available.
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...fetchOptions,
      headers,
      signal: options.signal,
    });
  } catch (cause) {
    if (options.signal.aborted) throw options.signal.reason;
    throw new ApiNetworkError(
      getNetworkErrorMessage({ url, endpoint, cause, timeoutMs }),
      { endpoint, url, cause }
    );
  }

  if (response.status === 401) {
    void response.body?.cancel();
    // The access token likely expired. Try a one-time refresh + retry before
    // giving up — this is what stops a long-open tab from silently showing an
    // empty account once the ~1h token lapses.
    if (!_isRetry && typeof window !== 'undefined') {
      const refreshed = await withAbort(refreshAuthToken(), options.signal);
      if (refreshed) {
        return performRequest<T>(endpoint, options, true, responseMode);
      }
    }
    // Refresh failed (or we already retried) → the session is unrecoverable.
    handleAuthFailure();
    const error: any = new Error('You are not signed in, or your session has expired.');
    error.response = response;
    error.code = 401;
    error.status = 401;
    throw error;
  }

  if (!response.ok) await rejectApiResponse(response, options.signal);

  if (responseMode === 'stream') return response as T;

  const data: ApiResponse<T> = await withAbort(response.json(), options.signal);

  if (data.code !== 0) {
    const error: any = new Error(data.message || 'API request failed');
    error.response = response;
    error.data = data.data;
    error.code = data.code;
    throw error;
  }

  return data.data;
}

/**
 * GET request.
 */
export function get<T>(endpoint: string, options?: ApiRequestOptions): Promise<T> {
  return apiRequest<T>(endpoint, { ...options, method: 'GET' });
}

/**
 * POST request.
 */
export function post<T>(endpoint: string, body?: unknown): Promise<T> {
  return apiRequest<T>(endpoint, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  });
}

/**
 * PUT request.
 */
export function put<T>(endpoint: string, body?: unknown): Promise<T> {
  return apiRequest<T>(endpoint, {
    method: 'PUT',
    body: body ? JSON.stringify(body) : undefined,
  });
}

/**
 * DELETE request.
 */
export function del<T>(endpoint: string): Promise<T> {
  return apiRequest<T>(endpoint, { method: 'DELETE' });
}

/**
 * PATCH request.
 */
export function patch<T>(endpoint: string, body?: unknown): Promise<T> {
  return apiRequest<T>(endpoint, {
    method: 'PATCH',
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function rejectApiResponse(response: Response, signal: AbortSignal): Promise<never> {
  let body: any = null;
  try { body = JSON.parse(await withAbort(response.text(), signal)); } catch (error) {
    if (signal.aborted) throw signal.reason;
  }

  // Puppyone custom format: {"code": N, "message": "...", "data": null}
  // FastAPI standard format: {"detail": "..." | {...}}
  const puppyoneMsg: string | undefined = body?.message;
  const fastApiDetail = body?.detail;

  // Extract the human-readable message
  let errorMsg = `HTTP ${response.status}`;
  if (puppyoneMsg) {
    errorMsg = puppyoneMsg;
  } else if (typeof fastApiDetail === 'string') {
    errorMsg = fastApiDetail;
  } else if (fastApiDetail?.message) {
    errorMsg = fastApiDetail.message;
  }

  // Detect duplicate: check multiple signals
  const isDuplicate = response.status === 409 && (
    fastApiDetail?.error === 'duplicate_access_point' ||
    (typeof puppyoneMsg === 'string' && (
      puppyoneMsg.includes('duplicate_access_point') ||
      puppyoneMsg.includes('already exists')
    ))
  );

  // If it's a dup, extract the inner message from Python dict string
  // e.g. "{'error': '...', 'message': 'A sync already exists...'}"
  let cleanMsg = errorMsg;
  if (isDuplicate && puppyoneMsg) {
    const m = puppyoneMsg.match(/['"]message['"]\s*:\s*['"](.*?)['"]\s*[,}]/s);
    if (m) cleanMsg = m[1];
  }

  const error: any = new Error(cleanMsg);
  error.status = response.status;
  error.code = body?.code ?? response.status;
  error.detail = fastApiDetail ?? body;
  error.isDuplicate = isDuplicate;
  throw error;
}
