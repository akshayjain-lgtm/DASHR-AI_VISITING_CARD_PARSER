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

// Multipart requests must NOT set a Content-Type header — the browser sets
// `multipart/form-data; boundary=...` itself, and overriding it (as
// request()'s default does) breaks multipart parsing on the server.
async function requestMultipart<T>(path: string, formData: FormData): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    credentials: "include",
    body: formData,
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, extractErrorMessage(body));
  }

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

export type ExhibitionOut = {
  exhibition_id: string;
  name: string | null;
  location: string | null;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
};

export type CardOut = {
  card_id: string;
  user_id: string;
  exhibition_id: string | null;
  original_filename: string | null;
  image_url: string;
  status: string;
  full_name: string | null;
  job_title: string | null;
  created_at: string;
};

export type BulkUploadResponse = {
  batch_size: number;
  cards: {
    card_id: string;
    original_filename: string | null;
    status: string;
    exhibition_id: string | null;
  }[];
};

export function listExhibitions(): Promise<ExhibitionOut[]> {
  return request("/exhibitions");
}

export function createExhibition(data: {
  name: string;
  location?: string;
  start_date?: string;
  end_date?: string;
}): Promise<ExhibitionOut> {
  return request("/exhibitions", { method: "POST", body: JSON.stringify(data) });
}

export function uploadCards(
  exhibitionId: string | null,
  files: File[]
): Promise<BulkUploadResponse> {
  const formData = new FormData();
  if (exhibitionId) formData.append("exhibition_id", exhibitionId);
  files.forEach((file) => formData.append("files", file));
  return requestMultipart("/cards/bulk-upload", formData);
}

export function listCards(
  params: {
    exhibition_id?: string;
    status?: string;
    limit?: number;
    offset?: number;
  } = {}
): Promise<CardOut[]> {
  const query = new URLSearchParams();
  if (params.exhibition_id) query.set("exhibition_id", params.exhibition_id);
  if (params.status) query.set("status", params.status);
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return request(`/cards${qs ? `?${qs}` : ""}`);
}
