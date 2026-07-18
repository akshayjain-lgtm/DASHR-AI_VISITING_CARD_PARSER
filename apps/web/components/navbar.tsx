"use client";

import { useRouter } from "next/navigation";
import { DashrLogo } from "./dashr-logo";
import { OBtn, GBtn } from "./buttons";

export function Navbar() {
  const router = useRouter();

  return (
    <nav className="border-b border-black/10 bg-white sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
        <DashrLogo onClick={() => router.push("/")} height={34} />
        <div className="hidden md:flex items-center gap-8">
          <button
            onClick={() => router.push("/")}
            className="text-sm font-semibold text-black/60 hover:text-black transition-colors"
          >
            Home
          </button>
          <button
            onClick={() => router.push("/pricing")}
            className="text-sm font-semibold text-black/60 hover:text-black transition-colors"
          >
            Pricing
          </button>
          <button
            onClick={() => router.push("/faq")}
            className="text-sm font-semibold text-black/60 hover:text-black transition-colors"
          >
            FAQ
          </button>
          <button
            onClick={() => router.push("/contact")}
            className="text-sm font-semibold text-black/60 hover:text-black transition-colors"
          >
            Contact Us
          </button>
        </div>
        <div className="flex items-center gap-3">
          <GBtn onClick={() => router.push("/login")}>Login</GBtn>
          <OBtn onClick={() => router.push("/product")}>Try Demo</OBtn>
        </div>
      </div>
    </nav>
  );
}
