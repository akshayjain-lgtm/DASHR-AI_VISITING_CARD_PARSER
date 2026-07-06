// Relative, same-origin path — proxied server-side to apps/api via the
// rewrite in next.config.mjs. Never call apps/api directly from the browser:
// that makes the session cookie third-party and browsers will block it.
const API_URL = "/api";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function extractErrorMessage(body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  // FastAPI's default validation-error shape: an array of {loc, msg, type}.
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === "string") return first.msg;
  }
  return "Request failed";
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options.headers },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, extractErrorMessage(body));
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

export type UserOut = {
  user_id: string;
  name: string | null;
  email: string;
  phone_no: string | null;
  org_id: string | null;
  role: string | null;
  phone_verified: boolean;
};

export function signup(data: {
  name: string;
  email: string;
  phone_no: string;
  password: string;
}): Promise<{ user_id: string; phone_no: string }> {
  return request("/auth/signup", { method: "POST", body: JSON.stringify(data) });
}

export function verifyOtp(data: {
  user_id: string;
  otp_code: string;
}): Promise<UserOut> {
  return request("/auth/signup/verify-otp", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function resendOtp(data: { user_id: string }): Promise<void> {
  return request("/auth/signup/resend-otp", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function login(data: { email: string; password: string }): Promise<UserOut> {
  return request("/auth/login", { method: "POST", body: JSON.stringify(data) });
}

export function logout(): Promise<void> {
  return request("/auth/logout", { method: "POST" });
}

export function me(): Promise<UserOut> {
  return request("/auth/me");
}
