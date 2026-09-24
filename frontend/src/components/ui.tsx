// Small shadcn-style primitives written by hand (no runtime UI dependency).
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

export function Card({ title, actions, children, className }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900", className)}>
      {(title || actions) && (
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Button({ variant = "primary", className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger" | "success";
}) {
  const styles = {
    primary: "bg-indigo-600 text-white hover:bg-indigo-500",
    ghost: "border border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800",
    danger: "bg-rose-600 text-white hover:bg-rose-500",
    success: "bg-emerald-600 text-white hover:bg-emerald-500",
  }[variant];
  return (
    <button
      className={cx(
        "rounded-lg px-3 py-1.5 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-50",
        styles,
        className,
      )}
      {...props}
    />
  );
}

const DECISION_STYLE: Record<string, string> = {
  ALLOW: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200",
  STEP_UP: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
  HOLD: "bg-orange-100 text-orange-800 dark:bg-orange-900/50 dark:text-orange-200",
  BLOCK: "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-200",
};
const DECISION_TEXT: Record<string, string> = {
  ALLOW: "İZİN", STEP_UP: "DOĞRULAMA", HOLD: "BEKLET", BLOCK: "BLOKE",
};

export function DecisionBadge({ decision }: { decision?: string | null }) {
  const d = decision ?? "";
  return (
    <span className={cx("inline-block rounded-full px-2 py-0.5 text-xs font-bold", DECISION_STYLE[d] ?? "bg-slate-200 dark:bg-slate-700")}>
      {DECISION_TEXT[d] ?? (d || "—")}
    </span>
  );
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "indigo" | "rose" | "amber" | "emerald" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
    indigo: "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/50 dark:text-indigo-200",
    rose: "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-200",
    amber: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
    emerald: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200",
  };
  return <span className={cx("inline-block rounded px-1.5 py-0.5 text-xs font-medium", tones[tone])}>{children}</span>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function RiskBar({ value }: { value: number }) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  const color = value >= 0.85 ? "bg-rose-500" : value >= 0.6 ? "bg-orange-500" : value >= 0.35 ? "bg-amber-400" : "bg-emerald-500";
  return (
    <div className="flex items-center gap-2" aria-label={`risk ${pct}%`}>
      <div className="h-2 w-20 overflow-hidden rounded bg-slate-200 dark:bg-slate-700">
        <div className={cx("h-full", color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums">{value.toFixed(2)}</span>
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <p role="alert" className="rounded-lg bg-rose-50 p-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200">{error}</p> : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}
