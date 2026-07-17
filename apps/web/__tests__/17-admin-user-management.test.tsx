// Tests for the `17-admin-user-management` feature (spec:
// `.claude/specs/17-admin-user-management.md`), written directly against the
// spec's documented frontend contract:
//
//   - `/settings` renders a full Team-management UI (invite form, pending
//     invites table, members table with Deactivate/Reactivate/Make Admin row
//     actions) for an admin; a read-only "you are a Member" panel for a
//     non-admin org member; and a "not part of an organization yet"
//     placeholder for an org-less user. It never shows management controls
//     to a non-admin.
//   - The login/signup page reads an `?invite=<token>` query param: shows a
//     "you're invited to join {org_name}" banner, hides the Company Name
//     field in signup mode (joining an existing org creates no new one),
//     and calls the invite-accept endpoint after a successful login or
//     OTP-verified signup, before redirecting to /dashboard.
//
// `global.fetch` is mocked end-to-end for every test in this file, per this
// project's established convention (see `16-dashboard-analytics.test.tsx`).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "@/app/settings/page";
import LoginPage from "@/app/login/page";
import type { UserOut, InviteOut, OrgMemberOut, InvitePreviewOut } from "@/lib/api";

const pushMock = vi.fn();
let searchParamsValue = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => searchParamsValue,
}));

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

const adminUser: UserOut = {
  user_id: "admin-1",
  name: "Admin User",
  email: "admin@acme.com",
  phone_no: "+919876543210",
  org_id: "org-1",
  org_name: "Acme Manufacturing",
  role: "admin",
  phone_verified: true,
  is_active: true,
  admin_name: null,
  admin_email: null,
};

const memberUser: UserOut = {
  ...adminUser,
  user_id: "member-1",
  email: "member@acme.com",
  role: "member",
  admin_name: adminUser.name,
  admin_email: adminUser.email,
};

const orgLessUser: UserOut = {
  ...adminUser,
  user_id: "orgless-1",
  email: "solo@example.com",
  org_id: null,
  org_name: null,
  role: null,
};

const sampleMembers: OrgMemberOut[] = [
  {
    user_id: "admin-1",
    name: "Admin User",
    email: "admin@acme.com",
    role: "admin",
    phone_no: "+919876543210",
    phone_verified: true,
    is_active: true,
    created_at: "2026-06-01T00:00:00Z",
  },
  {
    user_id: "teammate-1",
    name: "Teammate One",
    email: "teammate@acme.com",
    role: "member",
    phone_no: null,
    phone_verified: true,
    is_active: true,
    created_at: "2026-06-02T00:00:00Z",
  },
];

const sampleInvites: InviteOut[] = [
  {
    invite_id: "invite-1",
    email: "pending@acme.com",
    role: "member",
    status: "pending",
    created_at: "2026-06-03T00:00:00Z",
    expires_at: "2026-06-10T00:00:00Z",
    accepted_at: null,
  },
];

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  searchParamsValue = new URLSearchParams();
  pushMock.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ==========================================================================
// 1. /settings -- admin view
// ==========================================================================

function createSettingsApiMock(opts: {
  me: UserOut;
  members?: OrgMemberOut[];
  invites?: InviteOut[];
}) {
  const calls: { method: string; url: string }[] = [];
  // Deep-copy each row, not just the array — several tests mutate a row
  // in place (e.g. deactivate flips is_active), and the fixtures above are
  // shared module-level consts reused across every test in this file.
  const members = (opts.members ?? []).map((m) => ({ ...m }));
  const invites = (opts.invites ?? []).map((i) => ({ ...i }));

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, url });

    if (method === "GET" && url === "/api/auth/me") return jsonResponse(200, opts.me);
    if (method === "GET" && url === "/api/orgs/members") return jsonResponse(200, members);
    if (method === "GET" && url === "/api/orgs/invites") return jsonResponse(200, invites);

    if (method === "POST" && url === "/api/orgs/invites") {
      const body = JSON.parse(String(init?.body));
      invites.push({
        invite_id: "new-invite",
        email: body.email,
        role: "member",
        status: "pending",
        created_at: "2026-06-04T00:00:00Z",
        expires_at: "2026-06-11T00:00:00Z",
        accepted_at: null,
      });
      return jsonResponse(201, invites[invites.length - 1]);
    }
    if (method === "DELETE" && /^\/api\/orgs\/invites\/.+$/.test(url)) {
      return jsonResponse(204, null);
    }
    if (method === "PATCH" && /\/deactivate$/.test(url)) {
      const target = members.find((m) => url.includes(m.user_id));
      if (target) target.is_active = false;
      return jsonResponse(200, target ?? {});
    }
    if (method === "PATCH" && /\/reactivate$/.test(url)) {
      const target = members.find((m) => url.includes(m.user_id));
      if (target) target.is_active = true;
      return jsonResponse(200, target ?? {});
    }
    if (method === "POST" && /\/make-admin$/.test(url)) {
      return jsonResponse(204, null);
    }
    throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
  });

  return { fetchMock, calls };
}

