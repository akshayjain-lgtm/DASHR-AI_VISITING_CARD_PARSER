import { FileText } from "lucide-react";
import { Navbar } from "@/components/navbar";

const SECTIONS = [
  {
    heading: "Using DASHR AI",
    body: "DASHR AI is provided to help you extract, enrich, and score contacts from visiting cards collected at trade exhibitions. You're responsible for the accuracy of the cards you upload and for having the right to process the contact data on them.",
  },
  {
    heading: "Accounts",
    body: "Each user (Admin or team member) belongs to one Organization. Admins can invite and manage team membership within their Organization; this role governs data visibility only, not billing — no user, including an Admin, has spending authority over another user's wallet.",
  },
  {
    heading: "Wallet & billing",
    body: "Parsing, enrichment, and scoring are billed per action from your own prepaid wallet, after your first 20 free actions of each type. Recharges are made via Razorpay and are spend-only — recharged balance cannot be withdrawn or refunded from the website; contact customer care for refund requests. An invoice titled \"Visiting Card Recharge and Scoring\" is issued for each recharge, not for each parse, enrichment, or scoring action.",
  },
  {
    heading: "Acceptable use",
    body: "You agree not to upload cards you don't have the right to process, attempt to access another organization's data, or use DASHR AI to build a competing product. We may suspend accounts that violate these terms.",
  },
  {
    heading: "Availability",
    body: "We aim for high availability but don't guarantee uninterrupted access. Bulk processing (parsing, enrichment, scoring) runs asynchronously and may take time to complete for large batches.",
  },
  {
    heading: "Changes",
    body: "We may update these terms as the product evolves. Continued use of DASHR AI after an update means you accept the revised terms.",
  },
  {
    heading: "Contact",
    body: "Questions about these terms can be sent to info@dashrtech.com.",
  },
];

export default function TermsOfUsePage() {
  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="max-w-3xl mx-auto px-6 pt-16 pb-10">
        <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-6">
          <FileText size={11} />
          Terms of Use
        </div>
        <h1 className="text-3xl font-black tracking-tight mb-3">Terms of Use</h1>
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
