import { ApiError, me, type UserOut } from "./api";

// Must match COOKIE_NAME in apps/api/app/deps.py — no shared config package
// exists between the two apps yet, so this is a manually-synced constant.
export const SESSION_COOKIE_NAME = "dashr_session";

// Presence-only check for routing UX (middleware). Does NOT validate the
// JWT — real enforcement happens server-side via get_current_user, which
// 401s on any forged/expired/tampered token.
export function hasSessionCookie(cookieValue: string | undefined): boolean {
  return Boolean(cookieValue);
}

// Calls GET /auth/me to fetch the real session user. Unlike
// hasSessionCookie, this does verify the session server-side — use it in
// client components/pages that need actual user data, not just routing UX.
export async function getCurrentUser(): Promise<UserOut | null> {
  try {
    return await me();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return null;
    throw err;
  }
}
