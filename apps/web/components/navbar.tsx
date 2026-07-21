"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Menu, X } from "lucide-react";
import { DashrLogo } from "./dashr-logo";
import { OBtn, GBtn } from "./buttons";

const LINKS = [
  { label: "Home", path: "/" },
  { label: "Pricing", path: "/pricing" },
  { label: "FAQ", path: "/faq" },
  { label: "Contact Us", path: "/contact" },
];

export function Navbar() {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  function go(path: string) {
    setMobileOpen(false);
    router.push(path);
  }

  return (
    <nav className="border-b border-black/10 bg-white sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
        <DashrLogo onClick={() => go("/")} height={34} />
        <div className="hidden md:flex items-center gap-8">
          {LINKS.map(({ label, path }) => (
            <button
              key={path}
              onClick={() => go(path)}
              className="text-sm font-semibold text-black/60 hover:text-black transition-colors"
            >
              {label}
            </button>
          ))}
        </div>
        <div className="hidden md:flex items-center gap-3">
          <GBtn onClick={() => go("/login")}>Login</GBtn>
          <OBtn onClick={() => go("/product")}>Try Demo</OBtn>
        </div>

        {/* Below md, the links/buttons above are hidden entirely — this
            hamburger is the only way to reach them, so it must exist on
            every width under md, not just the very narrowest ones. */}
        <button
          onClick={() => setMobileOpen((v) => !v)}
          aria-label={mobileOpen ? "Close menu" : "Open menu"}
          className="md:hidden text-black/70 hover:text-black p-1"
        >
          {mobileOpen ? <X size={22} /> : <Menu size={22} />}
        </button>
      </div>

      {mobileOpen && (
        <div className="md:hidden border-t border-black/10 bg-white px-4 py-4 space-y-1">
          {LINKS.map(({ label, path }) => (
            <button
              key={path}
              onClick={() => go(path)}
              className="block w-full text-left px-2 py-2.5 text-sm font-semibold text-black/70 hover:text-black transition-colors"
            >
              {label}
            </button>
          ))}
          <div className="flex items-center gap-3 pt-3 mt-2 border-t border-black/8">
            <GBtn onClick={() => go("/login")} className="flex-1 justify-center">
              Login
            </GBtn>
            <OBtn onClick={() => go("/product")} className="flex-1 justify-center">
              Try Demo
            </OBtn>
          </div>
        </div>
      )}
    </nav>
  );
}
