"use client";

import type { ReactNode } from "react";
import { ArrowRight } from "lucide-react";

export function CtaBanner({
  heading,
  subcopy,
  ctaLabel,
  onCtaClick,
  secondary,
}: {
  heading: string;
  subcopy: string;
  ctaLabel: string;
  onCtaClick: () => void;
  secondary?: ReactNode;
}) {
  return (
    <section className="bg-[#E65527] py-16">
      <div className="max-w-6xl mx-auto px-6 flex flex-col md:flex-row items-center justify-between gap-6">
        <div>
          <h2 className="text-2xl font-black text-white mb-1">{heading}</h2>
          <p className="text-white/65 text-sm">{subcopy}</p>
        </div>
        <div className="flex items-center gap-6">
          <button
            onClick={onCtaClick}
            className="bg-white text-[#E65527] px-7 py-3 font-black text-sm hover:bg-white/90 transition-colors whitespace-nowrap inline-flex items-center gap-2"
          >
            {ctaLabel} <ArrowRight size={14} />
          </button>
          {secondary}
        </div>
      </div>
    </section>
  );
}
