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

// Thrown by deleteCard when the target card has merged/duplicate children
// and the caller hasn't confirmed the cascade yet (API responds 409 with a
// {child_count} body). Distinct from ApiError so callers can show a second,
// cascade-specific confirmation instead of a generic error. Carries only
// childCount, not a pre-built message — useDeleteCardConfirm is the single
// place that turns it into confirmation-dialog copy, so the wording isn't
// duplicated across every call site.
export class CardHasMergedChildrenError extends Error {
  childCount: number;

  constructor(childCount: number) {
    super(`Card has ${childCount} merged children`);
    this.childCount = childCount;
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

export type SellerProfileOut = {
  profile_id: string | null;
  company_name: string | null;
  industry: string | null;
  product_lines: string | null;
  // Pydantic v2 serializes Decimal to a JSON string, not a number.
  last_year_revenue: string | null;
  revenue_currency: string | null;
  target_customer_description: string | null;
  target_regions: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type CardOut = {
  card_id: string;
  user_id: string;
  exhibition_id: string | null;
  original_filename: string | null;
  image_url: string;
  // "new" | "processing" | "extracted" | "failed" | "duplicate" | "merged"
  status: string;
  full_name: string | null;
  job_title: string | null;
  merged_into_card_id: string | null;
  created_at: string;
  company_id: string | null;
  // Mirrors Company.name, null if no company linked yet
  company_name: string | null;
  // "pending" | "enriching" | "enriched" | "not_found" | "failed", null if no company linked yet
  company_enrichment_status: string | null;
};

export type CardCompanyOut = {
  company_id: string;
  name: string | null;
  domain: string | null;
  website: string | null;
  // "pending" | "enriching" | "enriched" | "not_found" | "failed"
  enrichment_status: string;
  summary: string | null;
  summary_generated_at: string | null;
  linkedin_employee_count: number | null;
  estimated_revenue_band: string | null;
  gstin_verified: boolean | null;
  udyam_registered: boolean | null;
  hiring_signal: string | null;
  google_rating: number | null;
};

export type CardEmailOut = {
  email: string | null;
  email_type: string | null;
  is_primary: boolean;
};

export type CardPhoneOut = {
  phone_e164: string | null;
  phone_raw: string | null;
  phone_type: string | null;
  is_primary: boolean;
};

export type CardDetailOut = {
  card_id: string;
  user_id: string;
  exhibition_id: string | null;
  original_filename: string | null;
  image_url: string;
  // "new" | "processing" | "extracted" | "failed" | "duplicate" | "merged"
  status: string;
  full_name: string | null;
  job_title: string | null;
  designation_level: string | null;
  special_remark: string | null;
  website: string | null;
  address: string | null;
  products_offered: string | null;
  gst_number: string | null;
  raw_ocr_text: string | null;
  extraction_error: string | null;
  merged_into_card_id: string | null;
  created_at: string;
  company: CardCompanyOut | null;
  emails: CardEmailOut[];
  phones: CardPhoneOut[];
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

export function getProfile(): Promise<SellerProfileOut> {
  return request("/profile");
}

export function updateProfile(data: {
  company_name?: string;
  industry?: string;
  product_lines?: string;
  target_customer_description?: string;
  target_regions?: string;
}): Promise<SellerProfileOut> {
  return request("/profile", { method: "PUT", body: JSON.stringify(data) });
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

export function getCard(cardId: string): Promise<CardDetailOut> {
  return request(`/cards/${cardId}`);
}

export function reprocessCard(cardId: string): Promise<CardOut> {
  return request(`/cards/${cardId}/reprocess`, { method: "POST" });
}

export function enrichCompany(cardId: string): Promise<CardOut> {
  return request(`/cards/${cardId}/enrich-company`, { method: "POST" });
}

// Not routed through request() — a 409 here means "cascade confirmation
// needed", not a generic failure, and needs its own body-shaped handling
// (mirrors requestMultipart's existing pattern of a dedicated fetch for a
// response shape the generic helper doesn't cover).
export async function deleteCard(cardId: string, confirmCascade = false): Promise<void> {
  const query = confirmCascade ? "?confirm_cascade=true" : "";
  const res = await fetch(`${API_URL}/cards/${cardId}${query}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (res.status === 204) return;

  const body = await res.json().catch(() => ({}));
  // 409 is overloaded: a {child_count} body means "cascade confirmation
  // needed" (CardHasMergedChildrenError); any other 409 (e.g. a concurrent
  // merge landed mid-delete) is a generic, retryable ApiError instead.
  if (
    res.status === 409 &&
    typeof body?.detail === "object" &&
    body.detail !== null &&
    "child_count" in body.detail
  ) {
    throw new CardHasMergedChildrenError(Number(body.detail.child_count));
  }
  throw new ApiError(res.status, extractErrorMessage(body));
}

export function processCards(
  params: { exhibitionId?: string; cardIds?: string[] } = {}
): Promise<{ enqueued_count: number }> {
  return request("/cards/process", {
    method: "POST",
    body: JSON.stringify({
      exhibition_id: params.exhibitionId ?? null,
      card_ids: params.cardIds ?? null,
    }),
  });
}

export function enrichCompanies(
  cardIds: string[]
): Promise<{ enqueued_count: number; skipped_count: number }> {
  return request("/cards/enrich-companies", {
    method: "POST",
    body: JSON.stringify({ card_ids: cardIds }),
  });
}

export function listCards(
  params: {
    exhibition_id?: string;
    status?: string;
    include_folded?: boolean;
    // Filters to cards with no exhibition assigned (the upload page's
    // "General capture" filter) — mutually exclusive with exhibition_id.
    unassigned?: boolean;
    limit?: number;
    offset?: number;
  } = {}
): Promise<CardOut[]> {
  const query = new URLSearchParams();
  if (params.exhibition_id) query.set("exhibition_id", params.exhibition_id);
  if (params.status) query.set("status", params.status);
  if (params.include_folded) query.set("include_folded", "true");
  if (params.unassigned) query.set("unassigned", "true");
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return request(`/cards${qs ? `?${qs}` : ""}`);
}
