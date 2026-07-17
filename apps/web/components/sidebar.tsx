"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  LayoutDashboard,
  Upload,
  Building2,
  Wallet,
  Settings,
  HelpCircle,
  LogOut,
  Menu,
  X,
} from "lucide-react";
import { DashrLogo } from "./dashr-logo";
import { logout } from "@/lib/api";

const NAV = [
  { id: "dashboard", label: "Leads", icon: LayoutDashboard, path: "/dashboard" },
  { id: "upload", label: "Upload", icon: Upload, path: "/upload" },
  { id: "profile", label: "Company Profile", icon: Building2, path: "/profile" },
  { id: "wallet", label: "Wallet", icon: Wallet, path: "/wallet" },
  { id: "settings", label: "Settings", icon: Settings, path: "/settings" },
  { id: "faq", label: "FAQ", icon: HelpCircle, path: "/faq" },
];

// The logo header, nav list, and sign-out button — identical between the
// always-visible desktop column and the mobile slide-in drawer, so both
// render this instead of duplicating the chrome around navButtons(). Only
// `onClose` (mobile-only, renders a close button in the header) differs.
function SidebarContent({
  active,
  onNavigate,
  onSignOut,
  onClose,
}: {
  active: string;
  onNavigate: (path: string) => void;
  onSignOut: () => void;
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
      <div className="p-3 border-t border-white/8">
        <button
          onClick={onSignOut}
          className="w-full flex items-center gap-3 px-3 py-2.5 text-sm text-white/35 hover:text-white transition-colors hover:bg-white/5"
        >
          <LogOut size={14} />
          Sign Out
        </button>
      </div>
    </>
  );
}

// Fixed w-52 column at sm+ (unchanged desktop/tablet behavior); below that,
// the always-visible column would eat ~40% of a phone screen's width, so it
// collapses behind a hamburger-triggered slide-in drawer instead. Shared by
// every page (Dashboard, Upload, Company Profile, Wallet, Settings) — this
// is the one place that needs to change for all of them to be phone-usable.
export function Sidebar({ active }: { active: string }) {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  async function handleSignOut() {
    try {
      await logout();
    } finally {
      // Hard navigation, not router.push: a soft client-side transition can
      // be served from Next's Router Cache, which may still hold a stale
      // "/" -> "/dashboard" redirect cached from while this session was
      // authenticated — that would bounce a freshly-logged-out user right
      // back into the dashboard. A full page load always re-hits middleware.
      window.location.href = "/";
    }
  }

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
              onSignOut={handleSignOut}
              onClose={() => setMobileOpen(false)}
            />
          </aside>
          <div className="flex-1 bg-black/40" onClick={() => setMobileOpen(false)} />
        </div>
      )}

      {/* Desktop/tablet sidebar — unchanged */}
      <aside className="hidden sm:flex w-52 bg-[#0d0d0d] min-h-screen flex-col shrink-0">
        <SidebarContent active={active} onNavigate={(path) => router.push(path)} onSignOut={handleSignOut} />
      </aside>
    </>
  );
}
