"use client";

import { useState } from "react";
import { CheckCircle, MessageSquare, LifeBuoy } from "lucide-react";
import { Sidebar } from "@/components/sidebar";
import { OBtn } from "@/components/buttons";
import { ApiError, submitFeedback, submitSupportQuery } from "@/lib/api";

const INPUT_CLASS =
  "w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white disabled:opacity-60";
const TEXTAREA_CLASS = `${INPUT_CLASS} resize-none`;
const LABEL_CLASS = "text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5";

type FeedbackForm = { what_worked: string; what_went_wrong: string };
type QueryForm = { subject: string; message: string };

const EMPTY_FEEDBACK: FeedbackForm = { what_worked: "", what_went_wrong: "" };
const EMPTY_QUERY: QueryForm = { subject: "", message: "" };

function FeedbackSection() {
  const [form, setForm] = useState<FeedbackForm>(EMPTY_FEEDBACK);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bothBlank = !form.what_worked.trim() && !form.what_went_wrong.trim();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (bothBlank) {
      setError("Tell us at least one of what's working or what went wrong.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await submitFeedback({
        what_worked: form.what_worked.trim() || undefined,
        what_went_wrong: form.what_went_wrong.trim() || undefined,
      });
      setSubmitted(true);
      setForm(EMPTY_FEEDBACK);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send your feedback. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="border border-black/10 p-6">
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center shrink-0">
          <MessageSquare size={17} className="text-[#E65527]" />
        </div>
        <div>
          <h2 className="font-black">Feedback</h2>
          <p className="text-sm text-black/45">Tell us what's working and what isn't.</p>
        </div>
      </div>

      {submitted ? (
        <div className="py-8 text-center">
          <CheckCircle size={28} className="mx-auto mb-3 text-green-600" />
          <p className="font-bold mb-1">Thanks — noted</p>
          <p className="text-sm text-black/50">We read every submission to improve the product.</p>
          <button
            onClick={() => setSubmitted(false)}
            className="text-sm text-[#E65527] font-bold mt-4 hover:underline"
          >
            Give more feedback
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label className={LABEL_CLASS}>What's working well?</label>
            <textarea
              aria-label="What's working well?"
              rows={3}
              value={form.what_worked}
              onChange={(e) => setForm({ ...form, what_worked: e.target.value })}
              placeholder="What do you like about DASHR AI?"
              disabled={submitting}
              className={TEXTAREA_CLASS}
            />
          </div>
          <div>
            <label className={LABEL_CLASS}>What went wrong?</label>
            <textarea
              aria-label="What went wrong?"
              rows={3}
              value={form.what_went_wrong}
              onChange={(e) => setForm({ ...form, what_went_wrong: e.target.value })}
              placeholder="What could be better?"
              disabled={submitting}
              className={TEXTAREA_CLASS}
            />
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <OBtn type="submit" disabled={submitting}>
            {submitting ? "Sending…" : "Submit Feedback"}
          </OBtn>
        </form>
      )}
    </div>
  );
}

function RaiseQuerySection() {
  const [form, setForm] = useState<QueryForm>(EMPTY_QUERY);
  const [submitting, setSubmitting] = useState(false);
  const [ticketId, setTicketId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await submitSupportQuery(form);
      setTicketId(result.ticket_id);
      setForm(EMPTY_QUERY);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't submit your query. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="border border-black/10 p-6">
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 bg-[#E65527]/8 flex items-center justify-center shrink-0">
          <LifeBuoy size={17} className="text-[#E65527]" />
        </div>
        <div>
          <h2 className="font-black">Raise a Query</h2>
          <p className="text-sm text-black/45">
            We'll email your team at info@dashrtech.com and give you a reference number.
          </p>
        </div>
      </div>

      {ticketId ? (
        <div className="py-8 text-center">
          <CheckCircle size={28} className="mx-auto mb-3 text-green-600" />
          <p className="font-bold mb-1">
            Query submitted — reference <span className="text-[#E65527]">{ticketId}</span>
          </p>
          <p className="text-sm text-black/50">We've emailed our team and will follow up.</p>
          <button
            onClick={() => setTicketId(null)}
            className="text-sm text-[#E65527] font-bold mt-4 hover:underline"
          >
            Submit another query
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label className={LABEL_CLASS}>Subject</label>
            <input
              type="text"
              aria-label="Subject"
              required
              value={form.subject}
              onChange={(e) => setForm({ ...form, subject: e.target.value })}
              placeholder="Short summary of the issue"
              disabled={submitting}
              className={INPUT_CLASS}
            />
          </div>
          <div>
            <label className={LABEL_CLASS}>Message</label>
            <textarea
              aria-label="Message"
              required
              rows={4}
              value={form.message}
              onChange={(e) => setForm({ ...form, message: e.target.value })}
              placeholder="Describe what's happening"
              disabled={submitting}
              className={TEXTAREA_CLASS}
            />
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <OBtn type="submit" disabled={submitting}>
            {submitting ? "Submitting…" : "Raise Query"}
          </OBtn>
        </form>
      )}
    </div>
  );
}

export default function FeedbackPage() {
  return (
    <div className="min-h-screen bg-white flex flex-col sm:flex-row">
      <Sidebar active="feedback" />
      <main className="flex-1 min-w-0 w-full p-4 sm:p-6 lg:p-10 max-w-3xl">
        <div className="mb-8">
          <h1 className="text-2xl font-black mb-1">Feedback</h1>
          <p className="text-sm text-black/45">
            Tell us what's working, what's not, or raise a support query directly.
          </p>
        </div>

        <div className="space-y-8">
          <FeedbackSection />
          <RaiseQuerySection />
        </div>
      </main>
    </div>
  );
}
