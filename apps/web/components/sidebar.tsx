"use client";

import { useRouter } from "next/navigation";
import {
  LayoutDashboard,
  Upload,
  Building2,
  Settings,
  LogOut,
} from "lucide-react";
import { DashrLogo } from "./dashr-logo";
import { logout } from "@/lib/api";

const NAV = [
  { id: "dashboard", label: "Leads", icon: LayoutDashboard, path: "/dashboard" },
  { id: "product", label: "Upload", icon: Upload, path: "/product" },
  { id: "profile", label: "Company Profile", icon: Building2, path: "/profile" },
  { id: "home", label: "Settings", icon: Settings, path: "/" },
];

export function Sidebar({ active }: { active: string }) {
  const router = useRouter();

  async function handleSignOut() {
    try {
      await logout();
    } finally {
      router.push("/");
    }
  }

  return (
    <aside className="w-52 bg-[#0d0d0d] min-h-screen flex flex-col shrink-0">
      <div className="px-5 py-4 border-b border-white/8">
        <DashrLogo onClick={() => router.push("/")} height={28} />
      </div>
      <nav className="flex-1 p-3 space-y-0.5 mt-1">
        {NAV.map(({ id, label, icon: Icon, path }) => (
          <button
            key={id}
            onClick={() => router.push(path)}
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
          onClick={handleSignOut}
          className="w-full flex items-center gap-3 px-3 py-2.5 text-sm text-white/35 hover:text-white transition-colors hover:bg-white/5"
        >
          <LogOut size={14} />
          Sign Out
        </button>
      </div>
    </aside>
  );
}
