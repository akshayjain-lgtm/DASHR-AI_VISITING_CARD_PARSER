"use client";

import { useRouter } from "next/navigation";
import { HelpCircle } from "lucide-react";
import { Navbar } from "@/components/navbar";
import { CtaBanner } from "@/components/cta-banner";
import { FaqAccordion, type FaqCategory } from "@/components/faq-accordion";

const FAQ_CATEGORIES: FaqCategory[] = [
  {
    category: "Getting Started",
    items: [
      {
        question: "What does DASHR AI actually do?",
        answer:
          "DASHR AI turns a stack of visiting cards collected at a trade exhibition into a scored, exportable lead list. Upload photos of the cards, and DASHR AI extracts contact details with AI, enriches each contact with public company data, and scores every lead for fit against your product profile.",
      },
      {
        question: "How many cards can I upload at once?",
        answer:
          "Upload a batch of scanned or photographed cards in one go — JPG, PNG, and PDF are supported, up to 500 cards per batch. Processing runs in the background, so you don't wait on the upload screen while it works.",
      },
      {
        question: "Do I need to create an account to try it?",
        answer:
          "You can try a live demo on the Product page with no signup required. Uploading your own cards and saving leads to your workspace requires a free account.",
      },
    ],
  },
  {
    category: "Extraction & Enrichment",
    items: [
      {
        question: "What fields does DASHR AI extract from a card?",
        answer:
          "Name, job title, company, email, phone number, and address are parsed from each card image using a vision AI model, then run through a validation pass that catches malformed emails, missing fields, and other extraction errors before anything is saved.",
      },
      {
        question: "What is company enrichment?",
        answer:
          "Once a company name is extracted from a card, DASHR AI cross-references it against public business data to attach firmographics — industry classification, employee count, revenue band, and location — so you know more about a lead than just their name and title.",
      },
      {
        question: "What if the AI misreads a card, like bad handwriting?",
        answer:
          "Extraction confidence is stored alongside every parsed card, so a badly misread card is visible on the Upload page. There's no manual field-correction tool today — if a card comes through wrong, delete it and re-upload a clearer photo.",
      },
    ],
  },
  {
    category: "Lead Scoring",
    items: [
      {
        question: "How is a lead's score calculated?",
        answer:
          "Each lead is scored against a configurable product-fit model using an explainable, rules-based engine — industry code, company size, revenue band, and the contact's title/seniority all factor in, so you can see why a lead scored the way it did instead of trusting an opaque black box.",
      },
      {
        question: "Can I tune the scoring model to my own product?",
        answer:
          "Yes — your target customer profile (industry, company size, regions, product lines) is set on your Company Profile page and calibrates how every lead is scored.",
      },
    ],
  },
  {
    category: "Wallet & Billing",
    items: [
      {
        question: "Is there a free tier?",
        answer:
          "Every user gets their first 20 parses, 20 enrichments, and 20 scorings free — tracked as three independent counters, so using up your free parses doesn't touch your free enrichments or scorings.",
      },
      {
        question: "What does it cost after the free tier runs out?",
        answer:
          "₹5 per card parsed, ₹3 per enrichment, and ₹2 per scoring, debited from your own prepaid wallet. If your wallet balance hits zero after your free allowance is used up, that action type is blocked until you recharge — nothing runs, and nothing is billed, without balance to cover it.",
      },
      {
        question: "Is the wallet shared across my team?",
        answer:
          "No. Every user — admin or team member — has their own wallet and spends independently. There is no shared org-level balance, and an admin can never spend from a team member's wallet.",
      },
      {
        question: "Can I withdraw or get a refund on my wallet balance?",
        answer:
          "Recharged balance is spend-only from the website and isn't refundable or withdrawable through self-serve tools. If you need funds back, reach out to customer care.",
      },
      {
        question: "How does invoicing work?",
        answer:
          "An invoice is generated each time you recharge your wallet — not per card parsed or per batch — under a single line item, \"Visiting Card Recharge and Scoring.\" It's billed to you individually and is visible to you and any admin in your organization.",
      },
    ],
  },
  {
    category: "Team & Roles",
    items: [
      {
        question: "What's the difference between an Admin and a team member?",
        answer:
          "Every user in your organization is either an Admin or a team member. That role only controls data visibility — who can see which leads, cards, and exhibitions within your org — not billing. The Admin can invite teammates, deactivate or reactivate members, and transfer admin ownership to another member.",
      },
      {
        question: "Can an Admin see or spend a team member's wallet balance?",
        answer:
          "No. Every user, including the Admin, has their own wallet, and no one else can recharge, spend, or view its balance. The one exception is Invoices: an Admin can see every team member's invoices, but that visibility is read-only and never grants spending authority.",
      },
    ],
  },
];

export default function FaqPage() {
  const router = useRouter();

  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="max-w-4xl mx-auto px-6 pt-16 pb-10">
        <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-6">
          <HelpCircle size={11} />
          Frequently Asked Questions
        </div>
        <p className="text-black/50 text-lg max-w-lg">
          Everything about turning a stack of trade-show cards into a scored, exportable lead
          list.
        </p>
      </section>

      <div className="max-w-4xl mx-auto px-6 pb-16 space-y-12">
        {FAQ_CATEGORIES.map(({ category, items }) => (
          <div key={category}>
            <h2 className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-4">
              {category}
            </h2>
            <FaqAccordion items={items} />
          </div>
        ))}
      </div>

      <CtaBanner
        heading="Still have questions?"
        subcopy="Try the demo, or reach out and we'll answer directly."
        ctaLabel="Try Demo"
        onCtaClick={() => router.push("/product")}
        secondary={
          <a href="mailto:hello@dashr.ai" className="text-white font-bold text-sm hover:underline">
            hello@dashr.ai
          </a>
        }
      />
    </div>
  );
}
