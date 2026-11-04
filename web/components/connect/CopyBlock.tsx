"use client";

import { useEffect, useState } from "react";
import styles from "./connect.module.css";

/** Clipboard API needs a secure context; fall back to a hidden textarea on plain http. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand("copy");
    el.remove();
    return ok;
  }
}

export function CopyButton({
  text,
  label = "Copy",
  className,
}: {
  text: string;
  label?: string;
  className?: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = setTimeout(() => setState("idle"), 2000);
    return () => clearTimeout(t);
  }, [state]);
  return (
    <button
      type="button"
      className={className ?? styles.secondary}
      onClick={() => void copyText(text).then((ok) => setState(ok ? "copied" : "failed"))}
    >
      <span aria-live="polite">
        {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : label}
      </span>
    </button>
  );
}

/**
 * A command shown in full (wrapped, selectable) with a copy button. `tall` caps the height for
 * long scripts; the box scrolls inside itself so the page never scrolls sideways.
 */
export function CopyBlock({ text, label, tall }: { text: string; label?: string; tall?: boolean }) {
  return (
    <div className={`${styles.codeBlock} ${tall ? styles.codeTall : ""}`}>
      <pre aria-label={label ?? "Command"} tabIndex={tall ? 0 : undefined}>
        <code>{text}</code>
      </pre>
      <CopyButton text={text} label={tall ? "Copy all" : "Copy"} className={styles.copyBtn} />
    </div>
  );
}
