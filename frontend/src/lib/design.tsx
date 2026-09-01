import { useEffect, useRef, useState, type ReactNode, type ComponentType } from "react";
import { Copy } from "lucide-react";
import { toast } from "sonner";

/* ---------- shadcn built-in animation (tw-animate-css) ----------
   We deliberately rely on the component library's default animation
   utilities (animate-in / fade-in / slide-in-from-*) and do NOT add any
   custom keyframes or gradient/colored CSS.                  ---------- */

/* Neutral accent map — kept only so existing imports keep compiling.
   Every tone resolves to the same shadcn semantic classes. */
const NEUTRAL = {
  chip: "bg-secondary text-secondary-foreground",
  bar: "bg-primary",
  text: "text-foreground",
  ring: "ring-primary/40",
  solid: "bg-primary text-primary-foreground",
} as const;
export const TONE = {
  indigo: NEUTRAL,
  violet: NEUTRAL,
  fuchsia: NEUTRAL,
  emerald: NEUTRAL,
  amber: NEUTRAL,
  rose: NEUTRAL,
  cyan: NEUTRAL,
  sky: NEUTRAL,
  orange: NEUTRAL,
  slate: NEUTRAL,
} as const;
export type Tone = keyof typeof TONE;
export const TONE_KEYS = Object.keys(TONE) as Tone[];
export function toneFor(_key: string): Tone {
  return "indigo";
}

/* ---------- animated counter ---------- */
export function useCountUp(value: number, dur = 900) {
  const [v, setV] = useState(0);
  const fromRef = useRef(0);
  useEffect(() => {
    const from = fromRef.current;
    const to = value || 0;
    const start = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - start) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      setV(from + (to - from) * e);
      if (p < 1) raf = requestAnimationFrame(step);
      else fromRef.current = to;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, dur]);
  return v;
}

/* ---------- tile ---------- */
export function Tile({
  icon: Icon,
  label,
  value,
  hint,
  tone: _tone,
  delay = 0,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  delay?: number;
}) {
  return (
    <div
      className="group relative overflow-hidden rounded-xl border bg-card p-4 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-sm animate-in fade-in slide-in-from-bottom-2"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="flex items-start justify-between">
        <div className="inline-flex size-9 items-center justify-center rounded-lg bg-muted text-muted-foreground transition-transform duration-300 group-hover:scale-110 group-hover:rotate-3">
          <Icon className="size-4" />
        </div>
      </div>
      <div className="mt-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className="text-2xl font-semibold tabular-nums tracking-tight">{value}</span>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

/* ---------- section card ---------- */
export function SectionCard({
  title,
  desc,
  icon: Icon,
  tone,
  right,
  children,
  className = "",
  delay = 0,
  bodyClassName = "",
}: {
  title: string;
  desc?: string;
  icon?: ComponentType<{ className?: string }>;
  tone?: Tone;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  delay?: number;
  bodyClassName?: string;
}) {
  return (
    <div
      className={`rounded-xl border bg-card animate-in fade-in slide-in-from-bottom-2 ${className}`}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center justify-between gap-3 border-b px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          {Icon && (
            <div className="inline-flex size-7 items-center justify-center rounded-md bg-muted text-muted-foreground">
              <Icon className="size-3.5" />
            </div>
          )}
          <div>
            <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
            {desc && <p className="mt-0.5 text-[11px] text-muted-foreground">{desc}</p>}
          </div>
        </div>
        {right}
      </div>
      {children && <div className={bodyClassName || ""}>{children}</div>}
    </div>
  );
}

/* ---------- progress bar ---------- */
export function Progress({ value, max, tone: _tone }: { value: number; max: number; tone?: Tone }) {
  const pct = Math.min(100, Math.max(0, max > 0 ? (value / max) * 100 : 0));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div
        className="h-full rounded-full bg-primary transition-all duration-700"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/* ---------- copy button ---------- */
export function Copyable({ text, className = "" }: { text: string; className?: string }) {
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard.writeText(text).then(
          () => toast.success("已复制"),
          () => toast.error("复制失败")
        );
      }}
      className={`inline-flex items-center gap-1 rounded text-[10px] text-muted-foreground transition-colors hover:text-foreground ${className}`}
    >
      <Copy className="size-3" />
    </button>
  );
}

/* ---------- chip (neutral) ---------- */
export function Chip({ children, className = "", tone: _tone }: { children: ReactNode; className?: string; tone?: Tone }) {
  return (
    <span
      className={`inline-flex h-5 items-center rounded-md bg-secondary px-1.5 text-[10px] font-medium text-secondary-foreground ${className}`}
    >
      {children}
    </span>
  );
}

/* ---------- page header ---------- */
export function PageHeader({
  icon: Icon,
  title,
  subtitle,
  right,
  delay = 0,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: string;
  subtitle?: string;
  right?: ReactNode;
  delay?: number;
}) {
  return (
    <div
      className="flex flex-wrap items-end justify-between gap-3 animate-in fade-in slide-in-from-top-2 duration-500"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          {Icon && <Icon className="size-5 text-primary" />}
          <span>{title}</span>
        </h1>
        {subtitle && <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>}
      </div>
      {right && <div className="flex flex-wrap items-center gap-2">{right}</div>}
    </div>
  );
}
