"use client";

import { useRouter } from "next/navigation";
import {
  Upload,
  Database,
  Award,
  Building2,
  Shield,
  User,
  Layers,
  ScanLine,
  ArrowRight,
} from "lucide-react";
import { Navbar } from "@/components/navbar";
import { DashrLogo } from "@/components/dashr-logo";
import { HeroViz } from "@/components/hero-viz";
import { OBtn, GBtn } from "@/components/buttons";
import { CtaBanner } from "@/components/cta-banner";

const STEPS = [
  {
    n: "01",
    icon: Upload,
    title: "Bulk Upload Cards",
    desc: "Drop a folder of scanned card images. Supports JPG, PNG, PDF — up to 500 cards per batch.",
  },
  {
    n: "02",
    icon: Database,
    title: "Auto-Extract & Save",
    desc: "OCR pulls name, company, designation, phone, and email. Every record instantly saved to your workspace.",
  },
  {
    n: "03",
    icon: Building2,
    title: "Enrich with Public Data",
    desc: "Company size, industry, revenue band, and location appended automatically from public business sources.",
  },
  {
    n: "04",
    icon: Award,
    title: "Get Lead Scores",
    desc: "Each contact scored against your product profile. Know who to call first — before Monday morning.",
  },
];

const FEATURES = [
  {
    icon: Upload,
    title: "Bulk Upload",
    desc: "Process hundreds of cards in one go. Drag-and-drop or folder select from any device.",
  },
  {
    icon: Shield,
    title: "Secure Database",
    desc: "All contact data stored in your private workspace with role-based access control.",
  },
  {
    icon: Building2,
    title: "Company Enrichment",
    desc: "Auto-append industry, size, and revenue data from public business registries.",
  },
  {
    icon: Award,
    title: "Lead Scoring",
    desc: "AI fit scores based on your product lines and ideal buyer profile.",
  },
  {
    icon: User,
    title: "User Profiles",
    desc: "Multi-user access with individual login and per-session activity tracking.",
  },
  {
    icon: Layers,
    title: "Exhibition Tracking",
    desc: "Tag each upload batch to a specific trade show for source attribution.",
  },
];

export default function HomePage() {
  const router = useRouter();

  return (
    <div className="bg-white">
      <Navbar />

      {/* Hero */}
      <section className="max-w-6xl mx-auto px-6 pt-20 pb-20">
        <div className="max-w-3xl">
          <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-8">
            <ScanLine size={11} />
            Built for industrial trade exhibitions
          </div>
          <h1 className="text-[3.25rem] font-black leading-[1.05] tracking-tight text-black mb-6">
            Turn a Stack of
            <br />
            <span className="text-[#E65527]">Business Cards</span> Into
            <br />
            Your Best Lead List
          </h1>
          <p className="text-lg text-black/55 leading-relaxed mb-10 max-w-[34rem]">
            Upload hundreds of cards from your last trade show. We extract
            every detail, enrich with company intelligence, and score each
            lead by fit for your product lines.
          </p>
          <div className="flex items-center gap-4">
            <OBtn onClick={() => router.push("/product")} className="px-7 py-3 text-base">
              Try Demo <ArrowRight size={16} />
            </OBtn>
            <GBtn>See How It Works</GBtn>
          </div>
        </div>
        <HeroViz />
      </section>

      {/* Divider */}
      <div className="h-px bg-black/8 max-w-6xl mx-auto" />

      {/* How It Works */}
      <section className="max-w-6xl mx-auto px-6 py-20">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-8 h-px bg-[#E65527]" />
          <span className="text-[11px] font-black uppercase tracking-[0.12em] text-[#E65527]">
            How It Works
          </span>
        </div>
        <h2 className="text-3xl font-black tracking-tight mb-12 max-w-sm">
          From card scan to scored lead in four steps
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-px bg-black/8">
          {STEPS.map(({ n, icon: Icon, title, desc }) => (
            <div
              key={n}
              className="bg-white p-8 group hover:bg-[#E65527]/3 transition-colors"
            >
              <div className="font-black text-3xl text-[#E65527]/15 mb-5 group-hover:text-[#E65527]/30 transition-colors font-mono">
                {n}
              </div>
              <div className="w-10 h-10 border border-[#E65527]/20 flex items-center justify-center mb-5 group-hover:border-[#E65527] group-hover:bg-[#E65527]/5 transition-all">
                <Icon size={17} className="text-[#E65527]" />
              </div>
              <h3 className="font-bold text-base mb-2">{title}</h3>
              <p className="text-sm text-black/50 leading-relaxed">{desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Features */}
      <section className="bg-[#fafafa] py-20 border-y border-black/8">
        <div className="max-w-6xl mx-auto px-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-8 h-px bg-[#E65527]" />
            <span className="text-[11px] font-black uppercase tracking-[0.12em] text-[#E65527]">
              Features
            </span>
          </div>
          <h2 className="text-3xl font-black tracking-tight mb-12 max-w-sm">
            Everything a field sales team needs after an exhibition
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-px bg-black/8">
            {FEATURES.map(({ icon: Icon, title, desc }) => (
              <div key={title} className="bg-white p-8 group">
                <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center mb-5 group-hover:bg-[#E65527] transition-colors">
                  <Icon
                    size={17}
                    className="text-[#E65527] group-hover:text-white transition-colors"
                  />
                </div>
                <h3 className="font-bold mb-2">{title}</h3>
                <p className="text-sm text-black/50 leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA Banner */}
      <CtaBanner
        heading="See it in action"
        subcopy="No signup required for the demo. Load a sample batch in 30 seconds."
        ctaLabel="Try Demo"
        onCtaClick={() => router.push("/product")}
      />

      {/* Footer */}
      <footer className="border-t border-black/10 bg-white py-10">
        <div className="max-w-6xl mx-auto px-6 flex flex-col md:flex-row items-center justify-between gap-4">
          <DashrLogo onClick={() => router.push("/")} height={28} />
          <div className="flex items-center gap-6 text-sm text-black/45">
            <button
              onClick={() => router.push("/product")}
              className="hover:text-black transition-colors"
            >
              Product
            </button>
            <button
              onClick={() => router.push("/faq")}
              className="hover:text-black transition-colors"
            >
              FAQ
            </button>
            <span className="hover:text-black transition-colors cursor-pointer">
              Pricing
            </span>
            <button
              onClick={() => router.push("/login")}
              className="hover:text-black transition-colors"
            >
              Login
            </button>
            <a
              href="mailto:hello@dashr.ai"
              className="hover:text-black transition-colors"
            >
              hello@dashr.ai
            </a>
          </div>
          <span className="text-xs text-black/25">© 2024 DASHR AI. All rights reserved.</span>
        </div>
      </footer>
    </div>
  );
}
