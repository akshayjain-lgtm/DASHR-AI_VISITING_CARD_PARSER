"use client";

import { useState } from "react";
import { Zap, CheckCircle } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";

type ProfileForm = {
  companyName: string;
  industry: string;
  businessType: string;
  productLines: string;
  targetBuyer: string;
  avgDealSize: string;
  salesRegion: string;
};

const FIELDS: {
  label: string;
  key: keyof ProfileForm;
  placeholder: string;
  multi?: boolean;
}[] = [
  { label: "Company Name", key: "companyName", placeholder: "Your company name" },
  { label: "Industry / Sector", key: "industry", placeholder: "e.g. Industrial Pumps & Valves" },
  { label: "Business Type", key: "businessType", placeholder: "Manufacturer / Distributor / EPC Contractor" },
  { label: "Product Lines", key: "productLines", placeholder: "List your key products or solutions…", multi: true },
  { label: "Target Buyer Description", key: "targetBuyer", placeholder: "Describe your ideal buyer role and industry…", multi: true },
  { label: "Average Deal Size", key: "avgDealSize", placeholder: "₹X – ₹Y" },
  { label: "Sales Region", key: "salesRegion", placeholder: "States or countries you sell into" },
];

export default function ProfilePage() {
  const [saved, setSaved] = useState(false);
  const [form, setForm] = useState<ProfileForm>({
    companyName: "Thermax Limited",
    industry: "Process Equipment & Heat Exchangers",
    businessType: "Manufacturer",
    productLines:
      "Industrial boilers, heat recovery systems, absorption chillers, water treatment systems",
    targetBuyer:
      "Plant engineers, procurement heads, facility managers in chemical, pharma, and food processing",
    avgDealSize: "₹50L – ₹2Cr",
    salesRegion: "Pan India, Middle East",
  });

  return (
    <div className="min-h-screen bg-white flex">
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
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white resize-none"
                />
              ) : (
                <input
                  type="text"
                  value={form[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  placeholder={placeholder}
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white"
                />
              )}
            </div>
          ))}
        </div>

        <div className="flex items-center gap-4 mt-8 pt-8 border-t border-black/8">
          <OBtn
            onClick={() => {
              setSaved(true);
              setTimeout(() => setSaved(false), 2500);
            }}
            className="gap-2"
          >
            {saved ? (
              <>
                <CheckCircle size={14} /> Saved!
              </>
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
