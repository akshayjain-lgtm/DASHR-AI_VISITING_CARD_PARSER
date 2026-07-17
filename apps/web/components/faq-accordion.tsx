"use client";

import { useId, useState } from "react";
import { ChevronDown } from "lucide-react";

export type FaqItem = {
  question: string;
  answer: string;
};

export type FaqCategory = {
  category: string;
  items: FaqItem[];
};

export function FaqAccordion({ items }: { items: FaqItem[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const baseId = useId();

  return (
    <div className="border border-black/10 divide-y divide-black/10">
      {items.map((item, i) => {
        const isOpen = openIndex === i;
        const buttonId = `${baseId}-question-${i}`;
        const panelId = `${baseId}-answer-${i}`;
        return (
          <div key={i}>
            <button
              id={buttonId}
              onClick={() => setOpenIndex(isOpen ? null : i)}
              aria-expanded={isOpen}
              aria-controls={panelId}
              className="w-full flex items-center justify-between gap-4 px-6 py-5 text-left"
            >
              <span className={`font-bold text-sm ${isOpen ? "text-[#E65527]" : "text-black"}`}>
                {item.question}
              </span>
              <ChevronDown
                size={16}
                className={`shrink-0 transition-transform ${
                  isOpen ? "rotate-180 text-[#E65527]" : "text-black/40"
                }`}
              />
            </button>
            {isOpen && (
              <div
                id={panelId}
                role="region"
                aria-labelledby={buttonId}
                className="px-6 pb-5 text-sm text-black/55 leading-relaxed"
              >
                {item.answer}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
