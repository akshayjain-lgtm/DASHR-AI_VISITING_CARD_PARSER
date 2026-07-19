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

// `bodyWasJson` distinguishes two very different failure modes that both
// land here as "!res.ok": a real API error (FastAPI always responds with a
// JSON {detail: ...} body) vs. a response that never reached our app at all
// — an intermediary (a proxy, or a tunneled dev URL like a Codespaces
// forwarded port) rejecting an oversized/slow request with its own HTML or
// plain-text error page. The latter has no `detail` to show, and "Request
// failed" alone left that indistinguishable from a genuine backend bug.
function extractErrorMessage(body: unknown, bodyWasJson: boolean): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  // FastAPI's default validation-error shape: an array of {loc, msg, type}.
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === "string") return first.msg;
  }
  if (!bodyWasJson) {
    return (
      "Didn't get a response from the server — the request likely never reached it. " +
      "This usually means the file was too large or the connection timed out " +
      "(tunneled dev URLs have their own limits below this app's). Try a smaller file " +
      "or a more direct connection."
    );
  }
  return "Request failed";
}

async function parseErrorBody(res: Response): Promise<{ body: unknown; bodyWasJson: boolean }> {
  try {
    return { body: await res.json(), bodyWasJson: true };
  } catch {
    return { body: null, bodyWasJson: false };
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options.headers },
  });

  if (!res.ok) {
    const { body, bodyWasJson } = await parseErrorBody(res);
    throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
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
    const { body, bodyWasJson } = await parseErrorBody(res);
    throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
  }

  return res.json();
}

export type UserOut = {
  user_id: string;
  name: string | null;
  email: string;
  phone_no: string | null;
  org_id: string | null;
  org_name: string | null;
  role: string | null;
  phone_verified: boolean;
  is_active: boolean;
  admin_name: string | null;
  admin_email: string | null;
};

