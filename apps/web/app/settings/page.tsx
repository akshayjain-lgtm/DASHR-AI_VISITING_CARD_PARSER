"use client";

import { useEffect, useState } from "react";
import { Users, Mail, ShieldCheck, UserX, UserCheck, Zap, CheckCircle, Building2, LogOut } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn, GBtn } from "@/components/buttons";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  ApiError,
  me,
  createInvite,
  listInvites,
  revokeInvite,
  listOrgMembers,
  deactivateMember,
  reactivateMember,
  makeAdmin,
  listMyInvites,
  acceptInvite,
  getProfile,
  updateProfile,
  logout,
  type UserOut,
  type InviteOut,
  type OrgMemberOut,
  type MyInviteOut,
  type SellerProfileOut,
} from "@/lib/api";

const TABS = [
  { id: "profile", label: "Company Profile", icon: Building2 },
  { id: "roles", label: "Roles and Access", icon: ShieldCheck },
] as const;

type TabId = (typeof TABS)[number]["id"];

type ProfileForm = {
  userName: string;
  designation: string;
  companyName: string;
  industry: string;
  productLines: string;
  targetBuyer: string;
  salesRegion: string;
  gstNo: string;
  billingAddress: string;
};

const EMPTY_FORM: ProfileForm = {
  userName: "",
  designation: "",
  companyName: "",
  industry: "",
  productLines: "",
  targetBuyer: "",
  salesRegion: "",
  gstNo: "",
  billingAddress: "",
};

type ProfileApiField =
  | "name"
  | "designation"
  | "company_name"
  | "industry"
  | "product_lines"
  | "target_customer_description"
  | "target_regions"
  | "gst_no"
  | "billing_address";

const FIELDS: {
  label: string;
  key: keyof ProfileForm;
  apiField: ProfileApiField;
  placeholder: string;
  multi?: boolean;
}[] = [
  { label: "User Name", key: "userName", apiField: "name", placeholder: "Your full name" },
  { label: "Role / Designation", key: "designation", apiField: "designation", placeholder: "e.g. Sales Manager" },
  { label: "Company Name", key: "companyName", apiField: "company_name", placeholder: "Your company name" },
  { label: "Industry / Sector", key: "industry", apiField: "industry", placeholder: "e.g. Industrial Pumps & Valves" },
  { label: "Product Lines", key: "productLines", apiField: "product_lines", placeholder: "List your key products or solutions…", multi: true },
  { label: "Target Buyer Description", key: "targetBuyer", apiField: "target_customer_description", placeholder: "Describe your ideal buyer role and industry…", multi: true },
  { label: "Sales Region", key: "salesRegion", apiField: "target_regions", placeholder: "States or countries you sell into" },
  { label: "GST No.", key: "gstNo", apiField: "gst_no", placeholder: "GSTIN (optional)" },
  { label: "Billing Address", key: "billingAddress", apiField: "billing_address", placeholder: "Billing address for invoices (optional)", multi: true },
];

// Both directions are derived from FIELDS so the ProfileForm <-> API field
// mapping is defined in exactly one place.
function toForm(profile: SellerProfileOut): ProfileForm {
  const form = { ...EMPTY_FORM };
  for (const { key, apiField } of FIELDS) {
    form[key] = profile[apiField] ?? "";
  }
  return form;
}

function toUpdatePayload(form: ProfileForm): Record<ProfileApiField, string> {
  const payload = {} as Record<ProfileApiField, string>;
  for (const { key, apiField } of FIELDS) {
    payload[apiField] = form[key];
  }
  return payload;
}

