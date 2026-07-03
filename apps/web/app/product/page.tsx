"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Upload, CheckCircle, Award, ArrowRight } from "lucide-react";
import { Navbar } from "@/components/navbar";
import { OBtn, GBtn } from "@/components/buttons";

const SAMPLE_LEADS = [
  { name: "Rajesh Kumar", company: "Bharat Heavy Electricals", designation: "Head of Procurement", score: 91 },
  { name: "Priya Nair", company: "Larsen & Toubro", designation: "VP Operations", score: 84 },
  { name: "Suresh Patel", company: "Mahindra Manufacturing", designation: "Plant Manager", score: 72 },
];

function ScorePill({ score }: { score: number }) {
  if (score >= 80)
    return (
      <span className="inline-flex px-2.5 py-0.5 text-xs font-black bg-[#E65527] text-white">
        {score}% High Fit
      </span>
    );
  return (
    <span className="inline-flex px-2.5 py-0.5 text-xs font-black bg-black/8 text-black/60">
      {score}% Med Fit
    </span>
  );
}

export default function ProductPage() {
  const router = useRouter();
  const [uploaded, setUploaded] = useState(false);
  const [dragging, setDragging] = useState(false);

  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="border-b border-black/10 py-14 px-6">
        <div className="max-w-4xl mx-auto">
          <div className="flex items-center gap-2 text-[11px] font-black uppercase tracking-[0.12em] text-[#E65527] mb-4">
            <div className="w-4 h-px bg-[#E65527]" /> Product Demo
          </div>
          <h1 className="text-4xl font-black tracking-tight mb-3">See how DASHR AI works</h1>
          <p className="text-black/50 text-lg max-w-lg">
            Upload a batch, watch extraction run, review scored leads — all in under two minutes.
          </p>
        </div>
      </section>

      <div className="max-w-4xl mx-auto px-6 py-14 space-y-16">
        {/* Step 01 */}
        <div>
          <div className="flex items-center gap-3 mb-6">
            <span className="text-[11px] font-black font-mono text-[#E65527] bg-[#E65527]/8 px-2.5 py-1 uppercase tracking-wider">
              STEP 01
            </span>
            <h2 className="font-bold text-lg">Bulk Upload Visiting Cards</h2>
          </div>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={() => {
              setDragging(false);
              setUploaded(true);
            }}
            onClick={() => setUploaded(true)}
            className={`border-2 border-dashed cursor-pointer py-16 flex flex-col items-center justify-center gap-4 transition-all ${
              dragging
                ? "border-[#E65527] bg-[#E65527]/5"
                : uploaded
                ? "border-green-500 bg-green-50/60"
                : "border-black/20 hover:border-[#E65527]/40 hover:bg-[#E65527]/2"
            }`}
          >
            {uploaded ? (
              <>
                <CheckCircle size={36} className="text-green-500" />
                <div className="text-center">
                  <p className="font-bold text-green-700">147 cards uploaded successfully</p>
                  <p className="text-sm text-green-600/70 mt-0.5">
                    Processing extraction… 3 of 147 complete
                  </p>
                </div>
              </>
            ) : (
              <>
                <div className="w-14 h-14 bg-[#E65527]/8 flex items-center justify-center">
                  <Upload size={24} className="text-[#E65527]" />
                </div>
                <div className="text-center">
                  <p className="font-bold mb-1">Drop card images here or click to browse</p>
                  <p className="text-sm text-black/40">
                    Supports JPG, PNG, PDF · Up to 500 cards per batch
                  </p>
                </div>
                <OBtn>Select Files</OBtn>
              </>
            )}
          </div>
        </div>

        {/* Step 02 */}
        <div>
          <div className="flex items-center gap-3 mb-6">
            <span className="text-[11px] font-black font-mono text-[#E65527] bg-[#E65527]/8 px-2.5 py-1 uppercase tracking-wider">
              STEP 02
            </span>
            <h2 className="font-bold text-lg">Auto-Extracted Contact Data</h2>
          </div>
          <div className="border border-black/10 overflow-hidden">
            <div className="grid grid-cols-4 gap-4 bg-black/3 border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/40">
              <span>Name</span>
              <span>Company</span>
              <span>Designation</span>
              <span>Status</span>
            </div>
            {SAMPLE_LEADS.map(({ name, company, designation }) => (
              <div
                key={name}
                className="grid grid-cols-4 gap-4 px-5 py-3.5 border-b border-black/5 text-sm hover:bg-black/2 transition-colors"
              >
                <span className="font-semibold">{name}</span>
                <span className="text-black/55">{company}</span>
                <span className="text-black/55">{designation}</span>
                <span className="flex items-center gap-1.5 text-xs text-green-600 font-semibold">
                  <CheckCircle size={11} /> Extracted
                </span>
              </div>
            ))}
            <div className="px-5 py-2.5 text-xs text-black/30">Showing 3 of 147 records</div>
          </div>
        </div>

        {/* Step 03 */}
        <div>
          <div className="flex items-center gap-3 mb-6">
            <span className="text-[11px] font-black font-mono text-[#E65527] bg-[#E65527]/8 px-2.5 py-1 uppercase tracking-wider">
              STEP 03
            </span>
            <h2 className="font-bold text-lg">Enriched &amp; Scored Leads</h2>
          </div>
          <div className="border border-black/10 overflow-hidden mb-6">
            <div className="grid grid-cols-4 gap-4 bg-black/3 border-b border-black/8 px-5 py-3 text-[11px] font-black uppercase tracking-wider text-black/40">
              <span>Name</span>
              <span>Company</span>
              <span>Designation</span>
              <span>Lead Score</span>
            </div>
            {SAMPLE_LEADS.map(({ name, company, designation, score }) => (
              <div
                key={name}
                className="grid grid-cols-4 gap-4 px-5 py-4 border-b border-black/5 text-sm hover:bg-black/2 transition-colors items-center"
              >
                <span className="font-semibold">{name}</span>
                <span className="text-black/55">{company}</span>
                <span className="text-black/55">{designation}</span>
                <ScorePill score={score} />
              </div>
            ))}
          </div>
          <div className="border border-[#E65527]/25 bg-[#E65527]/4 p-5 flex items-start gap-4">
            <div className="w-10 h-10 bg-[#E65527] flex items-center justify-center shrink-0">
              <Award size={17} className="text-white" />
            </div>
            <div>
              <p className="font-bold mb-1">Score: 91% — High Fit</p>
              <p className="text-sm text-black/55 leading-relaxed">
                Rajesh Kumar at BHEL matches your target profile: heavy engineering
                procurement, 5 000+ employee company, active capital expenditure cycle.
                Recommended follow-up within 48 hours.
              </p>
            </div>
          </div>
        </div>

        {/* CTA */}
        <div className="text-center border-t border-black/8 pt-12">
          <h2 className="text-2xl font-black mb-3">Ready to process your own leads?</h2>
          <p className="text-black/50 mb-7 text-sm">
            Set up your company profile to calibrate scoring for your product lines.
          </p>
          <div className="flex items-center justify-center gap-4">
            <OBtn onClick={() => router.push("/login")} className="px-7 py-3 text-base">
              Create Free Account <ArrowRight size={15} />
            </OBtn>
            <GBtn onClick={() => router.push("/")}>← Back to Home</GBtn>
          </div>
        </div>
      </div>
    </div>
  );
}