export function signup(data: {
  name: string;
  email: string;
  phone_no: string;
  password: string;
  company_name?: string;
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
  name: string | null;
  designation: string | null;
  company_name: string | null;
  industry: string | null;
  product_lines: string | null;
  // Pydantic v2 serializes Decimal to a JSON string, not a number.
  last_year_revenue: string | null;
  revenue_currency: string | null;
  target_customer_description: string | null;
  target_regions: string | null;
  gst_no: string | null;
  billing_address: string | null;
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
  // 0-100, null until POST /cards/{id}/score has run at least once
  lead_score: number | null;
  // {designation_score, company_size_score, industry_fit_score, momentum_signal_score, remark_signal_score, total, version}
  score_breakdown: Record<string, number | string> | null;
  scored_at: string | null;
  // True when a field was corrected after this card's last score — a free
  // rescore is allowed (never billed, never counted against the free
  // allowance).
  rescore_available: boolean;
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
  // Public IndiaMART storefront/catalogue URL for this supplier; null until enrichment finds one.
  catalog_url: string | null;
  marketplace_verified_badge: boolean | null;
  marketplace_vintage_years: number | null;
  // IndiaMART supplier-profile fields (Apify "IndiaMart Scraper" actor).
  indiamart_rating: number | null;
  indiamart_rating_count: number | null;
  indiamart_member_since_year: number | null;
  indiamart_business_type: string | null;
  indiamart_employee_count_band: string | null;
  indiamart_annual_turnover_band: string | null;
  indiamart_year_established: string | null;
  indiamart_gst_number: string | null;
  // Only ever observed as a bare year (e.g. "2017"), never a full date.
  indiamart_gst_registration_year: number | null;
  indiamart_call_response_rate: string | null;
};

export type CardEmailOut = {
  email_id: string;
  email: string | null;
  email_type: string | null;
  is_primary: boolean;
};

export type CardPhoneOut = {
  phone_id: string;
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
  lead_score: number | null;
  score_breakdown: Record<string, number | string> | null;
  scored_at: string | null;
  // True when a field was corrected after this card's last score — a free
  // rescore is allowed (never billed, never counted against the free
  // allowance).
  rescore_available: boolean;
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

export type ArchiveUploadOut = {
  archive_id: string;
  exhibition_id: string | null;
  original_filename: string | null;
  // "zip" | "pdf"
  container_type: string;
  // "processing" | "completed" | "completed_with_errors" | "failed"
  status: string;
  error_message: string | null;
  created_at: string;
};

export function uploadArchive(
  exhibitionId: string | null,
  file: File
): Promise<ArchiveUploadOut> {
  const formData = new FormData();
  if (exhibitionId) formData.append("exhibition_id", exhibitionId);
  formData.append("file", file);
  return requestMultipart("/archive-uploads", formData);
}

export function getArchiveUpload(archiveId: string): Promise<ArchiveUploadOut> {
  return request(`/archive-uploads/${archiveId}`);
}

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
  name?: string;
  designation?: string;
  company_name?: string;
  industry?: string;
  product_lines?: string;
  target_customer_description?: string;
  target_regions?: string;
  gst_no?: string;
  billing_address?: string;
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

export type CorrectableFieldName =
  | "full_name"
  | "job_title"
  | "address"
  | "products_offered"
  | "company_name"
  | "email"
  | "phone"
  | "catalog_url";

export type CardFieldCorrectionRequest = {
  field_name: CorrectableFieldName;
  corrected_value: string;
  // Required (must be a CardEmail.email_id/CardPhone.phone_id on this card)
  // when field_name is "email"/"phone"; omitted otherwise.
  record_id?: string | null;
};

export function correctCardField(
  cardId: string,
  data: CardFieldCorrectionRequest
): Promise<CardDetailOut> {
  return request(`/cards/${cardId}/corrections`, {
    method: "POST",
    body: JSON.stringify(data),
  });
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

  const { body, bodyWasJson } = await parseErrorBody(res);
  // 409 is overloaded: a {child_count} body means "cascade confirmation
  // needed" (CardHasMergedChildrenError); any other 409 (e.g. a concurrent
  // merge landed mid-delete) is a generic, retryable ApiError instead.
  if (
    res.status === 409 &&
    typeof (body as { detail?: unknown } | null)?.detail === "object" &&
    (body as { detail: unknown }).detail !== null &&
    "child_count" in (body as { detail: Record<string, unknown> }).detail
  ) {
    throw new CardHasMergedChildrenError(
      Number((body as { detail: { child_count: unknown } }).detail.child_count)
    );
  }
  throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
}

// Same 409-overload handling as deleteCard (a {child_count} body means
// cascade confirmation needed), plus a best-effort skipped_count for ids
// that weren't visible to the caller — not routed through request() for the
// same reason deleteCard isn't.
export async function bulkDeleteCards(
  cardIds: string[],
  confirmCascade = false
): Promise<{ deleted_count: number; skipped_count: number }> {
  const res = await fetch(`${API_URL}/cards/bulk-delete`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_ids: cardIds, confirm_cascade: confirmCascade }),
  });
  const { body, bodyWasJson } = await parseErrorBody(res);
  if (res.ok) return body as { deleted_count: number; skipped_count: number };

  if (
    res.status === 409 &&
    typeof (body as { detail?: unknown } | null)?.detail === "object" &&
    (body as { detail: unknown }).detail !== null &&
    "child_count" in (body as { detail: Record<string, unknown> }).detail
  ) {
    throw new CardHasMergedChildrenError(
      Number((body as { detail: { child_count: unknown } }).detail.child_count)
    );
  }
  throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
}

export function processCards(
  params: { exhibitionId?: string; cardIds?: string[] } = {}
): Promise<{ enqueued_count: number; wallet_blocked_count: number }> {
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
): Promise<{ enqueued_count: number; skipped_count: number; wallet_blocked_count: number }> {
  return request("/cards/enrich-companies", {
    method: "POST",
    body: JSON.stringify({ card_ids: cardIds }),
  });
}

export function scoreCard(cardId: string): Promise<CardOut> {
  return request(`/cards/${cardId}/score`, { method: "POST" });
}

export function scoreCards(
  cardIds: string[]
): Promise<{ enqueued_count: number; skipped_count: number; wallet_blocked_count: number }> {
  return request("/cards/score", {
    method: "POST",
    body: JSON.stringify({ card_ids: cardIds }),
  });
}

