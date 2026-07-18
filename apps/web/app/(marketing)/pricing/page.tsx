"use client";

import { useRouter } from "next/navigation";
import { Gift, ScanLine, Building2, Award, Wallet } from "lucide-react";
import { Navbar } from "@/components/navbar";
import { CtaBanner } from "@/components/cta-banner";

const RATES = [
  {
    icon: ScanLine,
    action: "Card Parsing",
    unit: "per card",
    price: 5,
    desc: "AI vision extraction of name, title, company, email, phone, and address from one scanned card.",
  },
  {
    icon: Building2,
    action: "Company Enrichment",
    unit: "per lookup",
    price: 3,
    desc: "Firmographics attached to an extracted company — industry, employee count, revenue band, and location.",
  },
  {
    icon: Award,
    action: "Lead Scoring",
    unit: "per lead",
    price: 2,
    desc: "Product-fit score against your Company Profile's target buyer and industry.",
  },
];

export default function PricingPage() {
  const router = useRouter();

  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="max-w-4xl mx-auto px-6 pt-16 pb-10">
        <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-6">
          <Wallet size={11} />
          Pricing
        </div>
        <h1 className="text-3xl font-black tracking-tight mb-3">
          Free to start, pay only for what you use
        </h1>
        <p className="text-black/50 text-lg max-w-lg">
          Every user gets a free allowance on every action. After that, each function is
          billed individually from your own prepaid wallet — no plans, no seats.
        </p>
      </section>

      {/* Free tier */}
      <section className="max-w-4xl mx-auto px-6 pb-16">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-8 h-px bg-[#E65527]" />
          <span className="text-[11px] font-black uppercase tracking-[0.12em] text-[#E65527]">
            Free Access
          </span>
        </div>
        <div className="border border-[#E65527]/25 bg-[#E65527]/4 p-6 flex items-start gap-4 mb-6">
          <div className="w-10 h-10 bg-[#E65527] flex items-center justify-center shrink-0">
            <Gift size={17} className="text-white" />
          </div>
          <div>
            <p className="font-bold mb-1">Every user starts with 20 free actions of each type</p>
            <p className="text-sm text-black/60 leading-relaxed">
              20 free card parses, 20 free enrichments, and 20 free scorings — tracked as three
              independent counters. Using up your free parses never touches your free
              enrichments or scorings.
            </p>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-px bg-black/8">
          {RATES.map(({ icon: Icon, action }) => (
            <div key={action} className="bg-white p-6 text-center">
              <div className="w-9 h-9 bg-[#E65527]/8 flex items-center justify-center mx-auto mb-3">
                <Icon size={16} className="text-[#E65527]" />
              </div>
              <div className="text-2xl font-black mb-1">20 free</div>
              <div className="text-xs text-black/45 font-semibold">{action}</div>
            </div>
          ))}
        </div>
      </section>

      {/* Per-function pricing */}
      <section className="bg-[#fafafa] py-16 border-y border-black/8">
        <div className="max-w-4xl mx-auto px-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-8 h-px bg-[#E65527]" />
            <span className="text-[11px] font-black uppercase tracking-[0.12em] text-[#E65527]">
              After Your Free Allowance
            </span>
          </div>
          <h2 className="text-2xl font-black tracking-tight mb-8 max-w-md">
            Pay per action, from your own prepaid wallet
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-px bg-black/8">
            {RATES.map(({ icon: Icon, action, unit, price, desc }) => (
              <div key={action} className="bg-white p-8">
                <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center mb-5">
                  <Icon size={17} className="text-[#E65527]" />
                </div>
                <h3 className="font-bold mb-1">{action}</h3>
                <div className="flex items-baseline gap-1.5 mb-3">
                  <span className="text-3xl font-black text-[#E65527]">₹{price}</span>
                  <span className="text-xs text-black/40 font-semibold">{unit}</span>
                </div>
                <p className="text-sm text-black/50 leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>

          <div className="mt-10 border border-black/10 bg-white p-6">
            <p className="text-sm text-black/60 leading-relaxed">
              <strong className="text-black">Every wallet is your own.</strong> Recharges
              (Netbanking, UPI, Debit/Credit Card) top up an individual prepaid balance — never a
              shared org balance, and no admin has spending authority over a teammate&apos;s
              wallet. If your free allowance for an action type is used up and your balance hits
              zero, that action is blocked until you recharge — nothing runs, and nothing is
              billed, without balance to cover it.
            </p>
          </div>
        </div>
      </section>

      <CtaBanner
        heading="Try it before you recharge"
        subcopy="Your free allowance covers 20 parses, 20 enrichments, and 20 scorings — no card required."
        ctaLabel="Try Demo"
        onCtaClick={() => router.push("/product")}
      />
    </div>
  );
}
