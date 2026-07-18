"use client";

import { useState } from "react";
import { Phone, Mail, MessageSquare, CheckCircle } from "lucide-react";
import { Navbar } from "@/components/navbar";
import { OBtn } from "@/components/buttons";
import { ApiError, submitContactEnquiry } from "@/lib/api";

const CONTACT_PHONE = "7982188283";
const CONTACT_EMAIL = "akshay.jain@dashrtech.com";

type EnquiryForm = {
  name: string;
  phone_no: string;
  email: string;
  query: string;
};

const EMPTY_FORM: EnquiryForm = { name: "", phone_no: "", email: "", query: "" };

export default function ContactPage() {
  const [form, setForm] = useState<EnquiryForm>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await submitContactEnquiry(form);
      setSubmitted(true);
      setForm(EMPTY_FORM);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send your enquiry. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="bg-white min-h-screen">
      <Navbar />

      <section className="max-w-4xl mx-auto px-6 pt-16 pb-10">
        <div className="inline-flex items-center gap-2 border border-[#E65527]/25 bg-[#E65527]/5 px-3 py-1.5 text-[11px] font-black text-[#E65527] uppercase tracking-[0.12em] mb-6">
          <MessageSquare size={11} />
          Contact Us
        </div>
        <h1 className="text-3xl font-black tracking-tight mb-3">We&apos;re here to help</h1>
        <p className="text-black/50 text-lg max-w-lg">
          Reach out directly, or send a quick note below and we&apos;ll get back to you.
        </p>
      </section>

      <div className="max-w-4xl mx-auto px-6 pb-20 grid grid-cols-1 md:grid-cols-[1fr_1.4fr] gap-10">
        {/* Direct contact details */}
        <div className="space-y-4">
          <a
            href={`tel:+91${CONTACT_PHONE}`}
            className="flex items-start gap-4 border border-black/10 p-5 hover:border-[#E65527]/40 transition-colors"
          >
            <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center shrink-0">
              <Phone size={17} className="text-[#E65527]" />
            </div>
            <div>
              <p className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-1">
                Phone
              </p>
              <p className="font-bold">+91 {CONTACT_PHONE}</p>
            </div>
          </a>
          <a
            href={`mailto:${CONTACT_EMAIL}`}
            className="flex items-start gap-4 border border-black/10 p-5 hover:border-[#E65527]/40 transition-colors"
          >
            <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center shrink-0">
              <Mail size={17} className="text-[#E65527]" />
            </div>
            <div>
              <p className="text-[11px] font-black uppercase tracking-wider text-black/40 mb-1">
                Email
              </p>
              <p className="font-bold break-all">{CONTACT_EMAIL}</p>
            </div>
          </a>
        </div>

        {/* Enquiry form */}
        <div className="border border-black/10 p-6">
          {submitted ? (
            <div className="py-10 text-center">
              <CheckCircle size={28} className="mx-auto mb-3 text-green-600" />
              <p className="font-bold mb-1">Thanks — we&apos;ve got your message</p>
              <p className="text-sm text-black/50">We&apos;ll get back to you shortly.</p>
              <button
                onClick={() => setSubmitted(false)}
                className="text-sm text-[#E65527] font-bold mt-4 hover:underline"
              >
                Send another enquiry
              </button>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-5">
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Name
                </label>
                <input
                  type="text"
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Your name"
                  disabled={submitting}
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
                />
              </div>
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Phone No
                </label>
                <input
                  type="tel"
                  required
                  value={form.phone_no}
                  onChange={(e) => setForm({ ...form, phone_no: e.target.value })}
                  placeholder="Your phone number"
                  disabled={submitting}
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
                />
              </div>
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Email Id
                </label>
                <input
                  type="email"
                  required
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  placeholder="you@company.com"
                  disabled={submitting}
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60"
                />
              </div>
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Query / Issue
                </label>
                <textarea
                  required
                  rows={4}
                  value={form.query}
                  onChange={(e) => setForm({ ...form, query: e.target.value })}
                  placeholder="How can we help?"
                  disabled={submitting}
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white resize-none disabled:opacity-60"
                />
              </div>

              {error && <p className="text-sm text-red-600">{error}</p>}

              <OBtn type="submit" disabled={submitting} className="w-full justify-center">
                {submitting ? "Sending…" : "Submit Enquiry"}
              </OBtn>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
