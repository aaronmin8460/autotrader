"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useTerminal } from "@/lib/terminal";
import { useTerminalCopy, type TerminalCopyKey } from "@/lib/terminal-copy";

import { LanguageSwitcher, ThemeSwitcher } from "./GlobalControls";

type NavKey = Exclude<TerminalCopyKey, "readOnly" | "realMoney" | "unavailable">;

interface Destination {
  key: NavKey;
  href: string;
  code: string;
  aliases?: string[];
  research?: boolean;
  live?: boolean;
}

const DESTINATIONS: readonly Destination[] = [
  { key: "command", href: "/", code: "01" },
  { key: "live", href: "/live", code: "02", aliases: ["/portfolio"], live: true },
  { key: "strategy", href: "/strategy", code: "03", aliases: ["/strategies"] },
  { key: "execution", href: "/execution", code: "04", aliases: ["/orders"] },
  { key: "risk", href: "/risk", code: "05" },
  { key: "performance", href: "/performance", code: "06" },
  { key: "capital", href: "/capital", code: "07" },
  { key: "compare", href: "/compare", code: "08", aliases: ["/equity-paper"] },
  {
    key: "research",
    href: "/research",
    code: "09",
    aliases: ["/shadows", "/equity-shadow"],
    research: true,
  },
  { key: "system", href: "/system", code: "10" },
];

const KEY_ROUTES: Readonly<Record<string, string>> = {
  c: "/",
  l: "/live",
  s: "/strategy",
  e: "/execution",
  r: "/risk",
  p: "/performance",
};

function Glyph({ name }: { name: NavKey }) {
  const paths: Record<NavKey, ReactNode> = {
    command: <><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><path d="M14 18h7M17.5 14v8"/></>,
    live: <><path d="M4 18V9M10 18V5M16 18v-7M22 18V3"/><path d="M2 21h22"/></>,
    strategy: <><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M22 12h-3M12 22v-3M2 12h3"/></>,
    execution: <><path d="M4 7h12M12 3l4 4-4 4M20 17H8M12 13l-4 4 4 4"/></>,
    risk: <><path d="M12 3l9 4v6c0 5-3.7 8-9 9-5.3-1-9-4-9-9V7z"/><path d="M12 8v5M12 17h.01"/></>,
    performance: <><path d="M3 19l6-7 4 3 8-10"/><path d="M16 5h5v5"/></>,
    capital: <><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></>,
    compare: <><rect x="3" y="5" width="7" height="14"/><rect x="14" y="5" width="7" height="14"/><path d="M10 9h4M10 15h4"/></>,
    research: <><path d="M9 3h6M10 3v6l-6 10a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3L14 9V3"/><path d="M7 16h10"/></>,
    system: <><circle cx="12" cy="12" r="3"/><path d="M19 13.5l2 1.5-2 3.5-2.5-1a8 8 0 0 1-2.5 1.4L13.5 22h-4l-.5-3.1a8 8 0 0 1-2.5-1.4l-2.5 1L2 15l2-1.5a8 8 0 0 1 0-3L2 9l2-3.5 2.5 1A8 8 0 0 1 9 5.1L9.5 2h4l.5 3.1a8 8 0 0 1 2.5 1.4l2.5-1L21 9l-2 1.5a8 8 0 0 1 0 3z"/></>,
  };
  return <svg viewBox="0 0 24 24" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function stateTone(value: string | null | undefined): string {
  if (!value) return "unknown";
  if (["ARMED", "BLOCKED", "FAILED", "MISMATCH"].includes(value)) return "danger";
  if (["WARN", "STALE", "DEGRADED", "UNKNOWN"].includes(value)) return "warn";
  if (["READY", "CLEAN", "OK", "PASS", "RUNNING", "FRESH", "PINNED", "YES"].includes(value)) return "good";
  return "neutral";
}

function StatusCell({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return <div className={`tv4-status-cell ${tone ?? stateTone(value)}`}><span className="tv4-status-dot"/><span className="tv4-status-label">{label}</span><strong>{value}</strong></div>;
}

function StatusRibbon() {
  const { safety, accounting, connected } = useTerminal();
  const live = safety.data;
  const money = accounting.data;
  const [clock, setClock] = useState("--:--:--");
  useEffect(() => {
    const update = () => setClock(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date()));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <header className="tv4-ribbon" aria-label="Global terminal status">
      <div className="tv4-ribbon-scroll">
        <div className="tv4-live-word">AUTOTRADER · LIVE</div>
        <StatusCell label="AUTH" value={live?.arm.state ?? "UNKNOWN"}/>
        <StatusCell label="READY" value={live?.live_ready === true ? "YES" : live?.live_ready === false ? "NO" : "UNKNOWN"} tone={live?.live_ready ? "good" : "warn"}/>
        <StatusCell label="RECON" value={live?.reconciliation.status ?? "UNKNOWN"}/>
        <StatusCell label="ACCT" value={money?.accounting_status ?? "UNKNOWN"}/>
        <StatusCell label="BROKER" value={live?.account.status ?? "UNKNOWN"}/>
        <StatusCell label="DATA" value={money?.data_freshness ?? (connected ? "FRESH" : "UNKNOWN")}/>
        <div className="tv4-sha">SHA <strong>{live?.code_sha?.slice(0, 8) ?? "UNKNOWN"}</strong></div>
      </div>
      <div className="tv4-clock"><span>NEW YORK</span><strong>{clock} ET</strong></div>
    </header>
  );
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname();
  const copy = useTerminalCopy();
  return (
    <aside className="tv4-sidebar">
      <Link href="/" className="tv4-brand" onClick={onNavigate}><span className="tv4-brand-mark">AT</span><span><strong>AUTOTRADER</strong><small>TERMINAL V4</small></span></Link>
      <div className="tv4-environment"><span className="tv4-pulse"/>LIVE · REAL MONEY</div>
      <nav className="tv4-nav" aria-label="Terminal workspaces">
        {DESTINATIONS.map((item) => {
          const active = path === item.href || item.aliases?.some((alias) => path.startsWith(alias));
          return <Link key={item.href} href={item.href} onClick={onNavigate} aria-current={active ? "page" : undefined} className={`${active ? "active" : ""} ${item.research ? "research" : ""} ${item.live ? "live" : ""}`}><Glyph name={item.key}/><span>{copy(item.key)}</span><small>{item.code}</small></Link>;
        })}
      </nav>
      <div className="tv4-sidebar-foot"><div className="tv4-control-row"><LanguageSwitcher/><ThemeSwitcher/></div><p>{copy("readOnly")}</p><span>ALL TIMES UTC UNLESS MARKED ET</span></div>
    </aside>
  );
}

function CommandPalette({ open, close }: { open: boolean; close: () => void }) {
  const copy = useTerminalCopy();
  const router = useRouter();
  const { terminal } = useTerminal();
  const [query, setQuery] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (open) { setQuery(""); window.setTimeout(() => input.current?.focus(), 0); } }, [open]);
  const commands = useMemo(() => {
    const routes = DESTINATIONS.map((item) => ({ label: `Go to ${copy(item.key)}`, detail: `WORKSPACE ${item.code}`, href: item.href }));
    const symbols = (terminal.data?.strategy?.universe ?? []).map((symbol) => ({ label: `Open ${symbol}`, detail: "LIVE POSITION INSPECTOR", href: `/live?symbol=${encodeURIComponent(symbol)}` }));
    const all = [...routes, ...symbols];
    const needle = query.trim().toLowerCase();
    return needle ? all.filter((item) => `${item.label} ${item.detail}`.toLowerCase().includes(needle)) : all;
  }, [copy, query, terminal.data]);
  if (!open) return null;
  return (
    <div className="tv4-overlay" role="presentation" onMouseDown={close}>
      <section className="tv4-palette" role="dialog" aria-modal="true" aria-label="Command palette" onMouseDown={(event) => event.stopPropagation()}>
        <div className="tv4-palette-input"><span>⌘K</span><input ref={input} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Navigate or inspect a symbol…"/></div>
        <div className="tv4-palette-list">{commands.slice(0, 12).map((command) => <button key={`${command.href}-${command.label}`} type="button" onClick={() => { close(); router.push(command.href); }}><span>{command.label}</span><small>{command.detail}</small></button>)}{commands.length === 0 ? <p>NO MATCHING READ-ONLY COMMAND</p> : null}</div>
        <footer><span>↑↓ SELECT</span><span>ENTER OPEN</span><span>ESC CLOSE</span><strong>DISPLAY ACTIONS ONLY</strong></footer>
      </section>
    </div>
  );
}

