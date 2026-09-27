import { NextResponse } from 'next/server';
import { createAuthServerClient } from '@/features/auth/supabase/server-client';
import { cookies } from 'next/headers';
import type { EmailOtpType } from '@supabase/supabase-js';
import {
  getRequestOrigin,
  isSafeRelativePath,
} from '@/lib/server-env';

/**
 * Supabase Email Verification - Route Handler (服务端)
 *
 * 仅处理密码重置 (type=recovery) 和邮箱变更 (type=email_change) 的链接回调。
 * 注册邮箱确认 (type=signup) 已完全迁移到 OTP 验证码流程，由前端
 * `/login` 页面的 verify-otp 视图直接调用 verifyOtp({ token, ... }) 完成。
 *
 * 邮件链接格式：/auth/confirm?token_hash=xxx&type=recovery&next=/reset-password
 */
export async function GET(request: Request) {
  const requestUrl = new URL(request.url);
  const origin = getRequestOrigin(request);
  const token_hash = requestUrl.searchParams.get('token_hash');
  const type = requestUrl.searchParams.get('type') as EmailOtpType | null;
  // Ignore attacker-controlled `next` values (e.g. `//evil.com`) that would
  // escape the origin in the `${origin}${next}` redirect below; fall back to
  // the safe default when it isn't a same-origin relative path.
  const rawNext = requestUrl.searchParams.get('next');
  const next = isSafeRelativePath(rawNext) ? rawNext : '/home';

  if (!token_hash || !type) {
    console.error('Auth confirm: missing token_hash or type');
    return NextResponse.redirect(`${origin}/login?error=invalid_confirmation_link`);
  }

  // Signup flow is fully OTP — the email no longer contains a link. If a stale
  // (pre-migration) link reaches this handler, redirect to /login so the user
  // can request a fresh OTP code via "Verify your email".
  if (type === 'signup') {
    console.warn('Auth confirm: received deprecated signup link; redirecting to /login');
    return NextResponse.redirect(`${origin}/login?error=signup_link_deprecated`);
  }

  const cookieStore = await cookies();

  const supabase = createAuthServerClient({
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: values =>
        values.forEach(({ name, value, options }) => cookieStore.set(name, value, options)),
    },
  });

  const { error } = await supabase.auth.verifyOtp({ token_hash, type });

  if (error) {
    console.error('Auth confirm verifyOtp failed:', error.message);
    return NextResponse.redirect(`${origin}/login?error=confirmation_failed`);
  }

  if (type === 'recovery') {
    return NextResponse.redirect(`${origin}/reset-password`);
  }

  // email_change and any other supported types
  return NextResponse.redirect(`${origin}${next}`);
}
