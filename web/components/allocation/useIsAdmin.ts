"use client";

import { useEffect, useState } from "react";
import { useSession } from "../AuthProvider";

/**
 * Whether the signed-in user may change allocation settings. Mirrors require_role("admin"):
 * owner satisfies any role. False until /me answers, so admin controls never flash.
 */
export function useIsAdmin(): boolean {
  const { api } = useSession();
  const [admin, setAdmin] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => !cancelled && setAdmin(me.roles.includes("admin") || me.roles.includes("owner")))
      .catch(() => !cancelled && setAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [api]);
  return admin;
}
