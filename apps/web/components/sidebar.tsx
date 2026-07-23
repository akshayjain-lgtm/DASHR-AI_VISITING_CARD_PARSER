"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { LayoutDashboard, Upload, Wallet, Settings, HelpCircle, MessageSquare, Menu, X } from "lucide-react";
import { DashrLogo } from "./dashr-logo";

const NAV = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard, path: "/dashboard" },
  { id: "upload", label: "Cardex", icon: Upload, path: "/upload" },
  { id: "wallet", label: "Wallet", icon: Wallet, path: "/wallet" },
  { id: "settings", label: "Settings", icon: Settings, path: "/settings" },
  { id: "faq", label: "FAQ", icon: HelpCircle, path: "/faq" },
  { id: "feedback", label: "Feedback", icon: MessageSquare, path: "/feedback" },
];

// The logo header and nav list — identical between the always-visible
// desktop column and the mobile slide-in drawer, so both render this
// instead of duplicating the chrome around navButtons(). Only `onClose`
// (mobile-only, renders a close button in the header) differs.
//
// Sign Out deliberately does NOT live here — it's on the Settings page
// (top-right of the Company Profile / Roles and Access tab row), not
// duplicated across every logged-in page.
function SidebarContent({
  active,
  onNavigate,
  onClose,
}: {
  active: string;
  onNavigate: (path: string) => void;
  onClose?: () => void;
}) {
  return (
    <>
      <div className="px-5 py-4 border-b border-white/8 flex items-center justify-between">
        <DashrLogo onClick={() => onNavigate("/dashboard")} height={28} />
        {onClose && (
          <button onClick={onClose} aria-label="Close menu" className="text-white/70 hover:text-white p-1">
            <X size={18} />
          </button>
        )}
      </div>
      <nav className="flex-1 p-3 space-y-0.5 mt-1">
        {NAV.map(({ id, label, icon: Icon, path }) => (
          <button
            key={id}
            onClick={() => onNavigate(path)}
            className={`w-full flex items-center gap-3 px-3 py-2.5 text-sm transition-colors ${
              active === id
                ? "bg-[#E65527] text-white font-bold"
                : "text-white/45 hover:text-white hover:bg-white/5"
            }`}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </nav>
    </>
  );
}

// Fixed w-52 column at sm+ (unchanged desktop/tablet behavior); below that,
// the always-visible column would eat ~40% of a phone screen's width, so it
// collapses behind a hamburger-triggered slide-in drawer instead. Shared by
// every page (Dashboard, Cardex, Wallet, Settings) — this is the one place
// that needs to change for all of them to be phone-usable.
export function Sidebar({ active }: { active: string }) {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <>
      {/* Mobile top bar + hamburger trigger — below sm only */}
      <div className="sm:hidden flex items-center justify-between px-4 py-3 bg-[#0d0d0d] sticky top-0 z-30">
        <DashrLogo onClick={() => router.push("/dashboard")} height={24} />
        <button
          onClick={() => setMobileOpen(true)}
          aria-label="Open menu"
          className="text-white/70 hover:text-white p-1"
        >
          <Menu size={20} />
        </button>
      </div>

      {/* Mobile slide-in drawer */}
      {mobileOpen && (
        <div className="sm:hidden fixed inset-0 z-40 flex">
          <aside className="w-64 bg-[#0d0d0d] min-h-screen flex flex-col shrink-0">
            <SidebarContent
              active={active}
              onNavigate={(path) => {
                setMobileOpen(false);
                router.push(path);
              }}
              onClose={() => setMobileOpen(false)}
            />
          </aside>
          <div className="flex-1 bg-black/40" onClick={() => setMobileOpen(false)} />
        </div>
      )}

      {/* Desktop/tablet sidebar — unchanged */}
      <aside className="hidden sm:flex w-52 bg-[#0d0d0d] min-h-screen flex-col shrink-0">
        <SidebarContent active={active} onNavigate={(path) => router.push(path)} />
      </aside>
    </>
  );
}