describe("/settings -- admin view", () => {
  it("renders the invite form, pending invites, and members table", async () => {
    const { fetchMock } = createSettingsApiMock({
      me: adminUser,
      members: sampleMembers,
      invites: sampleInvites,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPage />);

    await screen.findByText("Invite a Teammate");
    expect(screen.getByPlaceholderText("teammate@company.com")).toBeInTheDocument();

    expect(await screen.findByText("pending@acme.com")).toBeInTheDocument();
    expect(screen.getByText("Teammate One")).toBeInTheDocument();
    expect(screen.getByText("admin@acme.com")).toBeInTheDocument();
  });

  it("sends an invite and shows it in the pending list", async () => {
    const { fetchMock, calls } = createSettingsApiMock({ me: adminUser, members: sampleMembers });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<SettingsPage />);
    await screen.findByText("Invite a Teammate");

    await user.type(screen.getByPlaceholderText("teammate@company.com"), "newhire@acme.com");
    await user.click(screen.getByRole("button", { name: /send invite/i }));

    await screen.findByText("newhire@acme.com");
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/orgs/invites")).toBe(true);
  });

  it("revokes a pending invite", async () => {
    const { fetchMock, calls } = createSettingsApiMock({
      me: adminUser,
      members: sampleMembers,
      invites: sampleInvites,
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<SettingsPage />);
    await screen.findByText("pending@acme.com");

    await user.click(screen.getByRole("button", { name: /revoke/i }));

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "DELETE" && c.url === "/api/orgs/invites/invite-1")
      ).toBe(true);
    });
  });

  it("deactivates a member after confirmation, and reactivate becomes available", async () => {
    const { fetchMock, calls } = createSettingsApiMock({ me: adminUser, members: sampleMembers });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<SettingsPage />);
    await screen.findByText("Teammate One");

    const teammateRow = screen.getByText("Teammate One").closest("div.grid") as HTMLElement;
    await user.click(within(teammateRow).getByRole("button", { name: /deactivate/i }));

    // Confirmation dialog must appear before any request fires.
    const dialogTitle = await screen.findByText(/Deactivate Member/i);
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);

    // Scoped to the dialog: the row's own "Deactivate" button is still in
    // the DOM behind the overlay and shares the same accessible name.
    const dialog = dialogTitle.closest("div.relative") as HTMLElement;
    await user.click(within(dialog).getByRole("button", { name: "Deactivate" }));

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "PATCH" && c.url === "/api/orgs/members/teammate-1/deactivate"
        )
      ).toBe(true);
    });
    await screen.findByText("Reactivate");
  });

  it("transfers admin ownership via Make Admin", async () => {
    const { fetchMock, calls } = createSettingsApiMock({ me: adminUser, members: sampleMembers });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<SettingsPage />);
    await screen.findByText("Teammate One");

    const teammateRow = screen.getByText("Teammate One").closest("div.grid") as HTMLElement;
    await user.click(within(teammateRow).getByRole("button", { name: /make admin/i }));

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "POST" && c.url === "/api/orgs/members/teammate-1/make-admin"
        )
      ).toBe(true);
    });
  });

  it("never shows management controls next to the admin's own row", async () => {
    const { fetchMock } = createSettingsApiMock({ me: adminUser, members: sampleMembers });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPage />);
    await screen.findByText("Teammate One");

    const adminRow = screen.getByText("admin@acme.com").closest("div.grid") as HTMLElement;
    expect(within(adminRow).queryByRole("button")).not.toBeInTheDocument();
  });
});

// ==========================================================================
// 2. /settings -- non-admin views
// ==========================================================================

