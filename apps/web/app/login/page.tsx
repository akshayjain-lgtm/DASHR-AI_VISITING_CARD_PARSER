"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { DashrLogo } from "@/components/dashr-logo";
import { OBtn } from "@/components/buttons";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [company, setCompany] = useState("");

  return (
    <div className="min-h-screen bg-white flex">
      {/* Left panel */}
      <div className="hidden lg:flex flex-col justify-between w-5/12 bg-[#0d0d0d] p-12">
        <DashrLogo onClick={() => router.push("/")} height={32} />
        <div>
          <div className="w-10 h-[3px] bg-[#E65527] mb-8" />
          <h2 className="text-3xl font-black text-white leading-tight mb-5">
            Your next big customer
            <br />
            is in that card stack.
          </h2>
          <p className="text-white/45 text-sm leading-relaxed max-w-xs">
            Industrial sellers use DASHR AI to process hundreds of exhibition
            contacts in minutes — not weeks.
          </p>
        </div>
        <p className="text-[11px] text-white/20">© 2024 DASHR AI</p>
      </div>

      {/* Right form */}
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-xs">
          <h1 className="text-2xl font-black mb-1">
            {mode === "login" ? "Welcome back" : "Create account"}
          </h1>
          <p className="text-sm text-black/40 mb-8">
            {mode === "login"
              ? "Sign in to your DASHR AI workspace"
              : "Start with a 14-day free trial"}
          </p>

          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              router.push("/dashboard");
            }}
          >
            {mode === "signup" && (
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Company Name
                </label>
                <input
                  type="text"
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  placeholder="Thermax Limited"
                  className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white"
                />
              </div>
            )}
            <div>
              <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                Work Email
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white"
              />
            </div>
            <div>
              <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white"
              />
            </div>
            <OBtn type="submit" className="w-full py-3 mt-2 text-base">
              {mode === "login" ? "Sign In" : "Create Account"}
            </OBtn>
          </form>

          <div className="mt-6 pt-6 border-t border-black/8 text-sm text-center text-black/45">
            {mode === "login" ? (
              <>
                Don&apos;t have an account?{" "}
                <button
                  onClick={() => setMode("signup")}
                  className="text-[#E65527] font-bold hover:underline"
                >
                  Sign up free
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button
                  onClick={() => setMode("login")}
                  className="text-[#E65527] font-bold hover:underline"
                >
                  Sign in
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
