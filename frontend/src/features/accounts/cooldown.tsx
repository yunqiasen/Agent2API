import { useEffect, useState } from "react";
import { type AccountCooldown, platName } from "@/lib/api";
import { Chip } from "@/lib/design";

export function CooldownCard({ items }: { items?: AccountCooldown[] }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!items?.length) return;
    setNow(Date.now() / 1000);
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [items]);
  const active = items?.filter(x => x.retry_at > now) || [];
  if (!active.length) return null;
  return <div className="space-y-1.5 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-[11px]" aria-label="模型限流">
    <Chip tone="amber">限流冷却</Chip>
    {active.map(c => <div key={`${c.service}:${c.model}`} className="break-words">
      <p><span>{c.service === "intl" ? "国际共享服务" : platName(c.service)} · {c.model || "该服务全部模型"}</span> · {Math.ceil(c.retry_at - now)} 秒后可再尝试</p>
      <p className="text-muted-foreground">恢复：{new Date(c.retry_at * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}（Asia/Shanghai）</p>
      <p>{c.message}</p>
    </div>)}
    <p className="text-muted-foreground">自动跳过冷却账号，凭证与每日奖励照常维护</p>
  </div>;
}
