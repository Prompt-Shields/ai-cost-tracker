"use client";

import { useEffect, useState } from "react";
import { useSession } from "./AuthProvider";
import styles from "./Header.module.css";

export function Header() {
  const { api, accountName, signOut } = useSession();
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
      <span className={styles.brand}>AI Cost Tracker</span>
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
