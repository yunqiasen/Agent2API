import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fmtTs, type ModelPricing } from "@/lib/api";

export function ModelPrice({ name, pricing }: { name: string; pricing: ModelPricing }) {
  const tone = pricing.stale
    ? "bg-amber-500/10 text-amber-700 dark:text-amber-300"
    : pricing.status === "free"
      ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
      : "bg-background/70 text-muted-foreground";
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`${name} 倍率说明`}
          className={`rounded px-1 py-0.5 text-[10px] font-medium tabular-nums focus-visible:outline-2 focus-visible:outline-ring ${tone}`}>
          {pricing.label}{pricing.stale && <span className="ml-1">· 旧</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-2 text-xs" align="start">
        <p className="font-medium">{name} · {pricing.label}</p>
        <p className="text-muted-foreground">官方积分倍率，仅供参考；实际扣费以上游为准。</p>
        {pricing.note && <p className="text-amber-700 dark:text-amber-300">{pricing.note}</p>}
        <p>最近成功同步：{fmtTs(pricing.updated_at)}</p>
        {pricing.source && <p className="break-all text-muted-foreground">来源：{pricing.source}</p>}
        {!!pricing.observations?.length && (
          <ul className="max-h-40 space-y-1 overflow-y-auto rounded-md bg-muted p-2 font-mono text-[11px]">
            {pricing.observations.map((s) => (
              <li key={s.account_id}>#{s.account_id} · {s.raw || "未公布"}{s.stale ? " · 旧数据" : ""}</li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  );
}