// Not routed through request() — the response is a CSV file, not JSON
// (mirrors deleteCard's dedicated-fetch pattern for a response shape the
// generic helper doesn't cover). Triggers a real browser download via a
// temporary anchor element instead of returning the CSV text to the caller.
export async function exportCards(cardIds: string[]): Promise<void> {
  const res = await fetch(`${API_URL}/cards/export`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_ids: cardIds }),
  });

  if (!res.ok) {
    const { body, bodyWasJson } = await parseErrorBody(res);
    throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  // The server's Content-Disposition filename isn't readable here — the
  // CORS middleware has no expose_headers set, so cross-origin JS can't
  // read it. The filename is rebuilt client-side instead, same
  // "dashr-leads-<YYYY-MM-DD>.csv" shape as the server sets.
  link.download = `dashr-leads-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export type WalletTransactionOut = {
  wallet_transaction_id: string;
  // "recharge_credit" | "parse_debit" | "enrichment_debit" | "scoring_debit" | "adjustment"
  transaction_type: string;
  // Pydantic v2 serializes Decimal to a JSON string, not a number.
  amount_inr: string;
  balance_after_inr: string;
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  reference_id: string | null;
  // How many parse/enrich/score actions this row covers — 1 for a single
  // card, >1 for a collective bulk-batch debit.
  quantity: number;
  created_at: string;
};

export type WalletOut = {
  balance_inr: string;
  currency: "INR";
  // Most recent 20 — listWalletTransactions() is the paginated full ledger.
  transactions: WalletTransactionOut[];
  // Each action type's own independent free-action count remaining, floored
  // at 0 once exhausted.
  free_actions_remaining: { parse: number; enrichment: number; scoring: number };
};

export type WalletRechargeOut = {
  razorpay_order_id: string;
  razorpay_key_id: string;
  // Pre-tax amount that will be credited to the wallet on capture.
  net_amount_inr: string;
  cgst_amount_inr: string;
  sgst_amount_inr: string;
  // What the Razorpay Order actually charges (net + 18% GST) — pass this
  // (in paise) as the checkout widget's `amount`, not net_amount_inr.
  gross_amount_inr: string;
  currency: "INR";
};

export function getWallet(): Promise<WalletOut> {
  return request("/wallet");
}

export function listWalletTransactions(
  params: { limit?: number; offset?: number } = {}
): Promise<WalletTransactionOut[]> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return request(`/wallet/transactions${qs ? `?${qs}` : ""}`);
}

export function createWalletRecharge(amountInr: string): Promise<WalletRechargeOut> {
  return request("/wallet/recharge", {
    method: "POST",
    body: JSON.stringify({ amount_inr: amountInr }),
  });
}

export type InvoiceOut = {
  invoice_id: string;
  user_id: string;
  org_id: string | null;
  wallet_transaction_id: string;
  invoice_number: string;
  sac_code: string;
  // Pydantic v2 serializes Decimal to a JSON string, not a number.
  taxable_value_inr: string;
  cgst_rate_percent: string;
  sgst_rate_percent: string;
  cgst_amount_inr: string;
  sgst_amount_inr: string;
  total_inr: string;
  currency: "INR";
  service_description: string;
  bill_to_name: string;
  bill_to_gst_no: string | null;
  bill_to_billing_address: string | null;
  issuer_name: string;
  issuer_gst_no: string;
  issuer_address: string;
  terms_and_conditions: string;
  issued_at: string;
};

export function listInvoices(
  params: { limit?: number; offset?: number } = {}
): Promise<InvoiceOut[]> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return request(`/invoices${qs ? `?${qs}` : ""}`);
}

// Admin-only, org-wide — not currently called from any page in this
// codebase (no admin invoice-review UI exists yet); included since the
// backend endpoint is part of the documented API contract.
export function listOrgInvoices(
  params: { limit?: number; offset?: number } = {}
): Promise<InvoiceOut[]> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  const qs = query.toString();
  return request(`/invoices/org${qs ? `?${qs}` : ""}`);
}

// Dedicated-fetch blob-download pattern, mirroring exportCards — the
// generic request() helper always expects a JSON body, which a PDF isn't.
export async function downloadInvoicePdf(invoiceId: string, invoiceNumber: string): Promise<void> {
  const res = await fetch(`${API_URL}/invoices/${invoiceId}/pdf`, {
    credentials: "include",
  });

  if (!res.ok) {
    const { body, bodyWasJson } = await parseErrorBody(res);
    throw new ApiError(res.status, extractErrorMessage(body, bodyWasJson));
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${invoiceNumber}.pdf`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
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

export type LeadVolumePoint = { date: string; count: number };
export type IndustryMixPoint = { industry: string; count: number };
export type ScoreDistribution = { high: number; medium: number; low: number; unscored: number };
export type ExhibitionPerformance = {
  exhibition_id: string;
  exhibition_name: string | null;
  lead_count: number;
  // avg_score intentionally removed for the time being, until scoring
  // itself is revisited — see .claude/specs/16-dashboard-analytics.md
};
// Raw VisitingCard.designation_level value, or "Unclassified" for null —
// no display-label mapping exists server-side, so the chart component maps it.
export type RoleMixPoint = { role: string; count: number };
export type RegionMixPoint = { region: string; count: number };
export type DashboardAnalyticsOut = {
  lead_volume: LeadVolumePoint[];
  industry_mix: IndustryMixPoint[];
  score_distribution: ScoreDistribution;
  exhibition_performance: ExhibitionPerformance[];
  role_mix: RoleMixPoint[];
  region_mix: RegionMixPoint[];
};

export function getDashboardAnalytics(
  params: { exhibitionIds?: string[]; startDate?: string; endDate?: string } = {}
): Promise<DashboardAnalyticsOut> {
  const query = new URLSearchParams();
  for (const id of params.exhibitionIds ?? []) query.append("exhibition_ids", id);
  if (params.startDate) query.set("start_date", params.startDate);
  if (params.endDate) query.set("end_date", params.endDate);
  const qs = query.toString();
  return request(`/analytics/dashboard${qs ? `?${qs}` : ""}`);
}

export type InvitePreviewOut = {
  org_name: string;
  invitee_email: string;
  status: string;
};

export type InviteOut = {
  invite_id: string;
  email: string;
  role: string;
  status: string;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
};

export type OrgMemberOut = {
  user_id: string;
  name: string | null;
  email: string;
  role: string | null;
  phone_no: string | null;
  phone_verified: boolean;
  is_active: boolean;
  created_at: string;
};

export type MyInviteOut = {
  invite_id: string;
  org_name: string;
  token: string;
  expires_at: string;
};

export function getInvitePreview(token: string): Promise<InvitePreviewOut> {
  return request(`/orgs/invites/${token}`);
}

export function listMyInvites(): Promise<MyInviteOut[]> {
  return request("/orgs/my-invites");
}

export function createInvite(email: string): Promise<InviteOut> {
  return request("/orgs/invites", { method: "POST", body: JSON.stringify({ email }) });
}

export function listInvites(): Promise<InviteOut[]> {
  return request("/orgs/invites");
}

export function revokeInvite(inviteId: string): Promise<void> {
  return request(`/orgs/invites/${inviteId}`, { method: "DELETE" });
}

export function acceptInvite(token: string): Promise<UserOut> {
  return request(`/orgs/invites/${token}/accept`, { method: "POST" });
}

export function listOrgMembers(): Promise<OrgMemberOut[]> {
  return request("/orgs/members");
}

export function deactivateMember(userId: string): Promise<OrgMemberOut> {
  return request(`/orgs/members/${userId}/deactivate`, { method: "PATCH" });
}

export function reactivateMember(userId: string): Promise<OrgMemberOut> {
  return request(`/orgs/members/${userId}/reactivate`, { method: "PATCH" });
}

export function makeAdmin(userId: string): Promise<void> {
  return request(`/orgs/members/${userId}/make-admin`, { method: "POST" });
}

export function submitContactEnquiry(data: {
  name: string;
  phone_no: string;
  email: string;
  query: string;
}): Promise<void> {
  return request("/contact", { method: "POST", body: JSON.stringify(data) });
}
