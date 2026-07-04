"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { DashrLogo } from "@/components/dashr-logo";
import { OBtn } from "@/components/buttons";
import { signup, verifyOtp, resendOtp, ApiError } from "@/lib/api";

type Mode = "login" | "signup" | "verify-otp";

const HEADINGS: Record<Mode, { title: string; subtitle: string }> = {
  login: { title: "Welcome back", subtitle: "Sign in to your DASHR AI workspace" },
  signup: { title: "Create account", subtitle: "Start with a 14-day free trial" },
  "verify-otp": {
    title: "Verify your phone",
    subtitle: "Enter the 6-digit code we sent to your phone",
  },
};

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");

  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [phoneDigits, setPhoneDigits] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const [userId, setUserId] = useState<string | null>(null);
  const [otpCode, setOtpCode] = useState("");
  const [resendCooldown, setResendCooldown] = useState(0);

  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setInterval(() => {
      setResendCooldown((s) => Math.max(0, s - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [resendCooldown]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (mode === "login") {
      router.push("/dashboard");
      return;
    }

    if (mode === "signup") {
      setLoading(true);
      try {
        const res = await signup({
          name,
          email,
          phone_no: `+91${phoneDigits}`,
          password,
        });
        setUserId(res.user_id);
        setMode("verify-otp");
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Something went wrong");
      } finally {
        setLoading(false);
      }
      return;
    }

    if (mode === "verify-otp") {
      if (!userId) return;
      setLoading(true);
      try {
        await verifyOtp({ user_id: userId, otp_code: otpCode });
        router.push("/dashboard");
      } catch {
        setError("Incorrect or expired code");
      } finally {
        setLoading(false);
      }
    }
  }

  async function handleResend() {
    if (!userId || resendCooldown > 0) return;
    setError(null);
    try {
      await resendOtp({ user_id: userId });
      setResendCooldown(30);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 429
          ? "Please wait before requesting another code"
          : "Could not resend code"
      );
    }
  }

  const heading = HEADINGS[mode];

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
          <h1 className="text-2xl font-black mb-1">{heading.title}</h1>
          <p className="text-sm text-black/40 mb-8">{heading.subtitle}</p>

          <form className="space-y-4" onSubmit={onSubmit}>
            {mode === "signup" && (
              <>
                <div>
                  <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                    Full Name
                  </label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Akshay Jain"
                    className="w-full border border-black/15 px-4 py-2.5 text-sm focus:outline-none focus:border-[#E65527] transition-colors bg-white"
                  />
                </div>
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
                <div>
                  <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                    Phone Number
                  </label>
                  <div className="flex items-stretch border border-black/15 focus-within:border-[#E65527] transition-colors bg-white">
                    <span className="flex items-center px-3 text-sm font-semibold text-black/50 border-r border-black/15">
                      +91
                    </span>
                    <input
                      type="tel"
                      inputMode="numeric"
                      maxLength={10}
                      value={phoneDigits}
                      onChange={(e) =>
                        setPhoneDigits(e.target.value.replace(/\D/g, "").slice(0, 10))
                      }
                      placeholder="9876543210"
                      className="w-full px-4 py-2.5 text-sm focus:outline-none bg-white"
                    />
                  </div>
                </div>
              </>
            )}

            {(mode === "login" || mode === "signup") && (
              <>
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
              </>
            )}

            {mode === "verify-otp" && (
              <div>
                <label className="text-[11px] font-black uppercase tracking-wider text-black/50 block mb-1.5">
                  Verification Code
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={6}
                  pattern="[0-9]*"
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                  placeholder="123456"
                  className="w-full border border-black/15 px-4 py-2.5 text-sm text-center tracking-[0.5em] font-bold focus:outline-none focus:border-[#E65527] transition-colors bg-white"
                />
                <button
                  type="button"
                  disabled={resendCooldown > 0}
                  onClick={handleResend}
                  className="mt-2 text-xs text-[#E65527] font-bold hover:underline disabled:opacity-40 disabled:no-underline disabled:cursor-not-allowed"
                >
                  {resendCooldown > 0 ? `Resend code (${resendCooldown}s)` : "Resend code"}
                </button>
              </div>
            )}

            {error && <p className="text-sm text-red-600">{error}</p>}

            <OBtn
              type="submit"
              className="w-full py-3 mt-2 text-base"
              disabled={loading || (mode === "verify-otp" && otpCode.length !== 6)}
            >
              {loading
                ? "Please wait…"
                : mode === "login"
                ? "Sign In"
                : mode === "signup"
                ? "Create Account"
                : "Verify"}
            </OBtn>
          </form>

          {mode !== "verify-otp" && (
            <div className="mt-6 pt-6 border-t border-black/8 text-sm text-center text-black/45">
              {mode === "login" ? (
                <>
                  Don&apos;t have an account?{" "}
                  <button
                    onClick={() => {
                      setError(null);
                      setMode("signup");
                    }}
                    className="text-[#E65527] font-bold hover:underline"
                  >
                    Sign up free
                  </button>
                </>
              ) : (
                <>
                  Already have an account?{" "}
                  <button
                    onClick={() => {
                      setError(null);
                      setMode("login");
                    }}
                    className="text-[#E65527] font-bold hover:underline"
                  >
                    Sign in
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
