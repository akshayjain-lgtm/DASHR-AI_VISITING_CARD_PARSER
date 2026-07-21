import { ArrowRight, User } from "lucide-react";

export function HeroViz() {
  return (
    // Three fixed-width panels with no wrap would overflow any phone
    // viewport — flex-wrap lets the row fold onto multiple lines instead of
    // forcing the page wider, and the panels themselves shrink a step on
    // mobile so it takes narrower screens longer to need that wrap at all.
    <div className="mt-16 flex flex-wrap items-center gap-3 sm:gap-4">
      {/* Card */}
      <div className="w-28 sm:w-36 h-20 border border-black/15 bg-white flex flex-col items-start justify-center px-3 sm:px-4 gap-1.5 shadow-[0_1px_3px_rgba(0,0,0,0.06)]">
        <div className="w-8 h-8 bg-[#E65527]/10 flex items-center justify-center">
          <User size={14} className="text-[#E65527]" />
        </div>
        <div className="w-20 h-1.5 bg-black/15 rounded-full" />
        <div className="w-14 h-1.5 bg-black/8 rounded-full" />
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <div className="w-5 sm:w-8 h-px bg-[#E65527]" />
        <ArrowRight size={12} className="text-[#E65527] shrink-0" />
      </div>
      {/* Table */}
      <div className="w-28 sm:w-36 h-20 border border-black/15 bg-white flex flex-col justify-center px-3 gap-1">
        <div className="flex gap-2 pb-1 mb-0.5 border-b border-black/8">
          <div className="w-14 h-1.5 bg-black/20 rounded-full" />
          <div className="w-10 h-1.5 bg-black/12 rounded-full" />
        </div>
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex gap-2">
            <div className="w-14 h-1.5 bg-black/10 rounded-full" />
            <div className="w-10 h-1.5 bg-black/6 rounded-full" />
          </div>
        ))}
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <div className="w-5 sm:w-8 h-px bg-[#E65527]" />
        <ArrowRight size={12} className="text-[#E65527] shrink-0" />
      </div>
      {/* Score */}
      <div className="w-28 sm:w-36 h-20 border-2 border-[#E65527] bg-white flex flex-col items-center justify-center gap-0.5">
        <span className="text-2xl font-black text-[#E65527]">87%</span>
        <span className="text-[9px] font-black uppercase tracking-[0.15em] text-[#E65527]">
          High Fit
        </span>
      </div>
    </div>
  );
}
