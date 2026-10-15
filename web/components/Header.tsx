"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useSession } from "./AuthProvider";
import styles from "./Header.module.css";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/deployments", label: "Deployments" },
  { href: "/connect", label: "Connect Azure" },
  { href: "/connections", label: "Connections" },
] as const;

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function Header() {
  const { api, accountName, signOut } = useSession();
  const pathname = usePathname() ?? "/";
  const [meName, setMeName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => !cancelled && setMeName(me.name ?? me.email ?? me.oid))
      .catch(() => {}); // The page shows API errors; the header just omits the name.
    return () => {
      cancelled = true;
    };
  }, [api]);

  const name = accountName ?? meName;
  return (
    <header className={styles.header}>
      <Link href="/" className={styles.brand}>
        AI Cost Tracker
      </Link>
      <nav className={styles.nav} aria-label="Main">
        {NAV.map((item) => {
          const active = isActive(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={active ? styles.navActive : styles.navLink}
              aria-current={active ? "page" : undefined}
            >
              {item.label}
            </Link>
          );
        })}
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