function MobileBar({ openMenu, openPalette }: { openMenu: () => void; openPalette: () => void }) {
  const path = usePathname();
  const copy = useTerminalCopy();
  const primary = DESTINATIONS.filter((item) => ["command", "live", "execution", "risk"].includes(item.key));
  return <><header className="tv4-mobile-head"><button type="button" onClick={openMenu} aria-label="Open navigation">☰</button><Link href="/"><strong>AT</strong><span>TERMINAL V4</span></Link><button type="button" onClick={openPalette} aria-label="Open command palette">⌘K</button></header><nav className="tv4-mobile-tabs" aria-label="Primary mobile navigation">{primary.map((item) => <Link key={item.href} href={item.href} className={path === item.href ? "active" : ""}><Glyph name={item.key}/><span>{copy(item.key)}</span></Link>)}<button type="button" onClick={openMenu}><span className="tv4-more">•••</span><span>More</span></button></nav></>;
}

export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [palette, setPalette] = useState(false);
  const [menu, setMenu] = useState(false);
  const chord = useRef(false);
  const chordTimer = useRef<number | null>(null);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setPalette((value) => !value); return; }
      if (event.key === "Escape") { setPalette(false); setMenu(false); return; }
      if (!typing && event.key === "/") { event.preventDefault(); setPalette(true); return; }
      if (typing) return;
      const key = event.key.toLowerCase();
      if (chord.current) {
        chord.current = false;
        if (chordTimer.current !== null) window.clearTimeout(chordTimer.current);
        const href = KEY_ROUTES[key];
        if (href) { event.preventDefault(); router.push(href); }
        return;
      }
      if (key === "g") { chord.current = true; chordTimer.current = window.setTimeout(() => { chord.current = false; }, 900); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);
  return (
    <div className="tv4-shell">
      <a href="#terminal-main" className="tv4-skip">Skip to terminal content</a>
      <div className="tv4-desktop-sidebar"><Sidebar/></div>
      {menu ? <div className="tv4-mobile-drawer"><button type="button" className="tv4-drawer-close" onClick={() => setMenu(false)} aria-label="Close navigation">×</button><Sidebar onNavigate={() => setMenu(false)}/></div> : null}
      <MobileBar openMenu={() => setMenu(true)} openPalette={() => setPalette(true)}/>
      <div className="tv4-workspace"><StatusRibbon/><main id="terminal-main" className="tv4-main">{children}</main></div>
      <button type="button" className="tv4-command-key" onClick={() => setPalette(true)} aria-label="Open command palette"><span>⌘</span>K</button>
      <CommandPalette open={palette} close={() => setPalette(false)}/>
    </div>
  );
}
