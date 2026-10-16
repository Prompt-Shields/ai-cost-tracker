"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { isAdmin } from "@/lib/api-m2-admin";
import { useSession } from "./AuthProvider";
import { useMe } from "./settings/useMe";
import styles from "./Header.module.css";

interface NavItem {
  href: string;
  label: string;
  adminOnly?: boolean;
}

/** Daily-use pages stay visible; the rest live in the "More" menu so the bar fits a phone. */
const PRIMARY: NavItem[] = [
  { href: "/", label: "Overview" },
  { href: "/deployments", label: "Deployments" },
  { href: "/cost-centres", label: "Cost centres" },
  { href: "/alerts", label: "Alerts" },
];

const MORE: { title: string; items: NavItem[] }[] = [
  {
    title: "Spend",
    items: [
      { href: "/budgets", label: "Budgets" },
      { href: "/allocation", label: "Allocation" },
      { href: "/ptu", label: "PTU" },
      { href: "/invoices", label: "Invoices" },
    ],
  },
  {
    title: "Setup",
    items: [
      { href: "/notifications", label: "Notifications" },
      { href: "/connections", label: "Connections" },
      { href: "/connect", label: "Connect Azure" },
      { href: "/settings/api-keys", label: "API keys" },
      { href: "/audit", label: "Audit log", adminOnly: true },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function Header() {
  const { accountName, signOut } = useSession();
  const { me } = useMe();
  const pathname = usePathname() ?? "/";
  const admin = isAdmin(me?.roles);

  const name = accountName ?? (me ? (me.name ?? me.email ?? me.oid) : null);
  const groups = MORE.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.adminOnly || admin),
  }));
  const moreActive = groups.some((g) => g.items.some((i) => isActive(pathname, i.href)));

  return (
    <header className={styles.header}>
      <Link href="/" className={styles.brand}>
        AI Cost Tracker
      </Link>
      <nav className={styles.nav} aria-label="Main">
        {PRIMARY.map((item) => (
          <NavLink key={item.href} item={item} active={isActive(pathname, item.href)} />
        ))}
        <MoreMenu groups={groups} pathname={pathname} active={moreActive} />
      </nav>
      <div className={styles.user}>
        {name && <span className={styles.name}>{name}</span>}
        {signOut && (
          <button type="button" className={styles.signOut} onClick={signOut}>
            Sign out
          </button>
        )}
      </div>
    </header>
  );
}

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  return (
    <Link
      href={item.href}
      className={active ? styles.navActive : styles.navLink}
      aria-current={active ? "page" : undefined}
    >
      {item.label}
    </Link>
  );
}

function MoreMenu({
  groups,
  pathname,
  active,
}: {
  groups: { title: string; items: NavItem[] }[];
  pathname: string;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on navigation, outside click and Escape, like a native menu.
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        ref.current?.querySelector("button")?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const current = groups.flatMap((g) => g.items).find((i) => isActive(pathname, i.href));

  return (
    <div className={styles.more} ref={ref}>
      <button
        type="button"
        className={active ? styles.navActive : styles.navLink}
        aria-expanded={open}
        aria-controls="nav-more"
        onClick={() => setOpen((o) => !o)}
      >
        {current ? current.label : "More"} <span aria-hidden>▾</span>
      </button>
      {open && (
        <div id="nav-more" className={styles.menu}>
          {groups.map((g) => (
            <div key={g.title} className={styles.menuGroup}>
              <div className={styles.menuTitle}>{g.title}</div>
              {g.items.map((item) => {
                const on = isActive(pathname, item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={on ? styles.menuActive : styles.menuLink}
                    aria-current={on ? "page" : undefined}
                    onClick={() => setOpen(false)}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