function CompanyProfileTab() {
  const [form, setForm] = useState<ProfileForm>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProfile()
      .then((profile) => {
        if (!cancelled) setForm(toForm(profile));
      })
      .catch(() => {
        if (!cancelled) {
          setError("Couldn't load your saved profile. Try refreshing the page.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSave = async () => {
    if (form.userName.trim() === "") {
      setError("User Name can't be blank.");
      return;
    }
    if (form.designation.trim() === "") {
      setError("Role / Designation can't be blank.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await updateProfile(toUpdatePayload(form));
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save your profile. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-2xl">
      <div className="border border-[#E65527]/20 bg-[#E65527]/4 px-5 py-4 mb-8 flex items-start gap-3">
        <Zap size={15} className="text-[#E65527] shrink-0 mt-0.5" />
        <p className="text-sm text-black/60 leading-relaxed">
          <strong className="text-black">Why does this matter?</strong> DASHR AI
          scores each lead by comparing their company profile against your ideal
          buyer definition. The more specific your profile, the more accurate the
          scores.
        </p>
      </div>

      <div className="space-y-5">
        {FIELDS.map(({ label, key, placeholder, multi }) => (
          <div key={key}>
            <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
              {label}
            </label>
            {multi ? (
              <textarea
                rows={3}
                value={form[key]}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                placeholder={placeholder}
                disabled={loading}
                className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white resize-none disabled:opacity-60"
              />
            ) : (
              <input
                type="text"
                value={form[key]}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                placeholder={placeholder}
                disabled={loading}
                className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
              />
            )}
          </div>
        ))}
      </div>

      {error && <p className="text-sm text-red-600 mt-4">{error}</p>}

      <div className="flex items-center gap-4 mt-8 pt-8 border-t border-black/8">
        <OBtn onClick={handleSave} disabled={loading || saving} className="gap-2">
          {saved ? (
            <>
              <CheckCircle size={14} /> Saved!
            </>
          ) : saving ? (
            "Saving…"
          ) : (
            "Save Profile"
          )}
        </OBtn>
        <button className="text-sm text-black/35 hover:text-black transition-colors">
          Cancel
        </button>
      </div>
    </div>
  );
}

function RoleBadge({ role }: { role: string | null }) {
  const isAdmin = role === "admin";
  return (
    <span
      className={`inline-flex px-2.5 py-0.5 text-[11px] font-black tracking-wide ${
        isAdmin ? "bg-[#E65527]/10 text-[#E65527]" : "bg-black/6 text-black/50"
      }`}
    >
      {isAdmin ? "Admin" : "Member"}
    </span>
  );
}

function StatusBadge({ active }: { active: boolean }) {
  return (
    <span
      className={`inline-flex px-2.5 py-0.5 text-[11px] font-black tracking-wide ${
        active ? "bg-green-600/10 text-green-700" : "bg-black/6 text-black/40"
      }`}
    >
      {active ? "Active" : "Deactivated"}
    </span>
  );
}

function RolesAccessTab() {
  const [currentUser, setCurrentUser] = useState<UserOut | null>(null);
  const [members, setMembers] = useState<OrgMemberOut[]>([]);
  const [invites, setInvites] = useState<InviteOut[]>([]);
  const [myInvites, setMyInvites] = useState<MyInviteOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [email, setEmail] = useState("");
  const [inviting, setInviting] = useState(false);
  const [actingOn, setActingOn] = useState<string | null>(null);
  const [pendingDeactivate, setPendingDeactivate] = useState<OrgMemberOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isAdmin = currentUser?.role === "admin";

  async function refreshTeam() {
    const [membersData, invitesData] = await Promise.all([listOrgMembers(), listInvites()]);
    setMembers(membersData);
    setInvites(invitesData);
  }

  useEffect(() => {
    let cancelled = false;
    me()
      .then(async (user) => {
        if (cancelled) return;
        setCurrentUser(user);
        if (user.role === "admin") {
          await refreshTeam();
        } else if (!user.org_id) {
          // Only relevant for an org-less user — anyone already in an org
          // can't accept a second invite anyway (accept_invite 409s).
          setMyInvites(await listMyInvites());
        }
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't load your account.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    setInviting(true);
    setError(null);
    try {
      await createInvite(email);
      setEmail("");
      await refreshTeam();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send the invite.");
    } finally {
      setInviting(false);
    }
  }

  async function handleRevoke(inviteId: string) {
    setActingOn(inviteId);
    setError(null);
    try {
      await revokeInvite(inviteId);
      await refreshTeam();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't revoke the invite.");
    } finally {
      setActingOn(null);
    }
  }

  async function handleReactivate(userId: string) {
    setActingOn(userId);
    setError(null);
    try {
      await reactivateMember(userId);
      await refreshTeam();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't reactivate this member.");
    } finally {
      setActingOn(null);
    }
  }

  async function handleConfirmDeactivate() {
    if (!pendingDeactivate) return;
    const userId = pendingDeactivate.user_id;
    setActingOn(userId);
    setError(null);
    try {
      await deactivateMember(userId);
      await refreshTeam();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't deactivate this member.");
    } finally {
      setActingOn(null);
      setPendingDeactivate(null);
    }
  }

  async function handleAcceptMyInvite(token: string) {
    setActingOn(token);
    setError(null);
    try {
      const updatedMe = await acceptInvite(token);
      setCurrentUser(updatedMe);
      setMyInvites([]);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't accept this invite.");
    } finally {
      setActingOn(null);
    }
  }

  async function handleMakeAdmin(userId: string) {
    setActingOn(userId);
    setError(null);
    try {
      await makeAdmin(userId);
      const [updatedMe] = await Promise.all([me(), refreshTeam()]);
      setCurrentUser(updatedMe);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't transfer admin ownership.");
    } finally {
      setActingOn(null);
    }
  }

  return (
    <>
      {loading ? (
        <p className="text-sm text-black/40">Loading…</p>
      ) : !currentUser ? (
        // A failed /auth/me (network error, or a session that died mid-visit —
        // e.g. this user was just deactivated) must never be mistaken for
        // "no organization": that's a different, misleading message.
        <p className="text-sm text-red-600">{error ?? "Couldn't load your account."}</p>
      ) : !currentUser?.org_id ? (
        <>
          {error && <p className="text-sm text-red-600 mb-6">{error}</p>}
          {myInvites.length > 0 ? (
            <div className="border border-black/10 overflow-hidden mb-6">
              <div className="grid grid-cols-[2fr_1fr_auto] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center">
                <div>Organization</div>
                <div>Expires</div>
                <div />
              </div>
              {myInvites.map((invite) => (
                <div
                  key={invite.invite_id}
                  className="grid grid-cols-[2fr_1fr_auto] gap-4 px-5 py-4 border-b border-black/5 text-sm items-center"
                >
                  <div className="font-bold">{invite.org_name}</div>
                  <div className="text-black/40 text-xs">
                    {new Date(invite.expires_at).toLocaleDateString("en-IN")}
                  </div>
                  <div className="flex justify-end">
                    <OBtn
                      onClick={() => handleAcceptMyInvite(invite.token)}
                      disabled={actingOn === invite.token}
                      className="text-xs px-3 py-1.5"
                    >
                      {actingOn === invite.token ? "Accepting…" : "Accept"}
                    </OBtn>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="border border-black/10 px-6 py-10 text-center">
              <Users size={22} className="mx-auto mb-3 text-black/25" />
              <p className="text-sm text-black/50">
                You&apos;re not part of an organization yet — sign up with a Company Name to create one.
              </p>
            </div>
          )}
        </>
      ) : !isAdmin ? (
        <div className="border border-black/10 px-6 py-10 text-center">
          <ShieldCheck size={22} className="mx-auto mb-3 text-black/25" />
          <p className="text-sm text-black/60 font-bold mb-1">
            {currentUser.org_name ?? "Your organization"}
          </p>
          <p className="text-sm text-black/45 mb-4">
            You are a Member of this organization. Only the admin can manage team membership.
          </p>
          {currentUser.admin_name && (
            <p className="text-[11px] text-black/40">
              Admin: <span className="font-bold text-black/60">{currentUser.admin_name}</span>
              {currentUser.admin_email && ` (${currentUser.admin_email})`}
            </p>
          )}
        </div>
      ) : (
        <>
          {error && <p className="text-sm text-red-600 mb-6">{error}</p>}

          <h2 className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-3">
            Invite a Teammate
          </h2>
          <form onSubmit={handleInvite} className="flex items-end gap-3 mb-10">
            <div className="flex-1 max-w-xs">
              <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                Email
              </label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@company.com"
                disabled={inviting}
                className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
              />
            </div>
            <OBtn type="submit" disabled={inviting} className="gap-2">
              <Mail size={14} />
              {inviting ? "Sending…" : "Send Invite"}
            </OBtn>
          </form>

          {invites.length > 0 && (
            <>
              <h2 className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-3">
                Pending Invites
              </h2>
              <div className="border border-black/10 overflow-hidden mb-10">
                <div className="grid grid-cols-[2fr_1fr_1fr_auto] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center">
                  <div>Email</div>
                  <div>Status</div>
                  <div>Expires</div>
                  <div />
                </div>
                {invites.map((invite) => (
                  <div
                    key={invite.invite_id}
                    className="grid grid-cols-[2fr_1fr_1fr_auto] gap-4 px-5 py-4 border-b border-black/5 text-sm items-center"
                  >
                    <div>{invite.email}</div>
                    <div className="capitalize text-black/60">{invite.status}</div>
                    <div className="text-black/40 text-xs">
                      {new Date(invite.expires_at).toLocaleDateString("en-IN")}
                    </div>
                    <div>
                      {invite.status === "pending" && (
                        <GBtn
                          onClick={() => handleRevoke(invite.invite_id)}
                          disabled={actingOn === invite.invite_id}
                          className="text-xs px-3 py-1.5"
                        >
                          {actingOn === invite.invite_id ? "Revoking…" : "Revoke"}
                        </GBtn>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          <h2 className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-3">
            Team Members
          </h2>
          <div className="border border-black/10 overflow-hidden">
            <div className="grid grid-cols-[2fr_2fr_1fr_1fr_auto] gap-4 bg-[#fafafa] border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/35 items-center">
              <div>Name</div>
              <div>Email</div>
              <div>Role</div>
              <div>Status</div>
              <div />
            </div>
            {members.map((member) => {
              const isSelf = member.user_id === currentUser.user_id;
              const busy = actingOn === member.user_id;
              return (
                <div
                  key={member.user_id}
                  className="grid grid-cols-[2fr_2fr_1fr_1fr_auto] gap-4 px-5 py-4 border-b border-black/5 text-sm items-center"
                >
                  <div>{member.name ?? "—"}</div>
                  <div className="text-black/60">{member.email}</div>
                  <div>
                    <RoleBadge role={member.role} />
                  </div>
                  <div>
                    <StatusBadge active={member.is_active} />
                  </div>
                  <div className="flex justify-end gap-2">
                    {!isSelf && member.role !== "admin" && (
                      <>
                        {member.is_active ? (
                          <GBtn
                            onClick={() => setPendingDeactivate(member)}
                            disabled={busy}
                            className="text-xs px-3 py-1.5 gap-1"
                          >
                            <UserX size={12} />
                            Deactivate
                          </GBtn>
                        ) : (
                          <GBtn
                            onClick={() => handleReactivate(member.user_id)}
                            disabled={busy}
                            className="text-xs px-3 py-1.5 gap-1"
                          >
                            <UserCheck size={12} />
                            {busy ? "Reactivating…" : "Reactivate"}
                          </GBtn>
                        )}
                        {member.is_active && (
                          <GBtn
                            onClick={() => handleMakeAdmin(member.user_id)}
                            disabled={busy}
                            className="text-xs px-3 py-1.5 gap-1"
                          >
                            <ShieldCheck size={12} />
                            Make Admin
                          </GBtn>
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      {pendingDeactivate && (
        <ConfirmDialog
          title="Deactivate Member"
          message={`${pendingDeactivate.name ?? pendingDeactivate.email} will immediately lose access to DASHR AI. You can reactivate them later.`}
          confirmLabel="Deactivate"
          isConfirming={actingOn === pendingDeactivate.user_id}
          onConfirm={handleConfirmDeactivate}
          onCancel={() => setPendingDeactivate(null)}
        />
      )}
    </>
  );
}

export default function SettingsPage() {
  const [tab, setTab] = useState<TabId>("profile");

  async function handleSignOut() {
    try {
      await logout();
    } finally {
      // Hard navigation, not router.push: see Sidebar's handleSignOut for
      // why (stale Router Cache redirect can bounce back into the app).
      window.location.href = "/";
    }
  }

  return (
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <Sidebar active="settings" />
      <main className="flex-1 p-10 max-w-4xl">
        <div className="mb-8">
          <h1 className="text-2xl font-black mb-1">Settings</h1>
          <p className="text-sm text-black/45">
            Manage your company profile, team, and organization membership.
          </p>
        </div>

        <div className="flex items-center justify-between border-b border-black/10 mb-8">
          <div className="flex items-center gap-1">
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`flex items-center gap-2 px-4 py-3 text-sm font-bold border-b-2 -mb-px transition-colors ${
                  tab === id
                    ? "border-[#E65527] text-[#E65527]"
                    : "border-transparent text-black/45 hover:text-black"
                }`}
              >
                <Icon size={14} />
                {label}
              </button>
            ))}
          </div>
          <button
            onClick={handleSignOut}
            className="flex items-center gap-2 px-4 py-3 text-sm font-bold text-black/45 hover:text-black transition-colors"
          >
            <LogOut size={14} />
            Sign Out
          </button>
        </div>

        {tab === "profile" ? <CompanyProfileTab /> : <RolesAccessTab />}
      </main>
    </div>
  );
}
