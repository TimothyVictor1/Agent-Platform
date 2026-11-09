"use client";
import Link from "next/link";
import { useState } from "react";
import { usePathname } from "next/navigation";
import { env } from "@/lib/env";
import { Button } from "@/components/ui/button";
export function AppNav() {
  const [dark, setDark] = useState(true);
  const pathname = usePathname();
  return <header className="border-b bg-card/50"><nav aria-label="Main navigation" className="mx-auto flex max-w-6xl flex-wrap items-center gap-6 px-6 py-4">
    <Link href="/" className="mr-auto font-semibold tracking-tight">{env.appName}</Link>
    <Link href="/" aria-current={pathname === "/" || pathname.startsWith("/sessions") ? "page" : undefined}>Sessions</Link>
    <Link href="/settings" aria-current={pathname === "/settings" ? "page" : undefined}>Settings</Link>
    <Button variant="outline" aria-label="Toggle dark theme" aria-pressed={dark} onClick={() => { document.documentElement.classList.toggle("dark", !dark); setDark(!dark); }}>{dark ? "Light theme" : "Dark theme"}</Button>
  </nav></header>;
}
