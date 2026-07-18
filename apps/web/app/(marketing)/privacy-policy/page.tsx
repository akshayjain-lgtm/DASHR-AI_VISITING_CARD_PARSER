import { ShieldCheck } from "lucide-react";
import { Navbar } from "@/components/navbar";

const SECTIONS = [
  {
    heading: "What we collect",
    body: "When you use DASHR AI, we collect the visiting card images you upload and the contact details our AI extracts from them (name, title, company, email, phone, and address), along with your own account details (name, work email, phone number, and company profile). We also collect firmographic data about the companies your leads work for, sourced from public business data.",
  },
  {
    heading: "How we use it",
    body: "Extracted contact data is used to build your scored lead list — enriched with public company data and ranked against your product-fit profile. Your account and billing details are used to operate your prepaid wallet, generate invoices, and secure your organization's data. We do not sell your data or your leads' data to third parties.",
  },
  {
    heading: "Data isolation",
    body: "DASHR AI is multi-tenant: every card, contact, lead, and exhibition is scoped to your organization and is never visible to another organization. Within your organization, an Admin can see all team members' leads and cards; wallet balances and invoices remain private to the individual user, with Admin visibility into invoices only, never spending authority.",
  },
  {
    heading: "Storage & retention",
    body: "Card images are stored in encrypted object storage, never directly in our database. Extracted and enriched data is retained for as long as your account is active, so your lead history remains available across exhibitions. You can request deletion of your account and associated data by contacting us.",
  },
  {
    heading: "Payments",
    body: "Wallet recharges are processed by Razorpay. We never store your card, UPI, or netbanking credentials — Razorpay handles payment collection, and we only receive a signature-verified confirmation of a successful recharge before crediting your wallet.",
  },
  {
    heading: "Your choices",
    body: "You can review and update your Company Profile at any time from Settings. To request a copy of your data, or its deletion, reach out via the Contact Us page.",
  },
  {
    heading: "Contact",
    body: "Questions about this policy can be sent to info@dashrtech.com.",
  },
];

export default function PrivacyPolicyPage() {
  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="max-w-3xl mx-auto px-6 pt-16 pb-10">
        <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-6">
          <ShieldCheck size={11} />
          Privacy Policy
        </div>
        <h1 className="text-3xl font-black tracking-tight mb-3">Privacy Policy</h1>
        <p className="text-black/45 text-sm">Last updated July 2026</p>
      </section>

      <div className="max-w-3xl mx-auto px-6 pb-20 space-y-10">
        {SECTIONS.map(({ heading, body }) => (
          <div key={heading}>
            <h2 className="text-lg font-black mb-2">{heading}</h2>
            <p className="text-sm text-black/60 leading-relaxed">{body}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
