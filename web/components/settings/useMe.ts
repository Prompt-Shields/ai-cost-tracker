"use client";

import { useEffect, useState } from "react";
import type { Me } from "@/lib/api";
import { useSession } from "../AuthProvider";

/** The signed-in principal from /me; null while loading or if it failed (callers degrade). */
export function useMe(): { me: Me | null; loaded: boolean } {
  const { api } = useSession();
  const [state, setState] = useState<{ me: Me | null; loaded: boolean }>({
    me: null,
    loaded: false,
  });
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => !cancelled && setState({ me, loaded: true }))
      .catch(() => !cancelled && setState({ me: null, loaded: true }));
    return () => {
      cancelled = true;
    };
  }, [api]);
  return state;
}
