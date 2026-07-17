"use client";

import { useEffect, useState } from "react";
import { Zap, CheckCircle } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { ApiError, getProfile, updateProfile, type SellerProfileOut } from "@/lib/api";

type ProfileForm = {
  companyName: string;
  industry: string;
  productLines: string;
  targetBuyer: string;
  salesRegion: string;
  gstNo: string;
  billingAddress: string;
};

const EMPTY_FORM: ProfileForm = {
  companyName: "",
  industry: "",
  productLines: "",
  targetBuyer: "",
  salesRegion: "",
  gstNo: "",
  billingAddress: "",
};

type ProfileApiField =
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

export default function ProfilePage() {
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
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <Sidebar active="profile" />
      <main className="flex-1 p-10 max-w-2xl">
        <div className="mb-8">
          <h1 className="text-2xl font-black mb-1">Company Profile</h1>
          <p className="text-sm text-black/45">
            Calibrates lead scoring to your product lines and buyer profile.
          </p>
        </div>

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
      </main>
    </div>
  );
}