describe("/settings -- member and org-less views", () => {
  it("shows a read-only panel for a non-admin member, with no management controls", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      if (url === "/api/auth/me") return jsonResponse(200, memberUser);
      throw new Error(`Unhandled fetch call in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPage />);

    await screen.findByText(/You are a Member/i);
    expect(screen.getByText(adminUser.name as string)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(adminUser.email))).toBeInTheDocument();
    expect(screen.queryByText("Invite a Teammate")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deactivate/i })).not.toBeInTheDocument();
    // A non-admin must never even reach the admin-only endpoints.
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes("/api/orgs/"))
    ).toBe(false);
  });

  it("shows an org-less placeholder for a user with no pending invites", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      if (url === "/api/auth/me") return jsonResponse(200, orgLessUser);
      if (url === "/api/orgs/my-invites") return jsonResponse(200, []);
      throw new Error(`Unhandled fetch call in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPage />);

    await screen.findByText(/not part of an organization yet/i);
  });

  it("lets an org-less user with a pending invite accept it from their own account", async () => {
    const pendingInvite = {
      invite_id: "invite-9",
      org_name: "Acme Manufacturing",
      token: "some-token",
      expires_at: "2026-08-01T00:00:00Z",
    };
    const calls: string[] = [];
    let accepted = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push(`${method} ${url}`);
      if (url === "/api/auth/me") return jsonResponse(200, orgLessUser);
      if (url === "/api/orgs/my-invites") return jsonResponse(200, accepted ? [] : [pendingInvite]);
      if (url === "/api/orgs/invites/some-token/accept" && method === "POST") {
        accepted = true;
        return jsonResponse(200, { ...orgLessUser, org_id: "org-1", org_name: "Acme Manufacturing", role: "member" });
      }
      throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<SettingsPage />);

    await screen.findByText("Acme Manufacturing");
    await user.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() => {
      expect(calls).toContain("POST /api/orgs/invites/some-token/accept");
    });
  });
});

// ==========================================================================
// 3. Login page -- invite banner + accept flow
// ==========================================================================

const invitePreview: InvitePreviewOut = {
  org_name: "Acme Manufacturing",
  invitee_email: "invitee@example.com",
  status: "pending",
};

describe("Login page -- invite flow", () => {
  it("shows the invite banner and hides the Company Name field in signup mode", async () => {
    searchParamsValue = new URLSearchParams("invite=tok123");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      if (url === "/api/orgs/invites/tok123") return jsonResponse(200, invitePreview);
      throw new Error(`Unhandled fetch call in test: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LoginPage />);

    await screen.findByText(/You're invited to join/i);
    expect(screen.getByText("Acme Manufacturing")).toBeInTheDocument();
    expect(screen.queryByText("Company Name")).not.toBeInTheDocument();
  });

  it("accepts the invite after a successful login, before redirecting to /dashboard", async () => {
    searchParamsValue = new URLSearchParams("invite=tok123");
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push(`${method} ${url}`);

      if (url === "/api/orgs/invites/tok123" && method === "GET") {
        return jsonResponse(200, invitePreview);
      }
      if (url === "/api/auth/login" && method === "POST") {
        return jsonResponse(200, memberUser);
      }
      if (url === "/api/orgs/invites/tok123/accept" && method === "POST") {
        return jsonResponse(200, memberUser);
      }
      throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<LoginPage />);
    await screen.findByText(/You're invited to join/i);

    // Invite flow defaults to signup mode; switch to login for this test.
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    // The email field is already pre-filled with the invite's email by the
    // preview effect — typing more into it would append rather than
    // replace, producing an invalid double-email string that silently
    // fails the input's built-in HTML5 email validation and blocks
    // submission. Only the password needs typing.
    await user.type(screen.getByPlaceholderText("••••••••"), "Str0ngPass!");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
    // Accept must fire before (or at least alongside) the redirect, not be skipped.
    expect(calls).toContain("POST /api/orgs/invites/tok123/accept");
    const loginIndex = calls.indexOf("POST /api/auth/login");
    const acceptIndex = calls.indexOf("POST /api/orgs/invites/tok123/accept");
    expect(acceptIndex).toBeGreaterThan(loginIndex);
  });

  it("still redirects to /dashboard even if the invite token is stale/already used", async () => {
    searchParamsValue = new URLSearchParams("invite=stale-token");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      const method = (init?.method ?? "GET").toUpperCase();

      if (url === "/api/orgs/invites/stale-token" && method === "GET") {
        return jsonResponse(404, { detail: "Invite not found" });
      }
      if (url === "/api/auth/login" && method === "POST") {
        return jsonResponse(200, memberUser);
      }
      if (url === "/api/orgs/invites/stale-token/accept" && method === "POST") {
        return jsonResponse(404, { detail: "Invite not found" });
      }
      throw new Error(`Unhandled fetch call in test: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    await user.type(screen.getByPlaceholderText("you@company.com"), "someone@example.com");
    await user.type(screen.getByPlaceholderText("••••••••"), "Str0ngPass!");
    await user.click(screen.getByRole("button", { name: "Sign In" }));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
  });
});
